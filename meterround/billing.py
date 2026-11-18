"""Bill a metering batch into integer cents.

The bill reads one frozen snapshot: the batch, its slots, and the decisions that
the same batch produced.  Only a snapshot in which the main slot and every
tenant slot are settleable may be billed.  Every other shape is returned as one
`待核` bill with the reasons attached; nothing is partially billed and a negative
common pool is never clamped to zero.

Money is exact: direct usage is rounded per line with largest-remainder, and the
common pool (= main slot cost - tenant direct cost) is allocated by an exact
ratio, also with largest remainder, so the tenant amounts sum to the main
amount.  Two allocation bases are supported, matching the contract's two valid
liability splits:

* ``usage_share``    - each tenant's share of this period's direct usage;
* ``capacity_share`` - each tenant's share of subscribed capacity.

The result is a true-up against anything already issued or billed: the tenant's
issued invoice amount is netted against the freshly computed bill for the same
period, so the ledger never charges the same period twice.
"""
import hashlib
import json
from collections import defaultdict
from fractions import Fraction

from . import store

METHODS=('usage_share','capacity_share')
METHOD_LABELS={'usage_share':'按本期直接用量比例','capacity_share':'按签约容量比例'}
STATUS_PENDING='待核'
STATUS_BILLED='已出账'
SNAPSHOT_VERSION='1'


class BillingError(ValueError):
    """Stable, machine-checkable billing failure (``code`` is the payload)."""

    def __init__(self,code):
        self.code=code
        super().__init__(code)


def _canonical(document):
    return json.dumps(document,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def _sha(document):
    return hashlib.sha256(_canonical(document).encode('utf-8')).hexdigest()


def _digest_rows(rows):
    return _sha([_canonical(dict(r)) for r in rows])


def largest_remainder(weights,total):
    """Split ``total`` integer cents across ``weights`` exactly.

    The floor of each exact share is paid out, then the remaining cents go one
    each to the largest fractional remainders; ties break on the original index.
    The returned list sums to ``total`` and is deterministic.
    """
    if total<0:
        raise BillingError('negative_total')
    if not weights:
        if total: raise BillingError('no_allocation_weights')
        return []
    base=sum(Fraction(w) for w in weights)
    if base<=0:
        if total: raise BillingError('no_allocation_weights')
        return [0]*len(weights)
    sums=[total*Fraction(w)/base for w in weights]
    floors=[int(s) for s in sums]
    remainder=total-sum(floors)
    order=sorted(range(len(weights)),key=lambda i:(-(sums[i]-floors[i]),i))
    for i in order[:remainder]:
        floors[i]+=1
    return floors


def _number(value):
    """Render usage/price deterministically as int when whole, else float."""
    as_float=float(value)
    return int(as_float) if as_float.is_integer() else as_float


def _parse_detail(detail):
    try:
        return json.loads(detail) if detail else {}
    except (TypeError,ValueError):
        return {}


def _tenant_rows(db,contract_id):
    return [dict(r) for r in db.execute('SELECT tenant_id,slot,subscribed_capacity FROM billing_tenant WHERE contract_id=? ORDER BY tenant_id',(contract_id,))]


def _batch_snapshot(db,batch_id,slot_ids):
    batch=db.execute('SELECT * FROM batch WHERE id=?',(batch_id,)).fetchone()
    if batch is None:raise BillingError('unknown_batch')
    decisions={}
    for slot_id in slot_ids:
        decision=db.execute('SELECT * FROM decision WHERE slot=?',(slot_id,)).fetchone()
        if decision is None:raise BillingError('missing_decision')
        decisions[slot_id]=dict(decision)
    source_rows=[dict(r) for r in db.execute('SELECT r.slot,r.at,r.value,s.sha256 AS source_sha256 FROM reading r JOIN source s ON s.id=r.source_id WHERE r.slot IN (%s) ORDER BY r.slot,r.at,r.id' % ','.join('?'*len(slot_ids)),slot_ids)] if slot_ids else []
    evidence_rows=[dict(r) for r in db.execute('SELECT e.reading_id,e.source_sha256,e.payload_sha256,e.role FROM evidence e JOIN reading r ON r.id=e.reading_id WHERE r.slot IN (%s) ORDER BY e.id' % ','.join('?'*len(slot_ids)),slot_ids)] if slot_ids else []
    return dict(batch),decisions,source_rows,evidence_rows


def _pending(reason,method,contract_id,batch_id,tenant_rows,snapshot_sha256,decision_refs=None):
    return {'status':STATUS_PENDING,'contract_id':contract_id,'batch_id':batch_id,'allocation_method':method,
            'invoice_id':None,'snapshot_sha256':snapshot_sha256,'reasons':[reason],
            'decision_refs':decision_refs or {},'source_sha256':'','evidence_sha256':'','contract_sha256':'',
            'main_usage':None,'main_cents':None,'common_pool_cents':None,'total_cents':None,
            'lines':[], 'detail':{'reasons':[reason],'tenants':[t['tenant_id'] for t in tenant_rows]}}


def _fail(reason,method,contract_id,batch_id,tenant_rows,snapshot_sha256=None,decision_refs=None):
    snapshot_sha256=snapshot_sha256 or _sha({'reason':reason,'method':method,'contract_id':contract_id,'batch_id':batch_id})
    return _pending(reason,method,contract_id,batch_id,tenant_rows,snapshot_sha256,decision_refs)


def plan(db,contract_id,method):
    """Compute (but do not persist) the next bill for one contract/method.

    Returns a record with ``status``, ``snapshot_sha256``, ``reasons``, the exact
    per-tenant ``lines`` and the referenced decisions/sources/evidence.
    """
    if method not in METHODS:raise BillingError('unknown_allocation_method')
    contract=db.execute('SELECT * FROM billing_contract WHERE id=?',(contract_id,)).fetchone()
    if contract is None:raise BillingError('unknown_contract')
    contract=dict(contract)
    tenant_rows=_tenant_rows(db,contract_id)
    batch_id=contract['batch_id']

    issued_invoice=db.execute('SELECT * FROM issued_invoice WHERE contract_id=? AND batch_id=? ORDER BY invoice_id LIMIT 1',(contract_id,batch_id)).fetchone()
    invoice_id=issued_invoice['invoice_id'] if issued_invoice else None
    issued=store.issued_amounts(db,contract_id,batch_id)

    try:
        batch=db.execute('SELECT * FROM batch WHERE id=?',(batch_id,)).fetchone()
        if batch is None:raise BillingError('unknown_batch')
        if contract['unit_price_cents'] is None or isinstance(contract['unit_price_cents'],bool) or int(contract['unit_price_cents'])<=0:
            raise BillingError('invalid_price')
        allowed={r['method'] for r in db.execute('SELECT method FROM billing_contract_allocation WHERE contract_id=?',(contract_id,))}
        if method not in allowed:raise BillingError('allocation_not_contracted')
        if not tenant_rows:raise BillingError('no_tenants')
        slots=defaultdict(int)
        for tenant in tenant_rows:
            slots[tenant['slot']]+=1
        if any(count>1 for count in slots.values()):raise BillingError('duplicate_tenant')
        if contract['main_slot'] in slots:raise BillingError('tenant_is_main')
        main_slot=db.execute('SELECT * FROM slot WHERE id=?',(contract['main_slot'],)).fetchone()
        if main_slot is None:raise BillingError('unknown_main_slot')
        if main_slot['batch_id']!=batch_id:raise BillingError('period_mismatch')
        for tenant in tenant_rows:
            slot=db.execute('SELECT * FROM slot WHERE id=?',(tenant['slot'],)).fetchone()
            if slot is None:raise BillingError('unknown_tenant_slot')
            if slot['batch_id']!=batch_id:raise BillingError('period_mismatch')
            if method=='capacity_share' and (tenant['subscribed_capacity'] is None or float(tenant['subscribed_capacity'])<=0):
                raise BillingError('invalid_capacity')

        slot_ids=[contract['main_slot']]+[t['slot'] for t in tenant_rows]
        batch,decisions,source_rows,evidence_rows=_batch_snapshot(db,batch_id,slot_ids)
        # The decision snapshot is the billing input: contract terms, the same-batch
        # decisions, and what has already been issued for this period.  Raw source
        # and evidence payloads are referenced but do not by themselves change the
        # bill; a supplemental reading changes decisions and therefore the snapshot.
        snapshot_sha256=_sha({'version':SNAPSHOT_VERSION,'contract':_canonical(contract),'batch':_canonical(batch),
                              'tenants':[_canonical(t) for t in tenant_rows],'method':method,
                              'decisions':{k:_canonical(v) for k,v in decisions.items()},
                              'issued':{k:int(v) for k,v in issued.items()}})
        contract_sha256=_sha({'contract':_canonical(contract),'tenants':[_canonical(t) for t in tenant_rows],'method':method})
        decision_refs={k:{'status':v['status'],'usage':v['usage'],'rule_version':v['rule_version'],'detail_sha256':_sha(_parse_detail(v['detail']))} for k,v in decisions.items()}

        for slot_id in slot_ids:
            if decisions[slot_id]['status']!='可结算':
                return _fail('slot_not_settleable:%s' % slot_id,method,contract_id,batch_id,tenant_rows,snapshot_sha256,decision_refs)
            if decisions[slot_id]['usage'] is None:
                return _fail('slot_not_settleable:%s' % slot_id,method,contract_id,batch_id,tenant_rows,snapshot_sha256,decision_refs)

        main_usage=float(decisions[contract['main_slot']]['usage'])
        usages={t['tenant_id']:float(decisions[t['slot']]['usage']) for t in tenant_rows}
        tenant_total=sum(usages.values())
        if main_usage<tenant_total:
            return _fail('negative_common_pool',method,contract_id,batch_id,tenant_rows,snapshot_sha256,decision_refs)

        price=int(contract['unit_price_cents'])
        main_cents=int(round(main_usage*price))
        direct=largest_remainder([usages[t['tenant_id']] for t in tenant_rows],min(int(round(tenant_total*price)),main_cents)) if tenant_total>0 else [0]*len(tenant_rows)
        pool=main_cents-sum(direct)
        if method=='usage_share':
            weights=[usages[t['tenant_id']] for t in tenant_rows]
        else:
            weights=[float(t['subscribed_capacity']) for t in tenant_rows]
        pool_share=largest_remainder(weights,pool)
        lines=[]
        for index,tenant in enumerate(tenant_rows):
            tenant_id=tenant['tenant_id']
            amount=direct[index]+pool_share[index]
            issued_cents=int(issued.get(tenant_id,0))
            lines.append({'tenant_id':tenant_id,'slot':tenant['slot'],'usage':_number(usages[tenant_id]),
                          'direct_cents':direct[index],'pool_cents':pool_share[index],'amount_cents':amount,
                          'issued_cents':issued_cents,'true_up_cents':amount-issued_cents})
        if sum(line['amount_cents'] for line in lines)!=main_cents:
            raise BillingError('allocation_not_exact')
        detail={'batch':batch_id,'method':method,'method_label':METHOD_LABELS[method],
                'main_usage':_number(main_usage),'tenant_usage_total':_number(tenant_total),
                'main_cents':main_cents,'direct_cents_total':sum(direct),'common_pool_cents':pool,
                'total_cents':main_cents,'allocation_weights':[_number(w) for w in weights],
                'unit_price_cents':price,'invoice_id':invoice_id,
                'decisions':decision_refs,'snapshot_sha256':snapshot_sha256,'reasons':[]}
        return {'status':STATUS_BILLED,'contract_id':contract_id,'batch_id':batch_id,'allocation_method':method,
                'invoice_id':invoice_id,'snapshot_sha256':snapshot_sha256,'contract_sha256':contract_sha256,'reasons':[],
                'decision_refs':decision_refs,'source_sha256':_digest_rows(source_rows),'evidence_sha256':_digest_rows(evidence_rows),
                'main_usage':main_usage,'main_cents':main_cents,'common_pool_cents':pool,'total_cents':main_cents,
                'lines':lines,'detail':detail}
    except BillingError as error:
        return _fail(error.code,method,contract_id,batch_id,tenant_rows)


def bill(db,contract_id,method='usage_share'):
    """Plan and persist one bill revision for ``contract_id``/``method``.

    Re-issuing the identical snapshot is a no-op: the existing revision is
    returned and no entry is duplicated.  A changed snapshot becomes a new
    revision, reversing the previous adjustment in the same transaction.
    """
    record=plan(db,contract_id,method)
    row=store.persist_bill(db,record)
    return {**record,'bill_id':row['id'],'revision':row['revision'],'status':row['status']}


def bill_all(db,contract_id):
    contracted=[r['method'] for r in db.execute('SELECT method FROM billing_contract_allocation WHERE contract_id=? ORDER BY method',(contract_id,))]
    return {method:bill(db,contract_id,method) for method in contracted if method in METHODS}


def billable_contracts(db):
    return [r['id'] for r in db.execute('SELECT id FROM billing_contract ORDER BY id')]


def summarize(record):
    """Stable JSON-ready view of a bill, suitable for review and diffing."""
    return {'bill_id':record.get('bill_id'),'contract_id':record['contract_id'],'batch_id':record['batch_id'],
            'allocation_method':record['allocation_method'],'revision':record.get('revision'),
            'status':record['status'],'invoice_id':record.get('invoice_id'),'snapshot_sha256':record['snapshot_sha256'],
            'contract_sha256':record.get('contract_sha256',''),
            'reasons':record.get('reasons',[]),'main_usage':record.get('main_usage'),'main_cents':record.get('main_cents'),
            'common_pool_cents':record.get('common_pool_cents'),'total_cents':record.get('total_cents'),
            'lines':record.get('lines',[]),'decision_refs':record.get('decision_refs',{})}

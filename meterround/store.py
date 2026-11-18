import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS batch(id TEXT PRIMARY KEY,start TEXT NOT NULL,end TEXT NOT NULL,window_hours INTEGER NOT NULL,spike_limit REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,name TEXT NOT NULL,sha256 TEXT NOT NULL UNIQUE,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS slot(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES batch(id));
CREATE TABLE IF NOT EXISTS assignment(slot TEXT NOT NULL REFERENCES slot(id),meter TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,capacity REAL NOT NULL,PRIMARY KEY(slot,meter,start));
CREATE TABLE IF NOT EXISTS multiplier(slot TEXT NOT NULL REFERENCES slot(id),start TEXT NOT NULL,end TEXT NOT NULL,value REAL NOT NULL,PRIMARY KEY(slot,start));
CREATE TABLE IF NOT EXISTS replacement(slot TEXT NOT NULL REFERENCES slot(id),old_meter TEXT NOT NULL,new_meter TEXT NOT NULL,old_final REAL NOT NULL,new_initial REAL NOT NULL,at TEXT NOT NULL,PRIMARY KEY(slot,at));
CREATE TABLE IF NOT EXISTS reading(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES source(id),line_no INTEGER NOT NULL,device TEXT NOT NULL,seq INTEGER NOT NULL,slot TEXT NOT NULL,meter TEXT NOT NULL,at TEXT NOT NULL,value REAL NOT NULL,raw TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(device,seq));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,code TEXT NOT NULL,subject TEXT NOT NULL,detail TEXT NOT NULL,evidence_id INTEGER REFERENCES reading(id));
CREATE TABLE IF NOT EXISTS decision(slot TEXT PRIMARY KEY REFERENCES slot(id),status TEXT NOT NULL,usage REAL,detail TEXT NOT NULL,rule_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, reading_id INTEGER NOT NULL REFERENCES reading(id), source_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL, role TEXT NOT NULL, UNIQUE(reading_id,role));
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')), started_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE IF NOT EXISTS billing_contract(id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, main_slot TEXT NOT NULL, unit_price_cents INTEGER NOT NULL, source_sha256 TEXT NOT NULL, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS billing_tenant(id INTEGER PRIMARY KEY, contract_id TEXT NOT NULL REFERENCES billing_contract(id), tenant_id TEXT NOT NULL, slot TEXT NOT NULL, subscribed_capacity REAL, UNIQUE(contract_id,tenant_id));
CREATE TABLE IF NOT EXISTS billing_contract_allocation(contract_id TEXT NOT NULL REFERENCES billing_contract(id), method TEXT NOT NULL, PRIMARY KEY(contract_id,method));
CREATE TABLE IF NOT EXISTS issued_invoice(invoice_id TEXT PRIMARY KEY, contract_id TEXT NOT NULL REFERENCES billing_contract(id), batch_id TEXT NOT NULL, period TEXT NOT NULL, source_sha256 TEXT NOT NULL, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issued_invoice_line(invoice_id TEXT NOT NULL REFERENCES issued_invoice(invoice_id), tenant_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, PRIMARY KEY(invoice_id,tenant_id));
CREATE TABLE IF NOT EXISTS bill(id INTEGER PRIMARY KEY, contract_id TEXT NOT NULL REFERENCES billing_contract(id), batch_id TEXT NOT NULL, allocation_method TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('待核','已出账')), snapshot_sha256 TEXT NOT NULL, contract_sha256 TEXT NOT NULL DEFAULT '', main_usage REAL, main_cents INTEGER, common_pool_cents INTEGER, total_cents INTEGER, detail TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, supersedes_bill_id INTEGER REFERENCES bill(id), UNIQUE(contract_id,allocation_method,revision));
CREATE TABLE IF NOT EXISTS bill_line(bill_id INTEGER NOT NULL REFERENCES bill(id), tenant_id TEXT NOT NULL, slot TEXT NOT NULL, usage REAL NOT NULL, direct_cents INTEGER NOT NULL, pool_cents INTEGER NOT NULL, amount_cents INTEGER NOT NULL, issued_cents INTEGER NOT NULL, true_up_cents INTEGER NOT NULL, PRIMARY KEY(bill_id,tenant_id));
CREATE TABLE IF NOT EXISTS billing_entry(id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL REFERENCES bill(id), contract_id TEXT NOT NULL REFERENCES billing_contract(id), batch_id TEXT NOT NULL, allocation_method TEXT NOT NULL, tenant_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('adjustment','reversal')), amount_cents INTEGER NOT NULL, reverses_entry_id INTEGER REFERENCES billing_entry(id), decision_refs TEXT NOT NULL, source_sha256 TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, refs TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(bill_id,tenant_id,kind));
"""
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    return db

def _text(document):
    return json.dumps(document,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def save_contract(db,document,source_bytes=None):
    """Persist a contract document. Identity is content-addressed; rewriting the same
    contract text never mutates what earlier bills referenced."""
    raw=source_bytes if source_bytes is not None else _text(document).encode('utf-8')
    digest=hashlib.sha256(raw).hexdigest()
    contract_id=str(document.get('contract_id') or document.get('batch_id') or digest)
    tenants=document.get('tenants') or []
    methods=document.get('allocation_contracts') or ['usage_share']
    unit_price=document.get('unit_price_cents')
    with db:
        db.execute("""INSERT INTO billing_contract(id,batch_id,main_slot,unit_price_cents,source_sha256,document) VALUES (?,?,?,?,?,?)
                      ON CONFLICT(id) DO UPDATE SET batch_id=excluded.batch_id,main_slot=excluded.main_slot,
                        unit_price_cents=excluded.unit_price_cents,source_sha256=excluded.source_sha256,document=excluded.document""",
                   (contract_id,str(document.get('batch_id','')),str(document.get('main_slot','')),
                    0 if not isinstance(unit_price,int) else unit_price,digest,_text(document)))
        kept=[str(tenant['id']) for tenant in tenants]
        if kept:
            db.execute('DELETE FROM billing_tenant WHERE contract_id=? AND tenant_id NOT IN (%s)' % ','.join('?'*len(kept)),[contract_id,*kept])
        else:
            db.execute('DELETE FROM billing_tenant WHERE contract_id=?',(contract_id,))
        for tenant in tenants:
            db.execute('INSERT INTO billing_tenant(contract_id,tenant_id,slot,subscribed_capacity) VALUES (?,?,?,?) ON CONFLICT(contract_id,tenant_id) DO UPDATE SET slot=excluded.slot,subscribed_capacity=excluded.subscribed_capacity',
                       (contract_id,str(tenant['id']),str(tenant['slot']),tenant.get('subscribed_capacity')))
        methods=[str(method) for method in methods]
        if methods:
            db.execute('DELETE FROM billing_contract_allocation WHERE contract_id=? AND method NOT IN (%s)' % ','.join('?'*len(methods)),[contract_id,*methods])
        else:
            db.execute('DELETE FROM billing_contract_allocation WHERE contract_id=?',(contract_id,))
        for method in methods:
            db.execute('INSERT OR IGNORE INTO billing_contract_allocation(contract_id,method) VALUES (?,?)',(contract_id,method))
        issued=document.get('issued_invoice')
        if issued:
            invoice_id=str(issued['invoice_id'])
            period=str(issued.get('period',document.get('batch_id','')))
            db.execute('INSERT OR IGNORE INTO issued_invoice(invoice_id,contract_id,batch_id,period,source_sha256,document) VALUES (?,?,?,?,?,?)',
                       (invoice_id,contract_id,str(document.get('batch_id','')),period,digest,_text(issued)))
            for tenant_id,amount in (issued.get('tenant_total_cents') or {}).items():
                db.execute('INSERT OR IGNORE INTO issued_invoice_line(invoice_id,tenant_id,amount_cents) VALUES (?,?,?)',
                           (invoice_id,str(tenant_id),int(amount)))
    return contract_id

def issued_amounts(db,contract_id,period=None):
    query='SELECT l.tenant_id,SUM(l.amount_cents) AS amount FROM issued_invoice_line l JOIN issued_invoice i ON i.invoice_id=l.invoice_id WHERE i.contract_id=?'
    params=[contract_id]
    if period is not None:
        query+=' AND i.period=?';params.append(period)
    rows=db.execute(query+' GROUP BY l.tenant_id',params)
    return {r['tenant_id']:int(r['amount']) for r in rows}

def find_bill(db,contract_id,method,snapshot_sha256):
    return db.execute('SELECT * FROM bill WHERE contract_id=? AND allocation_method=? AND snapshot_sha256=?',(contract_id,method,snapshot_sha256)).fetchone()

def next_revision(db,contract_id,method):
    row=db.execute('SELECT MAX(revision) AS revision FROM bill WHERE contract_id=? AND allocation_method=?',(contract_id,method)).fetchone()
    return int(row['revision'] or 0)+1

def active_bill(db,contract_id,method):
    return db.execute("SELECT * FROM bill WHERE contract_id=? AND allocation_method=? AND status='已出账' ORDER BY revision DESC LIMIT 1",(contract_id,method)).fetchone()

def active_entries(db,contract_id,method,tenant_id=None):
    query="""SELECT e.* FROM billing_entry e JOIN bill b ON b.id=e.bill_id
             WHERE e.contract_id=? AND e.allocation_method=? AND b.status='已出账'
               AND e.kind='adjustment' AND NOT EXISTS (
                   SELECT 1 FROM billing_entry r WHERE r.kind='reversal' AND r.reverses_entry_id=e.id)"""
    params=[contract_id,method]
    if tenant_id is not None:
        query+=' AND e.tenant_id=?';params.append(tenant_id)
    return db.execute(query+' ORDER BY e.id',params).fetchall()

def persist_bill(db,record):
    """Write one bill revision, its per-tenant lines and entries atomically.

    `record` is produced by `meterround.billing.plan`. Bill rows, issued invoices
    and every reading/source/evidence/decision row are append-only: a follow-up
    bill is a new revision that reverses the previous adjustment rather than
    editing it."""
    method=record['allocation_method'];contract=record['contract_id']
    with db:
        existing=db.execute('SELECT * FROM bill WHERE contract_id=? AND allocation_method=? AND snapshot_sha256=? ORDER BY revision DESC LIMIT 1',(contract,method,record['snapshot_sha256'])).fetchone()
        if existing is not None:
            return existing
        previous=active_bill(db,contract,method)
        revision=next_revision(db,contract,method)
        bill_id=db.execute("""INSERT INTO bill(contract_id,batch_id,allocation_method,revision,status,snapshot_sha256,contract_sha256,main_usage,main_cents,common_pool_cents,total_cents,detail,supersedes_bill_id)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (contract,record['batch_id'],method,revision,record['status'],record['snapshot_sha256'],
                            record.get('contract_sha256',''),record.get('main_usage'),record.get('main_cents'),
                            record.get('common_pool_cents'),record.get('total_cents'),
                            record['detail'] if isinstance(record['detail'],str) else _text(record['detail']),previous['id'] if previous else None)).lastrowid
        for line in record['lines']:
            db.execute('INSERT INTO bill_line(bill_id,tenant_id,slot,usage,direct_cents,pool_cents,amount_cents,issued_cents,true_up_cents) VALUES (?,?,?,?,?,?,?,?,?)',
                       (bill_id,line['tenant_id'],line['slot'],line['usage'],line['direct_cents'],line['pool_cents'],
                        line['amount_cents'],line['issued_cents'],line['true_up_cents']))
            if record['status']!='已出账':
                continue
            reversing=active_entries(db,contract,method,line['tenant_id'])
            for entry in reversing:
                db.execute("""INSERT INTO billing_entry(bill_id,contract_id,batch_id,allocation_method,tenant_id,kind,amount_cents,reverses_entry_id,decision_refs,source_sha256,evidence_sha256,refs)
                              VALUES (?,?,?,?,?,'reversal',?,?,?,?,?,?)""",
                           (bill_id,contract,record['batch_id'],method,line['tenant_id'],-int(entry['amount_cents']),entry['id'],
                            entry['decision_refs'],record['source_sha256'],record['evidence_sha256'],_text({'reverses_entry_id':entry['id'],'reverses_bill_id':entry['bill_id']})))
            if line['true_up_cents']!=0:
                refs={'contract_id':contract,'batch_id':record['batch_id'],'bill_id':bill_id,'revision':revision,
                      'allocation_method':method,'tenant_id':line['tenant_id'],'invoice_id':record.get('invoice_id'),
                      'issued_cents':line['issued_cents'],'bill_amount_cents':line['amount_cents'],
                      'reversed_entry_ids':[e['id'] for e in reversing]}
                db.execute("""INSERT INTO billing_entry(bill_id,contract_id,batch_id,allocation_method,tenant_id,kind,amount_cents,reverses_entry_id,decision_refs,source_sha256,evidence_sha256,refs)
                              VALUES (?,?,?,?,?,'adjustment',?,NULL,?,?,?,?)""",
                           (bill_id,contract,record['batch_id'],method,line['tenant_id'],line['true_up_cents'],
                            _text(record['decision_refs']),record['source_sha256'],record['evidence_sha256'],_text(refs)))
    return db.execute('SELECT * FROM bill WHERE id=?',(bill_id,)).fetchone()

def statement(db,contract_id,method=None):
    """Replayable ledger: issuance, reversals and the current net payable/refund."""
    methods=[method] if method else [r['method'] for r in db.execute('SELECT method FROM billing_contract_allocation WHERE contract_id=? ORDER BY method',(contract_id,))]
    out=[]
    for chosen in methods:
        rows=[{'tenant_id':r['tenant_id'],'amount_cents':int(r['amount_cents'])} for r in active_entries(db,contract_id,chosen)]
        net={}
        for row in rows:
            net[row['tenant_id']]=net.get(row['tenant_id'],0)+row['amount_cents']
        out.append({'contract_id':contract_id,'allocation_method':chosen,'net_cents':net,
                    'entries':[dict(r) for r in db.execute('SELECT * FROM billing_entry WHERE contract_id=? AND allocation_method=? ORDER BY id',(contract_id,chosen))]})
    return out[0] if method else out

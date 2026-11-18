"""Field battery waste ledger and the three physical endpoints.

``register`` binds each frozen pack to the handset reading evidence that
justified the field swap.  ``load`` / ``ship`` / ``process`` remain single-step
physical endpoints: they mutate real pack/container state inside one SQLite
transaction and persist the operation intent next to the result so a repeated
operation id or a crash between calls can be recovered from ``state``.

Campaign-level planning and idempotent execution live in this module; the
JSON hand-over report lives in :mod:`meterround.battery_report`.
"""

import hashlib
import json
from datetime import datetime, timezone
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from .store import connect

MAX_PACKS = 12
MAX_BOXES = 6
CONTRACTS = ('maximize_recovery', 'minimize_shipments')

# Isolations that are cleared automatically once the underlying source/ledger
# problem is repaired on a resumed campaign.  Endpoint refusals stay sticky.
REVERSIBLE_ISOLATIONS = {
    'battery_pack_source_missing',
    'battery_pack_source_changed',
    'battery_pack_frozen_mismatch',
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _number(value, code):
    try:
        number = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        raise ValueError(code) from None
    return number


def _validate_case(doc):
    if not isinstance(doc, dict) or not doc.get('packs') or not doc.get('containers'):
        raise ValueError('bad_battery_case')
    pack_ids = set()
    clean_packs = []
    for item in doc['packs']:
        try:
            pid = str(item['id'])
            device = str(item['device'])
            slot = str(item['slot'])
            digest = str(item['reading_source_sha256'])
            chemistry = str(item['chemistry'])
            mass = _number(item['mass_kg'], 'pack_mass_mismatch')
            metal = _number(item['metal_kg'], 'pack_mass_mismatch')
            fluid = _number(item['fluid_kg'], 'pack_mass_mismatch')
        except KeyError:
            raise ValueError('bad_battery_case') from None
        if not pid or pid in pack_ids:
            raise ValueError('bad_battery_case')
        if not device or not slot or not chemistry or not digest:
            raise ValueError('bad_battery_case')
        if mass != metal + fluid or mass <= 0 or metal < 0 or fluid < 0:
            raise ValueError('pack_mass_mismatch')
        pack_ids.add(pid)
        clean_packs.append({
            'id': pid, 'device': device, 'slot': slot,
            'reading_source_sha256': digest, 'chemistry': chemistry,
            'mass_kg': mass, 'metal_kg': metal, 'fluid_kg': fluid,
        })
    container_ids = set()
    clean_containers = []
    for item in doc['containers']:
        try:
            cid = str(item['id'])
            capacity = _number(item['capacity_kg'], 'bad_battery_case')
        except KeyError:
            raise ValueError('bad_battery_case') from None
        if not cid or cid in container_ids or capacity <= 0:
            raise ValueError('bad_battery_case')
        container_ids.add(cid)
        clean_containers.append({'id': cid, 'capacity_kg': capacity})
    contracts = doc.get('contracts')
    if contracts is not None:
        contracts = [str(x) for x in contracts]
        if any(x not in CONTRACTS for x in contracts) or not contracts:
            raise ValueError('bad_battery_case')
    return clean_packs, clean_containers, contracts


def _source_rows(db, pack):
    return [dict(row) for row in db.execute(
        '''SELECT r.id AS reading_id,r.seq,r.sha256 AS payload_sha256,
                  s.sha256 AS source_sha256,e.id AS evidence_id,
                  e.source_sha256 AS evidence_source_sha256,
                  e.payload_sha256 AS evidence_payload_sha256
           FROM reading r JOIN source s ON s.id=r.source_id
           LEFT JOIN evidence e ON e.reading_id=r.id
           WHERE r.device=? AND r.slot=? AND s.sha256=?
           ORDER BY r.seq''',
        (pack['device'], pack['slot'], pack['reading_source_sha256']),
    )]


def register(db_path, case_id, fixture):
    """Freeze a battery case and trace every pack to handset reading evidence.

    Re-registering the same file is idempotent.  A changed file digest, changed
    source digest, or changed frozen pack/container facts is refused instead of
    silently rewriting the evidence chain.
    """
    raw = Path(fixture).read_bytes()
    doc = json.loads(raw)
    packs, containers, contracts = _validate_case(doc)
    digest = hashlib.sha256(raw).hexdigest()
    payload = json.dumps(doc, sort_keys=True)
    payload_sha256 = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    db = connect(db_path)
    try:
        with db:
            old = db.execute('SELECT digest FROM battery_case WHERE id=?', (case_id,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('battery_case_conflict')
            for pack in packs:
                rows = _source_rows(db, pack)
                if not rows:
                    raise ValueError('battery_device_source_missing')
                if any(not row['evidence_id'] for row in rows):
                    raise ValueError('battery_evidence_missing')
                if any(row['evidence_source_sha256'] != pack['reading_source_sha256']
                       or row['evidence_payload_sha256'] != row['payload_sha256'] for row in rows):
                    raise ValueError('battery_evidence_conflict')
            db.execute(
                'INSERT OR IGNORE INTO battery_case(id,payload,digest,payload_sha256) VALUES (?,?,?,?)',
                (case_id, payload, digest, payload_sha256),
            )
            for pack in packs:
                old_pack = db.execute('SELECT * FROM battery_pack WHERE id=?', (pack['id'],)).fetchone()
                if old_pack:
                    same = (
                        old_pack['case_id'] == case_id
                        and old_pack['chemistry'] == pack['chemistry']
                        and Fraction(str(old_pack['mass_kg'])) == pack['mass_kg']
                        and Fraction(str(old_pack['metal_kg'])) == pack['metal_kg']
                        and Fraction(str(old_pack['fluid_kg'])) == pack['fluid_kg']
                    )
                    if not same:
                        raise ValueError('battery_case_conflict')
                else:
                    db.execute(
                        '''INSERT INTO battery_pack(
                               id,case_id,chemistry,mass_kg,metal_kg,fluid_kg,
                               container,location,disposition,device,slot,source_sha256,reading_id)
                           VALUES (?,?,?,?,?,?,NULL,'field','unprocessed',?,?,?,?)''',
                        (pack['id'], case_id, pack['chemistry'], float(pack['mass_kg']),
                         float(pack['metal_kg']), float(pack['fluid_kg']), pack['device'],
                         pack['slot'], pack['reading_source_sha256'],
                         _source_rows(db, pack)[0]['reading_id']),
                    )
                rows = _source_rows(db, pack)
                first = rows[0]
                db.execute(
                    '''INSERT INTO battery_pack_source(
                           pack_id,case_id,device,slot,source_sha256,first_reading_id,
                           first_reading_sha256,reading_count,verified_at)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(pack_id) DO UPDATE SET
                           case_id=excluded.case_id,device=excluded.device,slot=excluded.slot,
                           source_sha256=excluded.source_sha256,
                           first_reading_id=excluded.first_reading_id,
                           first_reading_sha256=excluded.first_reading_sha256,
                           reading_count=excluded.reading_count,
                           verified_at=excluded.verified_at''',
                    (pack['id'], case_id, pack['device'], pack['slot'],
                     pack['reading_source_sha256'], first['reading_id'],
                     first['payload_sha256'], len(rows), _now()),
                )
                db.execute('DELETE FROM battery_pack_evidence WHERE pack_id=?', (pack['id'],))
                for row in rows:
                    db.execute(
                        '''INSERT INTO battery_pack_evidence(
                               pack_id,reading_id,evidence_id,source_sha256,payload_sha256)
                           VALUES (?,?,?,?,?)''',
                        (pack['id'], row['reading_id'], row['evidence_id'],
                         row['source_sha256'], row['payload_sha256']),
                    )
                db.execute(
                    'UPDATE battery_pack SET device=?,slot=?,source_sha256=?,reading_id=? WHERE id=?',
                    (pack['device'], pack['slot'], pack['reading_source_sha256'],
                     first['reading_id'], pack['id']),
                )
            for item in containers:
                old_box = db.execute('SELECT * FROM battery_container WHERE id=?', (item['id'],)).fetchone()
                if old_box:
                    if (old_box['case_id'] != case_id
                            or Fraction(str(old_box['capacity_kg'])) != item['capacity_kg']):
                        raise ValueError('battery_case_conflict')
                    continue
                db.execute(
                    'INSERT INTO battery_container(id,case_id,capacity_kg,location) VALUES (?,?,?,?)',
                    (item['id'], case_id, float(item['capacity_kg']), 'field'),
                )
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest, 'packs': len(packs),
            'containers': len(containers), 'contracts': contracts}


def state(db_path, case_id):
    db = connect(db_path)
    try:
        packs = [dict(x) for x in db.execute(
            'SELECT * FROM battery_pack WHERE case_id=? ORDER BY id', (case_id,))]
        containers = [dict(x) for x in db.execute(
            'SELECT * FROM battery_container WHERE case_id=? ORDER BY id', (case_id,))]
        if not packs:
            raise ValueError('unknown_battery_case')
        sources = [dict(x) for x in db.execute(
            'SELECT * FROM battery_pack_source WHERE case_id=? ORDER BY pack_id', (case_id,))]
        links = {}
        for row in db.execute(
                '''SELECT e.pack_id,e.reading_id,e.evidence_id,e.source_sha256,e.payload_sha256
                   FROM battery_pack_evidence e
                   JOIN battery_pack_source s ON s.pack_id=e.pack_id
                   WHERE s.case_id=? ORDER BY e.pack_id,e.reading_id''', (case_id,)):
            links.setdefault(row['pack_id'], []).append(dict(row))
        for pack in packs:
            pack['source'] = next((s for s in sources if s['pack_id'] == pack['id']), None)
            if pack['source'] is not None:
                source_links = links.get(pack['id'], [])
                pack['source']['links'] = source_links
                pack['source']['reading_ids'] = [link['reading_id'] for link in source_links]
                pack['source']['evidence_ids'] = [link['evidence_id'] for link in source_links]
        return {'packs': packs, 'containers': containers}
    finally:
        db.close()


def _operation_matches(old, intent, kind):
    if old['kind'] != kind:
        return False
    stored = old['intent']
    if stored:
        try:
            if json.loads(stored) != intent:
                return False
        except ValueError:
            return False
        return True
    result = json.loads(old['result'])
    if kind == 'load':
        return result.get('pack') == intent.get('pack') and result.get('container') == intent.get('container')
    if kind == 'ship':
        return result.get('container') == intent.get('container')
    if kind == 'process':
        return result.get('container') == intent.get('container') and result.get('route') == intent.get('route')
    return False


def _replay(db, operation_id, case_id, kind, intent):
    old = db.execute('SELECT * FROM battery_operation WHERE id=?', (operation_id,)).fetchone()
    if not old:
        return None
    if old['case_id'] != case_id or not _operation_matches(old, intent, kind):
        raise ValueError('operation_id_conflict')
    return {'status': 'replayed', 'record': json.loads(old['result'])}


def _record(db, operation_id, case_id, kind, intent, result, campaign_id=None):
    # Intent and result commit in the same transaction as the physical state
    # change, so a crash can never leave a committed shipment without its
    # operation id.
    now = _now()
    db.execute(
        '''INSERT INTO battery_operation(
               id,case_id,kind,result,campaign_id,intent,status,started_at,finished_at)
           VALUES (?,?,?,?,?,?,'done',?,?)''',
        (operation_id, case_id, kind, json.dumps(result, sort_keys=True),
         campaign_id, json.dumps(intent, sort_keys=True), now, now),
    )
    return result


def _active_pack(db, case_id, pack_id):
    return db.execute(
        'SELECT * FROM battery_pack WHERE id=? AND case_id=?', (pack_id, case_id)).fetchone()


def _active_container(db, case_id, container_id):
    return db.execute(
        'SELECT * FROM battery_container WHERE id=? AND case_id=?', (container_id, case_id)).fetchone()


def load(db_path, case_id, pack_id, container_id, operation_id, campaign_id=None):
    intent = {'pack': pack_id, 'container': container_id}
    db = connect(db_path)
    try:
        with db:
            prior = _replay(db, operation_id, case_id, 'load', intent)
            if prior:
                return prior
            pack = _active_pack(db, case_id, pack_id)
            box = _active_container(db, case_id, container_id)
            if not pack or not box:
                raise ValueError('unavailable_pack_or_container')
            if pack['isolated_reason'] or box['isolation_reason']:
                raise ValueError('battery_unit_isolated')
            if (pack['location'] != 'field' or pack['container'] or box['location'] != 'field'
                    or pack['disposition'] != 'unprocessed'):
                raise ValueError('unavailable_pack_or_container')
            members = db.execute(
                'SELECT mass_kg FROM battery_pack WHERE container=?', (container_id,)).fetchall()
            used = sum((Fraction(str(row['mass_kg'])) for row in members), Fraction(0))
            if used + Fraction(str(pack['mass_kg'])) > Fraction(str(box['capacity_kg'])):
                raise ValueError('container_capacity_exceeded')
            db.execute('UPDATE battery_pack SET container=? WHERE id=?', (container_id, pack_id))
            db.execute('UPDATE battery_container SET last_operation_id=? WHERE id=?',
                       (operation_id, container_id))
            result = {'status': 'loaded', 'pack': pack_id, 'container': container_id,
                      'mass_kg': pack['mass_kg']}
            return _record(db, operation_id, case_id, 'load', intent, result, campaign_id)
    finally:
        db.close()


def ship(db_path, case_id, container_id, operation_id, campaign_id=None):
    intent = {'container': container_id}
    db = connect(db_path)
    try:
        with db:
            prior = _replay(db, operation_id, case_id, 'ship', intent)
            if prior:
                return prior
            box = _active_container(db, case_id, container_id)
            packs = [dict(x) for x in db.execute(
                'SELECT * FROM battery_pack WHERE container=? AND case_id=? ORDER BY id',
                (container_id, case_id))]
            if (not box or box['location'] != 'field' or not packs
                    or box['isolation_reason']
                    or any(p['location'] != 'field' or p['disposition'] != 'unprocessed'
                           or p['isolated_reason'] for p in packs)):
                raise ValueError('container_not_shippable')
            mass = sum((Fraction(str(p['mass_kg'])) for p in packs), Fraction(0))
            if mass > Fraction(str(box['capacity_kg'])):
                raise ValueError('container_capacity_exceeded')
            db.execute("UPDATE battery_container SET location='facility',shipped_at=?,last_operation_id=? WHERE id=?",
                       (_now(), operation_id, container_id))
            db.execute("UPDATE battery_pack SET location='facility' WHERE container=?", (container_id,))
            result = {'status': 'shipped', 'container': container_id,
                      'packs': [p['id'] for p in packs], 'mass_kg': float(mass)}
            return _record(db, operation_id, case_id, 'ship', intent, result, campaign_id)
    finally:
        db.close()


def process(db_path, case_id, container_id, route, operation_id, campaign_id=None):
    if route not in ('recover', 'thermal'):
        raise ValueError('bad_treatment_route')
    intent = {'container': container_id, 'route': route}
    db = connect(db_path)
    try:
        with db:
            prior = _replay(db, operation_id, case_id, 'process', intent)
            if prior:
                return prior
            box = _active_container(db, case_id, container_id)
            packs = [dict(x) for x in db.execute(
                'SELECT * FROM battery_pack WHERE container=? AND case_id=? ORDER BY id',
                (container_id, case_id))]
            if (not box or box['location'] != 'facility' or not packs
                    or box['isolation_reason'] or box['processed_at']
                    or any(p['disposition'] != 'unprocessed' or p['location'] != 'facility'
                           or p['isolated_reason'] for p in packs)):
                raise ValueError('container_not_processable')
            if route == 'recover' and len({p['chemistry'] for p in packs}) != 1:
                raise ValueError('mixed_chemistry_cannot_recover')
            recovered = sum(Fraction(str(p['metal_kg'])) for p in packs) if route == 'recover' else Fraction(0)
            neutralized = (sum(Fraction(str(p['fluid_kg'])) for p in packs) if route == 'recover'
                           else sum(Fraction(str(p['mass_kg'])) for p in packs))
            mass = sum(Fraction(str(p['mass_kg'])) for p in packs)
            for pack in packs:
                db.execute('UPDATE battery_pack SET disposition=? WHERE id=?', (route, pack['id']))
                recovered_pack = Fraction(str(pack['metal_kg'])) if route == 'recover' else Fraction(0)
                neutralized_pack = (Fraction(str(pack['fluid_kg'])) if route == 'recover'
                                    else Fraction(str(pack['mass_kg'])))
                db.execute(
                    '''INSERT OR REPLACE INTO battery_pack_result(
                           pack_id,case_id,campaign_id,container_id,route,
                           recovered_kg,neutralized_kg,operation_id,processed_at)
                       VALUES (?,?,?,?,?,?,?,?,?)''',
                    (pack['id'], case_id, campaign_id, container_id, route,
                     float(recovered_pack), float(neutralized_pack), operation_id, _now()),
                )
            db.execute(
                '''INSERT OR REPLACE INTO battery_container_result(
                       container_id,case_id,campaign_id,route,mass_kg,recovered_kg,
                       neutralized_kg,pack_count,operation_id,processed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (container_id, case_id, campaign_id, route, float(mass), float(recovered),
                 float(neutralized), len(packs), operation_id, _now()),
            )
            db.execute(
                'UPDATE battery_container SET processed_at=?,route=?,last_operation_id=? WHERE id=?',
                (_now(), route, operation_id, container_id),
            )
            result = {
                'status': 'processed', 'container': container_id, 'route': route,
                'packs': [p['id'] for p in packs], 'mass_kg': float(mass),
                'recovered_kg': float(recovered), 'neutralized_kg': float(neutralized),
                'chemistry': sorted({p['chemistry'] for p in packs}),
                'per_pack': {
                    p['id']: {
                        'recovered_kg': float(Fraction(str(p['metal_kg'])) if route == 'recover' else 0),
                        'neutralized_kg': float(Fraction(str(p['fluid_kg'])) if route == 'recover'
                                                else Fraction(str(p['mass_kg']))),
                    } for p in packs
                },
            }
            return _record(db, operation_id, case_id, 'process', intent, result, campaign_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Exact campaign planning
# ---------------------------------------------------------------------------

def _fraction(value):
    return Fraction(str(value))


def _number_out(value):
    value = Fraction(value)
    if value.denominator == 1:
        return int(value)
    return float(value)


def _iso(db, case_id, scope, subject, reason, operation_id=None):
    db.execute(
        '''INSERT OR IGNORE INTO battery_isolation(
               case_id,scope,subject,reason,operation_id,created_at)
           VALUES (?,?,?,?,?,?)''',
        (case_id, scope, subject, reason, operation_id, _now()),
    )


def _frozen_case(db, case_id):
    row = db.execute('SELECT * FROM battery_case WHERE id=?', (case_id,)).fetchone()
    if not row:
        raise ValueError('unknown_battery_case')
    payload = row['payload']
    if row['payload_sha256'] and hashlib.sha256(payload.encode('utf-8')).hexdigest() != row['payload_sha256']:
        raise ValueError('battery_case_tampered')
    doc = json.loads(payload)
    packs, containers, contracts = _validate_case(doc)
    return doc, packs, containers, contracts, row


def _check_source_row(db, pack, row):
    """Return a reason when one reading/evidence link no longer matches."""
    source = db.execute('SELECT raw,sha256 FROM source WHERE sha256=?', (pack['reading_source_sha256'],)).fetchone()
    if not source:
        return 'battery_pack_source_changed'
    if hashlib.sha256(source['raw']).hexdigest() != pack['reading_source_sha256']:
        return 'battery_pack_source_changed'
    if row['source_sha256'] != pack['reading_source_sha256']:
        return 'battery_pack_source_changed'
    if row['evidence_source_sha256'] != pack['reading_source_sha256']:
        return 'battery_pack_source_changed'
    if row['evidence_payload_sha256'] != row['payload_sha256']:
        return 'battery_pack_source_changed'
    return None


def _verify_pack_evidence(db, local, frozen):
    """Verify the whole pack -> reading -> evidence -> source chain.

    Returns ``None`` when sound, otherwise a stable isolation reason.
    """
    source = db.execute(
        'SELECT * FROM battery_pack_source WHERE pack_id=? AND case_id=?',
        (frozen['id'], local['case_id']),
    ).fetchone()
    if not source:
        return 'battery_pack_source_missing'
    if source['source_sha256'] != frozen['reading_source_sha256']:
        return 'battery_pack_source_changed'
    if local['source_sha256'] and local['source_sha256'] != frozen['reading_source_sha256']:
        return 'battery_pack_source_changed'
    source_row = db.execute('SELECT raw FROM source WHERE sha256=?', (frozen['reading_source_sha256'],)).fetchone()
    if not source_row or hashlib.sha256(source_row['raw']).hexdigest() != frozen['reading_source_sha256']:
        return 'battery_pack_source_changed'
    rows = [dict(r) for r in db.execute(
        '''SELECT e.reading_id,e.source_sha256 AS evidence_source_sha256,
                  e.payload_sha256 AS evidence_payload_sha256,
                  e.source_sha256 AS source_sha256,
                  r.sha256 AS payload_sha256,r.raw AS reading_raw
           FROM battery_pack_evidence e
           JOIN reading r ON r.id=e.reading_id
           JOIN evidence ev ON ev.id=e.evidence_id
           WHERE e.pack_id=?
           ORDER BY e.reading_id''',
        (frozen['id'],),
    )]
    if not rows or len(rows) != source['reading_count']:
        return 'battery_pack_source_changed'
    first_id = None
    for row in rows:
        if _check_source_row(db, frozen, row):
            return 'battery_pack_source_changed'
        if hashlib.sha256(row['reading_raw'].encode('utf-8')).hexdigest() != row['payload_sha256']:
            return 'battery_pack_source_changed'
        if first_id is None:
            first_id = row['reading_id']
    if source['first_reading_id'] != first_id or source['first_reading_sha256'] != rows[0]['payload_sha256']:
        return 'battery_pack_source_changed'
    # New readings for the same device/slot/source would mean the frozen source
    # no longer describes the handset hand-off.
    now = db.execute(
        '''SELECT r.id FROM reading r JOIN source s ON s.id=r.source_id
           JOIN evidence e ON e.reading_id=r.id
           WHERE r.device=? AND r.slot=? AND s.sha256=?''',
        (frozen['device'], frozen['slot'], frozen['reading_source_sha256']),
    ).fetchall()
    if len(now) != len(rows):
        return 'battery_pack_source_changed'
    return None


def _same_pack(local, frozen, case_id):
    return all((
        local['case_id'] == case_id,
        local['chemistry'] == frozen['chemistry'],
        _fraction(local['mass_kg']) == frozen['mass_kg'],
        _fraction(local['metal_kg']) == frozen['metal_kg'],
        _fraction(local['fluid_kg']) == frozen['fluid_kg'],
    ))


def _plan_context(db, case_id):
    """Reconcile the frozen case against local reality.

    Returns candidate packs/containers, isolation records and exclusions.  A
    broken pack/container is never silently replaced; it is removed from the
    executable set with a stable reason.
    """
    doc, frozen_packs, frozen_containers, contracts, case_row = _frozen_case(db, case_id)
    if len(frozen_packs) > MAX_PACKS:
        return {'doc': doc, 'packs': [], 'containers': [], 'isolated': [],
                'excluded': [{'scope': 'case', 'subject': case_id,
                              'reason': 'battery_pack_limit_exceeded'}],
                'infeasible': 'battery_pack_limit_exceeded', 'case': case_row}
    isolated = []
    excluded = []
    candidates = []
    for frozen in frozen_packs:
        local = db.execute('SELECT * FROM battery_pack WHERE id=?', (frozen['id'],)).fetchone()
        if not local:
            reason = 'battery_pack_missing'
            _iso(db, case_id, 'pack', frozen['id'], reason)
            isolated.append({'scope': 'pack', 'subject': frozen['id'], 'reason': reason})
            continue
        if local['case_id'] != case_id:
            reason = 'battery_pack_not_in_frozen_case'
            _iso(db, case_id, 'pack', frozen['id'], reason)
            isolated.append({'scope': 'pack', 'subject': frozen['id'], 'reason': reason})
            continue
        if not _same_pack(local, frozen, case_id):
            reason = 'battery_pack_frozen_mismatch'
            db.execute('UPDATE battery_pack SET isolated_reason=? WHERE id=?', (reason, frozen['id']))
            _iso(db, case_id, 'pack', frozen['id'], reason)
            isolated.append({'scope': 'pack', 'subject': frozen['id'], 'reason': reason})
            continue
        reason = _verify_pack_evidence(db, local, frozen)
        if not reason and local['isolated_reason'] in REVERSIBLE_ISOLATIONS:
            db.execute('UPDATE battery_pack SET isolated_reason=NULL WHERE id=?', (frozen['id'],))
            local = db.execute('SELECT * FROM battery_pack WHERE id=?', (frozen['id'],)).fetchone()
        if reason:
            db.execute('UPDATE battery_pack SET isolated_reason=? WHERE id=?', (reason, frozen['id']))
            _iso(db, case_id, 'pack', frozen['id'], reason)
            isolated.append({'scope': 'pack', 'subject': frozen['id'], 'reason': reason})
            continue
        if local['isolated_reason']:
            excluded.append({'scope': 'pack', 'subject': frozen['id'],
                             'reason': local['isolated_reason']})
            continue
        if local['disposition'] != 'unprocessed':
            excluded.append({'scope': 'pack', 'subject': frozen['id'],
                             'reason': 'battery_pack_already_processed'})
            continue
        if local['location'] != 'field' or local['container']:
            excluded.append({'scope': 'pack', 'subject': frozen['id'],
                             'reason': 'battery_pack_not_at_field'})
            continue
        candidates.append(dict(frozen, local=dict(local)))
    usable = []
    for frozen in frozen_containers:
        local = db.execute('SELECT * FROM battery_container WHERE id=?', (frozen['id'],)).fetchone()
        if not local or local['case_id'] != case_id:
            reason = 'battery_container_missing'
            if local:
                db.execute('UPDATE battery_container SET isolation_reason=? WHERE id=?', (reason, frozen['id']))
            _iso(db, case_id, 'container', frozen['id'], reason)
            isolated.append({'scope': 'container', 'subject': frozen['id'], 'reason': reason})
            continue
        if _fraction(local['capacity_kg']) != frozen['capacity_kg']:
            reason = 'battery_container_capacity_changed'
            db.execute('UPDATE battery_container SET isolation_reason=? WHERE id=?', (reason, frozen['id']))
            _iso(db, case_id, 'container', frozen['id'], reason)
            isolated.append({'scope': 'container', 'subject': frozen['id'], 'reason': reason})
            continue
        if local['isolation_reason'] == 'battery_container_capacity_changed':
            db.execute('UPDATE battery_container SET isolation_reason=NULL WHERE id=?', (frozen['id'],))
            local = db.execute('SELECT * FROM battery_container WHERE id=?', (frozen['id'],)).fetchone()
        if local['isolation_reason']:
            # An isolated box stays isolated until an operator clears the flag
            # explicitly; packs inside it are never silently rerouted.
            excluded.append({'scope': 'container', 'subject': frozen['id'],
                             'reason': local['isolation_reason']})
            continue
        used = db.execute(
            'SELECT COALESCE(SUM(mass_kg),0) FROM battery_pack WHERE container=?',
            (frozen['id'],)).fetchone()[0]
        if used:
            excluded.append({'scope': 'container', 'subject': frozen['id'],
                             'reason': 'battery_container_not_available'})
            continue
        if local['processed_at'] or local['location'] != 'field':
            excluded.append({'scope': 'container', 'subject': frozen['id'],
                             'reason': 'battery_container_not_available'})
            continue
        usable.append(dict(frozen))
    # Deterministic capacity order keeps the DP's tie-breaking reproducible.
    usable.sort(key=lambda x: (-x['capacity_kg'], x['id']))
    if len(usable) > MAX_BOXES:
        excluded.extend({'scope': 'container', 'subject': c['id'], 'reason': 'battery_box_limit'}
                        for c in usable[MAX_BOXES:])
        usable = usable[:MAX_BOXES]
    return {'doc': doc, 'packs': candidates, 'containers': usable,
            'isolated': isolated, 'excluded': excluded, 'infeasible': None,
            'contracts': contracts, 'case': case_row}


def _better(candidate, best, contract):
    if best is None:
        return True
    recovered, boxes = candidate[0], candidate[1]
    best_recovered, best_boxes = best[0], best[1]
    if contract == 'maximize_recovery':
        if recovered != best_recovered:
            return recovered > best_recovered
        return boxes < best_boxes
    if boxes != best_boxes:
        return boxes < best_boxes
    return recovered > best_recovered


def _exact_solve(packs, containers, contract):
    """Exhaustive subset DP over every pack-to-box assignment.

    For at most 12 packs and 6 containers this enumerates all assignments; it
    is not a greedy or LP relaxation.  A mixed-chemistry box can only be
    thermally treated, so its metal contributes zero recovered kilograms.
    """
    n = len(packs)
    size = 1 << n
    mass = [Fraction(0)] * size
    metal = [Fraction(0)] * size
    chemistry = [0] * size
    for mask in range(1, size):
        bit = mask & -mask
        index = bit.bit_length() - 1
        rest = mask ^ bit
        mass[mask] = mass[rest] + packs[index]['mass_kg']
        metal[mask] = metal[rest] + packs[index]['metal_kg']
        chemistry[mask] = chemistry[rest] | (1 << packs[index]['chem_index'])
    single = [chemistry[mask].bit_count() <= 1 for mask in range(size)]
    caps = [c['capacity_kg'] for c in containers]
    stats = {'states': 0, 'transitions': 0}

    @lru_cache(maxsize=None)
    def best_for(mask, box_index):
        stats['states'] += 1
        if mask == 0:
            return (Fraction(0), 0, ())
        if box_index >= len(caps):
            return None
        # A box may stay unused (fewer shipments), or receive any non-empty
        # subset that respects its exact capacity.
        best = best_for(mask, box_index + 1)
        subset = mask
        while subset:
            stats['transitions'] += 1
            if mass[subset] <= caps[box_index]:
                rest = best_for(mask ^ subset, box_index + 1)
                if rest is not None:
                    recovered = metal[subset] if single[subset] else Fraction(0)
                    candidate = (recovered + rest[0], 1 + rest[1],
                                 ((subset, box_index),) + rest[2])
                    if _better(candidate, best, contract):
                        best = candidate
            subset = (subset - 1) & mask
        return best

    result = best_for(size - 1, 0)
    return result, stats


def _plan_payload(case_id, contract, context, groups, predicted, optimality):
    boxes = []
    for box_no, (subset, box_index) in enumerate(
            sorted(groups, key=lambda item: (context['containers'][item[1]]['id'], min(
                i + 1 for i in range(len(context['packs'])) if item[0] >> i & 1))), 1):
        members = [context['packs'][i] for i in range(len(context['packs'])) if subset >> i & 1]
        members.sort(key=lambda p: p['id'])
        container = context['containers'][box_index]
        chemistries = sorted({p['chemistry'] for p in members})
        route = 'recover' if len(chemistries) == 1 else 'thermal'
        box_mass = sum((p['mass_kg'] for p in members), Fraction(0))
        box_metal = sum((p['metal_kg'] for p in members), Fraction(0))
        box_fluid = sum((p['fluid_kg'] for p in members), Fraction(0))
        boxes.append({
            'box_no': box_no,
            'container_id': container['id'],
            'capacity_kg': _number_out(container['capacity_kg']),
            'pack_ids': [p['id'] for p in members],
            'mass_kg': _number_out(box_mass),
            'metal_kg': _number_out(box_metal),
            'fluid_kg': _number_out(box_fluid),
            'chemistry': chemistries,
            'mixed_chemistry': len(chemistries) > 1,
            'route': route,
            'predicted_recovered_kg': _number_out(box_metal if route == 'recover' else Fraction(0)),
            'predicted_neutralized_kg': _number_out(box_fluid if route == 'recover' else box_mass),
            'full': box_mass == container['capacity_kg'],
        })
    payload = {
        'case_id': case_id,
        'contract': contract,
        'boxes': boxes,
        'pack_ids': [p['id'] for p in context['packs']],
        'predicted': predicted,
        'optimality': optimality,
        'isolated': context['isolated'],
        'excluded': context['excluded'],
        'frozen_payload_sha256': context['case']['payload_sha256'],
    }
    return payload


def _persist_plan(db, plan):
    existing = db.execute('SELECT * FROM battery_plan WHERE plan_id=?', (plan['plan_id'],)).fetchone()
    if existing:
        if existing['digest'] != plan['digest']:
            raise ValueError('battery_plan_conflict')
        return plan
    db.execute(
        '''INSERT INTO battery_plan(plan_id,case_id,contract,digest,payload,created_at,status)
           VALUES (?,?,?,?,?,?,?)''',
        (plan['plan_id'], plan['case_id'], plan['contract'], plan['digest'],
         json.dumps(plan, sort_keys=True), _now(), plan.get('status', 'planned')),
    )
    for box in plan.get('boxes', []):
        db.execute(
            '''INSERT INTO battery_plan_box(
                   plan_id,box_no,container_id,capacity_kg,pack_ids,mass_kg,
                   metal_kg,fluid_kg,chemistry,route)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (plan['plan_id'], box['box_no'], box['container_id'], box['capacity_kg'],
             json.dumps(box['pack_ids']), box['mass_kg'], box['metal_kg'],
             box['fluid_kg'], json.dumps(box['chemistry']), box['route']),
        )
    return plan


def plan_campaign(db_path, case_id, contract, plan_id=None):
    """Plan the frozen battery case under one authorised contract.

    ``maximize_recovery`` orders objectives as (recovered metal kg, -boxes);
    ``minimize_shipments`` orders them as (-boxes, recovered metal kg).  The
    returned plan is executable as-is and is persisted with its digest.
    """
    if contract not in CONTRACTS:
        raise ValueError('bad_battery_contract')
    db = connect(db_path)
    try:
        with db:
            context = _plan_context(db, case_id)
            authorized = context.get('contracts')
            if authorized and contract not in authorized:
                raise ValueError('battery_contract_not_authorized')
            base = {
                'case_id': case_id,
                'contract': contract,
                'boxes': [],
                'pack_ids': [],
                'predicted': {'recovered_kg': 0, 'neutralized_kg': 0, 'mass_kg': 0,
                              'shipments': 0},
                'optimality': {
                    'contract': contract,
                    'method': 'exhaustive_subset_dp',
                    'envelope': {'max_packs': MAX_PACKS, 'max_boxes': MAX_BOXES},
                    'candidate_packs': len(context['packs']),
                    'candidate_containers': len(context['containers']),
                },
                'isolated': context['isolated'],
                'excluded': context['excluded'],
                'frozen_payload_sha256': context['case']['payload_sha256'],
                'status': 'infeasible',
            }
            if context['infeasible']:
                base['reason'] = context['infeasible']
                base['digest'] = hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()
                base['plan_id'] = plan_id or f"plan:{case_id}:{contract}:{base['digest'][:16]}"
                return _persist_plan(db, base)
            if not context['packs']:
                base['status'] = 'empty'
                base['reason'] = ('battery_case_already_handled' if context['excluded']
                                  else ('battery_all_packs_isolated'
                                        if context['isolated'] else 'battery_no_packs'))
                base['digest'] = hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()
                base['plan_id'] = plan_id or f"plan:{case_id}:{contract}:{base['digest'][:16]}"
                return _persist_plan(db, base)
            if not context['containers']:
                base['reason'] = 'battery_no_usable_container'
                base['digest'] = hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()
                base['plan_id'] = plan_id or f"plan:{case_id}:{contract}:{base['digest'][:16]}"
                return _persist_plan(db, base)
            for index, pack in enumerate(context['packs']):
                pack['chem_index'] = sorted({p['chemistry'] for p in context['packs']}).index(pack['chemistry'])
            chosen, stats = _exact_solve(context['packs'], context['containers'], contract)
            # Solve both contracts on the same candidate set so the report can
            # show why the chosen contract selects its physical outcome.
            comparison = {}
            for other in CONTRACTS:
                other_result, _ = _exact_solve(context['packs'], context['containers'], other)
                if other_result is None:
                    comparison[other] = None
                else:
                    comparison[other] = {
                        'recovered_kg': _number_out(other_result[0]),
                        'shipments': other_result[1],
                    }
            if chosen is None:
                base['reason'] = 'battery_no_feasible_box_plan'
                base['optimality']['contract_comparison'] = comparison
                base['optimality']['dp_states'] = stats['states']
                base['optimality']['transitions'] = stats['transitions']
                base['digest'] = hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()
                base['plan_id'] = plan_id or f"plan:{case_id}:{contract}:{base['digest'][:16]}"
                return _persist_plan(db, base)
            recovered, shipments, groups = chosen
            total_mass = sum((p['mass_kg'] for p in context['packs']), Fraction(0))
            # Neutralised mass is fluid for recovery boxes and whole mass for
            # thermal boxes; mixed boxes never contribute recovered metal.
            neutralized = Fraction(0)
            for subset, box_index in groups:
                members = [context['packs'][i] for i in range(len(context['packs'])) if subset >> i & 1]
                if len({p['chemistry'] for p in members}) == 1:
                    neutralized += sum((p['fluid_kg'] for p in members), Fraction(0))
                else:
                    neutralized += sum((p['mass_kg'] for p in members), Fraction(0))
            predicted = {
                'recovered_kg': _number_out(recovered),
                'neutralized_kg': _number_out(neutralized),
                'mass_kg': _number_out(total_mass),
                'shipments': shipments,
            }
            optimality = {
                'contract': contract,
                'method': 'exhaustive_subset_dp',
                'objective_order': ('recovered_metal_kg_then_min_boxes'
                                    if contract == 'maximize_recovery'
                                    else 'min_boxes_then_recovered_metal_kg'),
                'envelope': {'max_packs': MAX_PACKS, 'max_boxes': MAX_BOXES},
                'candidate_packs': len(context['packs']),
                'candidate_containers': len(context['containers']),
                'dp_states': stats['states'],
                'transitions': stats['transitions'],
                'contract_comparison': comparison,
                'exactness': ('all non-empty pack subsets were considered for every '
                              'available container in capacity order'),
                'chosen': {'recovered_kg': _number_out(recovered), 'shipments': shipments},
            }
            payload = _plan_payload(case_id, contract, context, groups, predicted, optimality)
            payload['status'] = 'planned'
            payload['digest'] = hashlib.sha256(
                json.dumps({k: v for k, v in payload.items() if k not in ('digest', 'plan_id')},
                           sort_keys=True).encode()).hexdigest()
            payload['plan_id'] = plan_id or f"plan:{case_id}:{contract}:{payload['digest'][:16]}"
            return _persist_plan(db, payload)
    finally:
        db.close()




# ---------------------------------------------------------------------------
# Idempotent campaign execution
# ---------------------------------------------------------------------------

class CampaignInterrupted(RuntimeError):
    """Raised by the crash-injection hook after a physical step committed."""

    def __init__(self, operation_id, kind):
        super().__init__(f'campaign interrupted after {kind}:{operation_id}')
        self.operation_id = operation_id
        self.kind = kind


def _campaign(db_path, campaign_id):
    db = connect(db_path)
    try:
        row = db.execute('SELECT * FROM battery_campaign WHERE campaign_id=?',
                         (campaign_id,)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def _load_plan(db, plan_id):
    row = db.execute('SELECT * FROM battery_plan WHERE plan_id=?', (plan_id,)).fetchone()
    if not row:
        raise ValueError('unknown_battery_plan')
    return json.loads(row['payload'])


def _plan_steps(plan):
    steps = []
    for box in plan.get('boxes', []):
        for pack_id in box['pack_ids']:
            steps.append({'kind': 'load', 'box_no': box['box_no'],
                          'container_id': box['container_id'], 'pack_id': pack_id,
                          'route': None})
        steps.append({'kind': 'ship', 'box_no': box['box_no'],
                      'container_id': box['container_id'], 'pack_id': None,
                      'route': None})
        steps.append({'kind': 'process', 'box_no': box['box_no'],
                      'container_id': box['container_id'], 'pack_id': None,
                      'route': box['route']})
    return steps


def _step_id(campaign_id, plan_digest, step):
    suffix = step['pack_id'] or step['route'] or 'box'
    return f"{campaign_id}:{plan_digest[:16]}:b{step['box_no']}:{step['kind']}:{suffix}"


def _intent_for(step, case_id):
    intent = {'case_id': case_id, 'container': step['container_id']}
    if step['kind'] == 'load':
        intent['pack'] = step['pack_id']
    if step['kind'] == 'process':
        intent['route'] = step['route']
    return intent


def _persist_step(db, operation_id, campaign_id, case_id, plan_id, step, status,
                  result=None, error=None):
    now = _now()
    row = db.execute('SELECT * FROM battery_step WHERE operation_id=?', (operation_id,)).fetchone()
    if row:
        db.execute(
            '''UPDATE battery_step SET status=?,result=COALESCE(?,result),error=?,
                   attempts=attempts+1,updated_at=? WHERE operation_id=?''',
            (status, json.dumps(result, sort_keys=True) if result is not None else None,
             error, now, operation_id),
        )
        return
    db.execute(
        '''INSERT INTO battery_step(
               operation_id,campaign_id,case_id,plan_id,box_no,kind,container_id,
               pack_id,route,intent,status,result,error,attempts,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)''',
        (operation_id, campaign_id, case_id, plan_id, step['box_no'], step['kind'],
         step['container_id'], step['pack_id'], step['route'],
         json.dumps(_intent_for(step, case_id), sort_keys=True), status,
         json.dumps(result, sort_keys=True) if result is not None else None,
         error, now, now),
    )


def _set_campaign(db_path, campaign_id, **fields):
    if not fields:
        return
    columns = ','.join(f'{key}=?' for key in fields)
    db = connect(db_path)
    try:
        with db:
            db.execute(f'UPDATE battery_campaign SET {columns},updated_at=? WHERE campaign_id=?',
                       (*fields.values(), _now(), campaign_id))
    finally:
        db.close()


def _call_endpoint(db_path, case_id, step, operation_id, campaign_id):
    if step['kind'] == 'load':
        return load(db_path, case_id, step['pack_id'], step['container_id'],
                    operation_id, campaign_id)
    if step['kind'] == 'ship':
        return ship(db_path, case_id, step['container_id'], operation_id, campaign_id)
    return process(db_path, case_id, step['container_id'], step['route'],
                   operation_id, campaign_id)


def _isolate_failure(db_path, case_id, step, code, operation_id):
    """Quarantine the affected physical unit after an endpoint refusal.

    A pack that never entered a box is quarantined as a pack; anything that
    already occupies or is being shipped in a box quarantines the whole box and
    every pack physically inside it.
    """
    db = connect(db_path)
    try:
        with db:
            container = db.execute(
                'SELECT * FROM battery_container WHERE id=? AND case_id=?',
                (step['container_id'], case_id)).fetchone()
            pack = None
            if step['pack_id']:
                pack = db.execute('SELECT * FROM battery_pack WHERE id=?',
                                  (step['pack_id'],)).fetchone()
            box_members = [row['id'] for row in db.execute(
                'SELECT id FROM battery_pack WHERE container=?', (step['container_id'],))]
            pack_free = (pack is not None and pack['container'] is None
                         and pack['location'] == 'field'
                         and pack['disposition'] == 'unprocessed')
            if step['kind'] == 'load' and pack_free and not box_members:
                # Nothing entered the box: quarantine the pack and leave the box
                # available for a replanned remainder.
                reason = f'load_refused:{code}'
                db.execute('UPDATE battery_pack SET isolated_reason=? WHERE id=?',
                           (reason, step['pack_id']))
                _iso(db, case_id, 'pack', step['pack_id'], reason, operation_id)
            else:
                subject = step['container_id']
                if container:
                    db.execute('UPDATE battery_container SET isolation_reason=? WHERE id=?',
                               (code, subject))
                _iso(db, case_id, 'container', subject, code, operation_id)
                for member in db.execute('SELECT id FROM battery_pack WHERE container=?',
                                         (subject,)):
                    db.execute('UPDATE battery_pack SET isolated_reason=? WHERE id=?',
                               (f'container_isolated:{code}', member['id']))
                    _iso(db, case_id, 'pack', member['id'],
                         f'container_isolated:{code}', operation_id)
    finally:
        db.close()


def _new_plan(db_path, campaign_id, case_id, contract):
    db = connect(db_path)
    try:
        seq = db.execute('SELECT COUNT(*) FROM battery_campaign_plan WHERE campaign_id=?',
                         (campaign_id,)).fetchone()[0] + 1
    finally:
        db.close()
    plan = plan_campaign(db_path, case_id, contract,
                         plan_id=f'{campaign_id}:plan:{seq}')
    db = connect(db_path)
    try:
        with db:
            db.execute('UPDATE battery_campaign SET plan_id=? WHERE campaign_id=?',
                       (plan['plan_id'], campaign_id))
            db.execute('INSERT OR IGNORE INTO battery_campaign_plan(campaign_id,plan_id,seq) '
                       'VALUES (?,?,?)', (campaign_id, plan['plan_id'], seq))
    finally:
        db.close()
    return plan


def _uncovered_packs(db_path, case_id, plan):
    """Field packs not present in the stored plan.

    A committed crash leaves packs in boxes or at the facility; they are still
    covered by the plan's remaining operation ids and must not trigger a fresh
    plan.  Only packs that no plan step mentions do.
    """
    db = connect(db_path)
    try:
        rows = [row['id'] for row in db.execute(
            """SELECT id FROM battery_pack
               WHERE case_id=? AND location='field' AND container IS NULL
                 AND disposition='unprocessed' AND isolated_reason IS NULL
               ORDER BY id""", (case_id,))]
    finally:
        db.close()
    covered = set()
    if plan:
        for box in plan.get('boxes', []):
            covered.update(box['pack_ids'])
    return [pack_id for pack_id in rows if pack_id not in covered]


def _unaccounted(db_path, case_id):
    db = connect(db_path)
    try:
        return db.execute(
            """SELECT count(*) FROM battery_pack
               WHERE case_id=? AND disposition='unprocessed' AND isolated_reason IS NULL""",
            (case_id,)).fetchone()[0]
    finally:
        db.close()


def execute_campaign(db_path, case_id, contract, campaign_id=None, plan_id=None,
                     stop_after=None, crash_after=None):
    """Execute one authorised contract, resuming from committed state.

    ``stop_after`` / ``crash_after`` accept ``boxN:kind`` and raise
    :class:`CampaignInterrupted` immediately after that step committed, so a
    caller can prove that restart resumes from persisted operations rather than
    repeating a shipment or a treatment.
    """
    if contract not in CONTRACTS:
        raise ValueError('bad_battery_contract')
    campaign_id = campaign_id or f'campaign:{case_id}:{contract}'
    stop_after = stop_after or crash_after
    db = connect(db_path)
    try:
        with db:
            existing = db.execute('SELECT * FROM battery_campaign WHERE campaign_id=?',
                                  (campaign_id,)).fetchone()
            if existing:
                if existing['case_id'] != case_id or existing['contract'] != contract:
                    raise ValueError('campaign_conflict')
            else:
                db.execute(
                    '''INSERT INTO battery_campaign(
                           campaign_id,case_id,contract,plan_id,status,created_at,updated_at)
                       VALUES (?,?,?,NULL,'planned',?,?)''',
                    (campaign_id, case_id, contract, _now(), _now()),
                )
    finally:
        db.close()

    campaign = _campaign(db_path, campaign_id)
    # Only a fully completed campaign is a pure replay.  A partial or infeasible
    # campaign is re-evaluated: an operator may have repaired a source digest or
    # container capacity, and per-operation-id replays keep committed work
    # untouched.
    if campaign['status'] == 'completed' and not stop_after:
        result = json.loads(campaign['result']) if campaign['result'] else None
        if result is not None:
            result['replayed'] = True
            return result

    for _ in range(64):
        # Reconcile the frozen case with current physical state on every pass.
        # This clears source/capacity isolations that an operator has repaired
        # and surfaces newly broken packs before a stored plan is replayed.
        db = connect(db_path)
        try:
            with db:
                _plan_context(db, case_id)
        finally:
            db.close()
        campaign = _campaign(db_path, campaign_id)
        stored = None
        if campaign['plan_id']:
            db = connect(db_path)
            try:
                stored = _load_plan(db, campaign['plan_id'])
            finally:
                db.close()
        uncovered = _uncovered_packs(db_path, case_id, stored)
        if stored is None or uncovered:
            seq = 1
            if campaign['plan_id']:
                db = connect(db_path)
                try:
                    seq = db.execute('SELECT COUNT(*) FROM battery_campaign_plan '
                                     'WHERE campaign_id=?', (campaign_id,)).fetchone()[0] + 1
                finally:
                    db.close()
            plan = plan_campaign(db_path, case_id, contract,
                                 plan_id=plan_id or f'{campaign_id}:plan:{seq}')
            db = connect(db_path)
            try:
                with db:
                    db.execute('UPDATE battery_campaign SET plan_id=?,status=? WHERE campaign_id=?',
                               (plan['plan_id'],
                                'planned' if plan['status'] == 'planned' else plan['status'],
                                campaign_id))
                    db.execute('INSERT OR IGNORE INTO battery_campaign_plan(campaign_id,plan_id,seq) '
                               'VALUES (?,?,?)', (campaign_id, plan['plan_id'], seq))
            finally:
                db.close()
        else:
            plan = stored

        if plan['status'] == 'empty':
            return _finish_campaign(db_path, campaign_id, case_id, contract)
        if plan['status'] == 'infeasible':
            return _finish_campaign(db_path, campaign_id, case_id, contract,
                                    forced='infeasible', reason=plan.get('reason'))

        _set_campaign(db_path, campaign_id, status='running', reason=None)
        failure = None
        for step in _plan_steps(plan):
            operation_id = _step_id(campaign_id, plan['digest'], step)
            db = connect(db_path)
            try:
                with db:
                    _persist_step(db, operation_id, campaign_id, case_id,
                                  plan['plan_id'], step, 'pending')
            finally:
                db.close()
            try:
                result = _call_endpoint(db_path, case_id, step, operation_id, campaign_id)
            except ValueError as exc:
                code = str(exc)
                db = connect(db_path)
                try:
                    with db:
                        _persist_step(db, operation_id, campaign_id, case_id,
                                      plan['plan_id'], step, 'failed', error=code)
                finally:
                    db.close()
                if code in ('operation_id_conflict', 'campaign_conflict',
                            'battery_case_conflict'):
                    return _finish_campaign(db_path, campaign_id, case_id, contract,
                                            forced='failed', reason=code)
                _isolate_failure(db_path, case_id, step, code, operation_id)
                failure = (step, code)
                break
            db = connect(db_path)
            try:
                with db:
                    _persist_step(db, operation_id, campaign_id, case_id,
                                  plan['plan_id'], step, 'done', result=result)
            finally:
                db.close()
            if stop_after and stop_after == f"box{step['box_no']}:{step['kind']}":
                raise CampaignInterrupted(operation_id, step['kind'])

        if failure:
            step, code = failure
            _set_campaign(db_path, campaign_id, status='recovering', reason=code)
            plan = _new_plan(db_path, campaign_id, case_id, contract)
            if plan['status'] == 'planned':
                continue
            if plan['status'] == 'infeasible':
                return _finish_campaign(db_path, campaign_id, case_id, contract,
                                        forced='infeasible', reason=plan.get('reason') or code)
            return _finish_campaign(db_path, campaign_id, case_id, contract,
                                    reason=code)
        if _unaccounted(db_path, case_id):
            # Packs that appeared after planning, or a plan that could not cover
            # them, get a fresh exact plan over the remainder.
            plan = _new_plan(db_path, campaign_id, case_id, contract)
            if plan['status'] == 'planned':
                continue
            return _finish_campaign(db_path, campaign_id, case_id, contract,
                                    forced='infeasible' if plan['status'] == 'infeasible' else None,
                                    reason=plan.get('reason'))
        return _finish_campaign(db_path, campaign_id, case_id, contract)

    return _finish_campaign(db_path, campaign_id, case_id, contract,
                            forced='failed', reason='campaign_attempt_limit')


def _finish_campaign(db_path, campaign_id, case_id, contract, forced=None, reason=None):
    from .battery_report import build_report
    report = build_report(db_path, case_id, contract=contract, campaign_id=campaign_id)
    processed = [p for p in report['packs'] if p['disposition'] in ('recover', 'thermal')]
    remaining = [p for p in report['packs']
                 if p['disposition'] in ('unprocessed', 'missing')]
    conserved = report['conservation']['ok']
    if forced:
        status = 'partial' if forced == 'infeasible' and processed else forced
    elif not conserved:
        status = 'failed'
        reason = reason or 'battery_mass_conservation_failed'
    elif not remaining:
        status = 'completed'
    elif processed:
        status = 'partial'
        reason = reason or (report['isolated'][0]['reason'] if report['isolated']
                            else 'battery_unprocessed_packs_remain')
    else:
        status = 'infeasible'
        if reason:
            pass
        elif report['isolated']:
            reason = ('battery_no_usable_container'
                      if any(item['scope'] == 'container' for item in report['isolated'])
                      else 'battery_all_packs_isolated')
        else:
            reason = 'battery_no_feasible_box_plan'
    result = {
        'status': status,
        'reason': reason,
        'campaign_id': campaign_id,
        'case_id': case_id,
        'contract': contract,
        'plan_id': report['campaign'].get('plan_id'),
        'plans': report['campaign'].get('plans', []),
        'boxes': report['boxes'],
        'packs': report['packs'],
        'operations': report['operations'],
        'totals': report['totals'],
        'conservation': report['conservation'],
        'isolated': report['isolated'],
        'optimality': report.get('optimality'),
        'report': report,
    }
    db = connect(db_path)
    try:
        with db:
            db.execute('UPDATE battery_campaign SET status=?,reason=?,result=?,updated_at=? '
                       'WHERE campaign_id=?',
                       (status, reason, json.dumps(result, sort_keys=True), _now(), campaign_id))
    finally:
        db.close()
    return result


def run_campaign(db_path, case_id, contract, campaign_id=None):
    return execute_campaign(db_path, case_id, contract, campaign_id=campaign_id)


def campaign_state(db_path, case_id, campaign_id):
    db = connect(db_path)
    try:
        campaign = db.execute('SELECT * FROM battery_campaign WHERE campaign_id=? AND case_id=?',
                              (campaign_id, case_id)).fetchone()
        if not campaign:
            raise ValueError('unknown_battery_campaign')
        result = json.loads(campaign['result']) if campaign['result'] else None
        steps = [dict(row) for row in db.execute(
            'SELECT * FROM battery_step WHERE campaign_id=? ORDER BY created_at,operation_id',
            (campaign_id,))]
        plans = [dict(row) for row in db.execute(
            '''SELECT p.plan_id,p.contract,p.status,p.created_at
               FROM battery_plan p JOIN battery_campaign_plan c ON c.plan_id=p.plan_id
               WHERE c.campaign_id=? ORDER BY c.seq''', (campaign_id,))]
        return {'campaign': dict(campaign), 'result': result, 'steps': steps, 'plans': plans}
    finally:
        db.close()

"""Local battery inventory and treatment primitives, without a campaign planner."""

import hashlib
import json
from pathlib import Path

from .store import connect


def register(db_path, case_id, fixture):
    raw = Path(fixture).read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('packs') or not doc.get('containers'):
        raise ValueError('bad_battery_case')
    db = connect(db_path)
    try:
        with db:
            for pack in doc['packs']:
                source = db.execute(
                    '''SELECT s.sha256 FROM reading r JOIN source s ON s.id=r.source_id
                       WHERE r.device=? AND r.slot=? ORDER BY r.seq LIMIT 1''',
                    (pack['device'], pack['slot']),
                ).fetchone()
                if not source or source['sha256'] != pack['reading_source_sha256']:
                    raise ValueError('battery_device_source_missing')
                if pack['mass_kg'] != pack['metal_kg'] + pack['fluid_kg']:
                    raise ValueError('pack_mass_mismatch')
            digest = hashlib.sha256(raw).hexdigest()
            old = db.execute('SELECT digest FROM battery_case WHERE id=?', (case_id,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('battery_case_conflict')
            db.execute('INSERT OR IGNORE INTO battery_case VALUES (?,?,?)', (case_id, json.dumps(doc, sort_keys=True), digest))
            for item in doc['packs']:
                db.execute(
                    'INSERT OR IGNORE INTO battery_pack VALUES (?,?,?,?,?,?,?,?,?)',
                    (item['id'], case_id, item['chemistry'], item['mass_kg'],
                     item['metal_kg'], item['fluid_kg'], None, 'field', 'unprocessed'),
                )
            for item in doc['containers']:
                db.execute(
                    'INSERT OR IGNORE INTO battery_container VALUES (?,?,?,?)',
                    (item['id'], case_id, item['capacity_kg'], 'field'),
                )
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest}


def state(db_path, case_id):
    db = connect(db_path)
    try:
        packs = [dict(x) for x in db.execute('SELECT * FROM battery_pack WHERE case_id=? ORDER BY id', (case_id,))]
        containers = [dict(x) for x in db.execute('SELECT * FROM battery_container WHERE case_id=? ORDER BY id', (case_id,))]
        if not packs:
            raise ValueError('unknown_battery_case')
        return {'packs': packs, 'containers': containers}
    finally:
        db.close()


def _replay(db, operation_id):
    old = db.execute('SELECT result FROM battery_operation WHERE id=?', (operation_id,)).fetchone()
    return {'status': 'replayed', 'record': json.loads(old['result'])} if old else None


def _record(db, operation_id, case_id, kind, result):
    db.execute('INSERT INTO battery_operation VALUES (?,?,?,?)',
               (operation_id, case_id, kind, json.dumps(result, sort_keys=True)))
    return result


def load(db_path, case_id, pack_id, container_id, operation_id):
    db = connect(db_path)
    try:
        with db:
            prior = _replay(db, operation_id)
            if prior:
                return prior
            pack = db.execute('SELECT * FROM battery_pack WHERE id=? AND case_id=?', (pack_id, case_id)).fetchone()
            box = db.execute('SELECT * FROM battery_container WHERE id=? AND case_id=?', (container_id, case_id)).fetchone()
            if not pack or not box or pack['location'] != 'field' or pack['container'] or box['location'] != 'field':
                raise ValueError('unavailable_pack_or_container')
            used = db.execute('SELECT COALESCE(SUM(mass_kg),0) FROM battery_pack WHERE container=?', (container_id,)).fetchone()[0]
            if used + pack['mass_kg'] > box['capacity_kg']:
                raise ValueError('container_capacity_exceeded')
            db.execute('UPDATE battery_pack SET container=? WHERE id=?', (container_id, pack_id))
            return _record(db, operation_id, case_id, 'load', {'status': 'loaded', 'pack': pack_id, 'container': container_id, 'mass_kg': pack['mass_kg']})
    finally:
        db.close()


def ship(db_path, case_id, container_id, operation_id):
    db = connect(db_path)
    try:
        with db:
            prior = _replay(db, operation_id)
            if prior:
                return prior
            box = db.execute('SELECT * FROM battery_container WHERE id=? AND case_id=?', (container_id, case_id)).fetchone()
            packs = [dict(x) for x in db.execute('SELECT * FROM battery_pack WHERE container=? AND case_id=?', (container_id, case_id))]
            if not box or box['location'] != 'field' or not packs:
                raise ValueError('container_not_shippable')
            db.execute("UPDATE battery_container SET location='facility' WHERE id=?", (container_id,))
            db.execute("UPDATE battery_pack SET location='facility' WHERE container=?", (container_id,))
            return _record(db, operation_id, case_id, 'ship', {'status': 'shipped', 'container': container_id, 'packs': [p['id'] for p in packs], 'mass_kg': sum(p['mass_kg'] for p in packs)})
    finally:
        db.close()


def process(db_path, case_id, container_id, route, operation_id):
    if route not in ('recover', 'thermal'):
        raise ValueError('bad_treatment_route')
    db = connect(db_path)
    try:
        with db:
            prior = _replay(db, operation_id)
            if prior:
                return prior
            box = db.execute('SELECT * FROM battery_container WHERE id=? AND case_id=?', (container_id, case_id)).fetchone()
            packs = [dict(x) for x in db.execute('SELECT * FROM battery_pack WHERE container=? AND case_id=?', (container_id, case_id))]
            if not box or box['location'] != 'facility' or not packs or any(p['disposition'] != 'unprocessed' for p in packs):
                raise ValueError('container_not_processable')
            if route == 'recover' and len({p['chemistry'] for p in packs}) != 1:
                raise ValueError('mixed_chemistry_cannot_recover')
            for pack in packs:
                db.execute('UPDATE battery_pack SET disposition=? WHERE id=?', (route, pack['id']))
            recovered = sum(p['metal_kg'] for p in packs) if route == 'recover' else 0
            neutralized = sum(p['fluid_kg'] for p in packs) if route == 'recover' else sum(p['mass_kg'] for p in packs)
            return _record(db, operation_id, case_id, 'process', {'status': 'processed', 'container': container_id, 'route': route, 'recovered_kg': recovered, 'neutralized_kg': neutralized})
    finally:
        db.close()

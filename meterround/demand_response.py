"""Executable local building controls; demand-response planning is deliberately absent."""

import hashlib
import json
from pathlib import Path

from .store import connect


def register(db_path, case_id, fixture):
    raw = Path(fixture).read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('zones') or not doc.get('periods'):
        raise ValueError('bad_demand_response_case')
    db = connect(db_path)
    try:
        with db:
            for zone in doc['zones']:
                row = db.execute(
                    '''SELECT s.sha256 FROM reading r JOIN source s ON s.id=r.source_id
                       WHERE r.device=? AND r.slot=? ORDER BY r.seq LIMIT 1''',
                    (zone['device'], zone['slot']),
                ).fetchone()
                if not row or row['sha256'] != zone['reading_source_sha256']:
                    raise ValueError('demand_response_meter_source_missing')
            digest = hashlib.sha256(raw).hexdigest()
            old = db.execute('SELECT digest FROM dr_case WHERE id=?', (case_id,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('demand_response_case_conflict')
            db.execute(
                'INSERT OR IGNORE INTO dr_case VALUES (?,?,?)',
                (case_id, json.dumps(doc, sort_keys=True), digest),
            )
            for zone in doc['zones']:
                db.execute(
                    'INSERT OR IGNORE INTO dr_zone VALUES (?,?,?,0)',
                    (case_id, zone['id'], zone['initial_temperature']),
                )
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest, 'zones': len(doc['zones'])}


def state(db_path, case_id):
    db = connect(db_path)
    try:
        rows = db.execute(
            'SELECT id,temperature,period FROM dr_zone WHERE case_id=? ORDER BY id',
            (case_id,),
        ).fetchall()
        if not rows:
            raise ValueError('unknown_demand_response_case')
        return [dict(row) for row in rows]
    finally:
        db.close()


def dispatch(db_path, case_id, period, loads, action_id):
    """Apply one period of physical controls and return measured simulator state."""
    db = connect(db_path)
    try:
        with db:
            prior = db.execute('SELECT result FROM dr_action WHERE id=?', (action_id,)).fetchone()
            if prior:
                return {'status': 'replayed', 'record': json.loads(prior['result'])}
            row = db.execute('SELECT payload FROM dr_case WHERE id=?', (case_id,)).fetchone()
            if not row:
                raise ValueError('unknown_demand_response_case')
            doc = json.loads(row['payload'])
            if period < 0 or period >= len(doc['periods']):
                raise ValueError('period_out_of_range')
            actual = [dict(x) for x in db.execute(
                'SELECT id,temperature,period FROM dr_zone WHERE case_id=? ORDER BY id',
                (case_id,),
            )]
            if any(x['period'] != period for x in actual):
                raise ValueError('period_sequence_conflict')
            selected = list(loads)
            if len(selected) != len(set(selected)) or any(x not in {z['id'] for z in doc['zones']} for x in selected):
                raise ValueError('invalid_load')
            equipment = {z['id']: z for z in doc['zones']}
            total_kw = sum(equipment[x]['power_kw'] for x in selected)
            window = doc['periods'][period]
            if total_kw > window['grid_cap_kw']:
                raise ValueError('grid_cap_exceeded')
            result_zones = {}
            for before in actual:
                z = equipment[before['id']]
                after = before['temperature'] + window['heat_gain'][z['id']]
                if z['id'] in selected:
                    after -= z['cooling']
                if after < z['minimum_temperature']:
                    raise ValueError('overcooling')
                exposure = max(0, after - z['maximum_temperature'])
                result_zones[z['id']] = {'temperature': after, 'exposure': exposure}
                db.execute(
                    'UPDATE dr_zone SET temperature=?,period=? WHERE case_id=? AND id=?',
                    (after, period + 1, case_id, z['id']),
                )
            baseline = sum(z['power_kw'] for z in doc['zones'])
            result = {
                'status': 'applied', 'period': period, 'loads': selected,
                'grid_kw': total_kw, 'shed_kw': baseline - total_kw,
                'energy_cents': total_kw * window['price_cents_per_kwh'],
                'zones': result_zones,
            }
            db.execute(
                'INSERT INTO dr_action VALUES (?,?,?,?,?)',
                (action_id, case_id, period, json.dumps(selected), json.dumps(result, sort_keys=True)),
            )
            return result
    finally:
        db.close()

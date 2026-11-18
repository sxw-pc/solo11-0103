"""Executable local building controls with an exact demand-response planner.

The meter-ingestion, settlement and green-attribute tables are untouched.  This
module adds an exhaustive (branch-and-bound with dominance memoisation) planner
for at most four zones and eight periods, a hard separation between planned and
physically executed periods, and late temperature feedback that only ever
corrects future planning inputs.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .ingest import at
from .store import connect

MIN_ENERGY = 'min_energy'
MIN_MAX_ZONE_EXPOSURE = 'min_max_zone_exposure'
CONTRACTS = (MIN_ENERGY, MIN_MAX_ZONE_EXPOSURE)

PLAN_PENDING = '待执行'
PLAN_PARTIAL = '部分执行'
PLAN_DONE = '已完成'
PLAN_HOLD = '待核'

MAX_ZONES = 4
MAX_PERIODS = 8
TEMP_TOLERANCE = 1e-9


class InfeasiblePlan(ValueError):
    """Raised after an 待核 audit row has been recorded; no command is sent."""


# --------------------------------------------------------------------------- #
# Case validation and source evidence
# --------------------------------------------------------------------------- #
def _require(doc, path, kind):
    cur = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            raise ValueError('bad_demand_response_case')
        cur = cur[key]
    if kind == 'number' and not isinstance(cur, (int, float)):
        raise ValueError('bad_demand_response_case')
    if kind == 'str' and not isinstance(cur, str):
        raise ValueError('bad_demand_response_case')
    if kind == 'dict' and not isinstance(cur, dict):
        raise ValueError('bad_demand_response_case')
    return cur


def validate_case(doc):
    """Structural validation shared by register and the planner."""
    if not isinstance(doc, dict):
        raise ValueError('bad_demand_response_case')
    zones = doc.get('zones')
    periods = doc.get('periods')
    if not isinstance(zones, list) or not isinstance(periods, list) or not zones or not periods:
        raise ValueError('bad_demand_response_case')
    if len(zones) > MAX_ZONES or len(periods) > MAX_PERIODS:
        raise ValueError('dr_case_out_of_local_scope')
    zone_ids = []
    for zone in zones:
        for key in ('id', 'device', 'slot', 'reading_source_sha256', 'initial_temperature',
                    'minimum_temperature', 'maximum_temperature', 'power_kw', 'cooling'):
            _require(zone, (key,), 'number' if key.endswith(('_temperature', '_kw', 'cooling')) else 'str')
        if zone['minimum_temperature'] > zone['maximum_temperature']:
            raise ValueError('bad_demand_response_case')
        zone_ids.append(zone['id'])
    if len(set(zone_ids)) != len(zone_ids):
        raise ValueError('bad_demand_response_case')
    for window in periods:
        _require(window, ('grid_cap_kw',), 'number')
        _require(window, ('price_cents_per_kwh',), 'number')
        gain = _require(window, ('heat_gain',), 'dict')
        if not isinstance(gain, dict) or any(zid not in gain for zid in zone_ids):
            raise ValueError('bad_demand_response_case')
        for zid in zone_ids:
            if not isinstance(gain[zid], (int, float)):
                raise ValueError('bad_demand_response_case')
    for key in ('max_total_exposure', 'max_zone_exposure', 'minimum_shed_kw_each_period'):
        _require(doc, (key,), 'number')
    contracts = doc.get('contracts')
    if not isinstance(contracts, list) or any(c not in CONTRACTS for c in contracts):
        raise ValueError('bad_demand_response_case')
    return zone_ids


def _zone_readings(db, zone):
    return [dict(r) for r in db.execute(
        '''SELECT r.id,r.device,r.slot,r.meter,r.at,r.value,r.sha256 AS payload_sha256,s.sha256 AS source_sha256
           FROM reading r JOIN source s ON s.id=r.source_id
           WHERE r.device=? AND r.slot=? ORDER BY r.seq''',
        (zone['device'], zone['slot']),
    )]


def _check_zone_source(db, zone):
    """Re-verify that the device/slot reading still attests the registered SHA."""
    row = db.execute(
        '''SELECT s.sha256 FROM reading r JOIN source s ON s.id=r.source_id
           WHERE r.device=? AND r.slot=? ORDER BY r.seq LIMIT 1''',
        (zone['device'], zone['slot']),
    ).fetchone()
    if not row or row['sha256'] != zone['reading_source_sha256']:
        raise ValueError('demand_response_meter_source_missing')


def _check_timezone_alignment(db, doc):
    """Every reading referenced by the case must share one UTC offset."""
    offsets = set()
    for zone in doc['zones']:
        for reading in _zone_readings(db, zone):
            offsets.add(at(reading['at']).utcoffset())
    if len(offsets) > 1:
        raise ValueError('dr_timezone_misaligned')


def source_summary(db, doc):
    """Per-zone provenance digest: readings, device/serial slot and source SHA."""
    summary = {}
    for zone in doc['zones']:
        readings = _zone_readings(db, zone)
        summary[zone['id']] = {
            'device': zone['device'],
            'slot': zone['slot'],
            'reading_source_sha256': zone['reading_source_sha256'],
            'readings': [
                {
                    'reading_id': r['id'], 'meter': r['meter'], 'at': r['at'],
                    'value': r['value'], 'source_sha256': r['source_sha256'],
                    'payload_sha256': r['payload_sha256'],
                }
                for r in readings
            ],
        }
    return summary


# --------------------------------------------------------------------------- #
# Registration and live state
# --------------------------------------------------------------------------- #
def register(db_path, case_id, fixture):
    raw = Path(fixture).read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('zones') or not doc.get('periods'):
        raise ValueError('bad_demand_response_case')
    validate_case(doc)
    db = connect(db_path)
    try:
        with db:
            for zone in doc['zones']:
                _check_zone_source(db, zone)
            _check_timezone_alignment(db, doc)
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


# --------------------------------------------------------------------------- #
# Physical dispatch (unchanged semantics; executed periods are immutable)
# --------------------------------------------------------------------------- #
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
                'energy_cents': _money(total_kw, window['price_cents_per_kwh']),
                'zones': result_zones,
            }
            db.execute(
                'INSERT INTO dr_action VALUES (?,?,?,?,?)',
                (action_id, case_id, period, json.dumps(selected), json.dumps(result, sort_keys=True)),
            )
            return result
    finally:
        db.close()


def _money(kw, price):
    value = kw * price
    return int(value) if isinstance(value, float) and value.is_integer() else value


# --------------------------------------------------------------------------- #
# Exact planner: exhaustive DFS + dominance memo + monotone bound pruning
# --------------------------------------------------------------------------- #
def _enumerate_actions(doc, period):
    """All load subsets honouring grid cap and the frozen shed commitment."""
    zones = doc['zones']
    window = doc['periods'][period]
    baseline = sum(z['power_kw'] for z in zones)
    actions = []
    for mask in range(1 << len(zones)):
        selected = tuple(zones[i]['id'] for i in range(len(zones)) if mask & (1 << i))
        total_kw = sum(next(z for z in zones if z['id'] == zid)['power_kw'] for zid in selected)
        if total_kw > window['grid_cap_kw']:
            continue
        if baseline - total_kw < doc['minimum_shed_kw_each_period']:
            continue
        actions.append((selected, total_kw))
    actions.sort(key=lambda item: (item[1], item[0]))
    return actions


def _objective_vector(contract, energy, zone_exposure):
    if contract == MIN_ENERGY:
        return energy, max(zone_exposure)
    return max(zone_exposure), energy


def plan_exact(doc, contract, start_temps, exposure_already, start_period=0):
    """Return the exact lexicographic optimum and a machine-checkable proof.

    Temperatures evolve as ``before + heat_gain - cooling_if_on``; positive
    excess over the zone maximum accumulates monotonically (later cooling can
    never erase past exposure).  The search enumerates every feasible load
    subset per period, so the returned optimum is globally exact.
    """
    if contract not in CONTRACTS:
        raise ValueError('bad_contract')
    zones = doc['zones']
    zids = [z['id'] for z in zones]
    equipment = {z['id']: z for z in zones}
    horizon = len(doc['periods'])
    initial_exposure = tuple(exposure_already[zid] for zid in zids)
    temps0 = tuple(start_temps[zid] for zid in zids)
    actions_by_period = {p: _enumerate_actions(doc, p) for p in range(start_period, horizon)}

    proof = {
        'algorithm': 'exhaustive_dfs_dominance_memo',
        'zones': len(zones), 'horizon_periods': horizon, 'start_period': start_period,
        'actions_per_period': {str(p): len(a) for p, a in actions_by_period.items()},
        'nodes_visited': 0, 'transitions_tested': 0, 'infeasible_transitions': 0,
        'bound_prunes': 0, 'memo_prunes': 0, 'feasible_sequences': 0,
        'objective_ties': 0,
    }
    best = {'vector': None, 'choices': None}
    memo = {}

    def visit(period, temps, exposure, spent, choices):
        proof['nodes_visited'] += 1
        if period == horizon:
            total_exposure = sum(exposure)
            if total_exposure > doc['max_total_exposure'] or any(
                e > doc['max_zone_exposure'] for e in exposure
            ):
                proof['bound_prunes'] += 1
                return
            vector = _objective_vector(contract, spent, list(exposure))
            proof['feasible_sequences'] += 1
            if best['vector'] is None or vector < best['vector']:
                best['vector'] = vector
                best['choices'] = list(choices)
            elif vector == best['vector']:
                proof['objective_ties'] += 1
            return
        key = (period, temps, exposure)
        prior_spent = memo.get(key)
        if prior_spent is not None and prior_spent <= spent:
            # Same physical state reached earlier with no greater spend:
            # every continuation available here was already explored there.
            proof['memo_prunes'] += 1
            return
        memo[key] = spent
        window = doc['periods'][period]
        for selected, total_kw in actions_by_period[period]:
            proof['transitions_tested'] += 1
            next_temps = []
            next_exposure = []
            ok = True
            candidate_total = sum(exposure)
            for i, zid in enumerate(zids):
                z = equipment[zid]
                after = temps[i] + window['heat_gain'][zid]
                if zid in selected:
                    after -= z['cooling']
                if after < z['minimum_temperature']:
                    ok = False
                    break
                delta = max(0, after - z['maximum_temperature'])
                exposed = exposure[i] + delta
                candidate_total += delta
                if exposed > doc['max_zone_exposure'] or candidate_total > doc['max_total_exposure']:
                    ok = False
                    break
                next_temps.append(after)
                next_exposure.append(exposed)
            if not ok:
                proof['infeasible_transitions'] += 1
                continue
            cost = _money(total_kw, window['price_cents_per_kwh'])
            choices.append((period, selected, total_kw))
            visit(period + 1, tuple(next_temps), tuple(next_exposure), spent + cost, choices)
            choices.pop()

    visit(start_period, temps0, initial_exposure, 0, [])
    if best['vector'] is None:
        proof['basis'] = 'every branch pruned: no sequence satisfies all constraints'
        return None, proof

    # Replay the winning choices to emit per-period physical detail.
    periods_out = []
    temps = dict(zip(zids, temps0))
    exposure = dict(zip(zids, initial_exposure))
    energy_total = 0
    for period, selected, total_kw in best['choices']:
        window = doc['periods'][period]
        zone_detail = {}
        for zid in zids:
            z = equipment[zid]
            before = temps[zid]
            after = before + window['heat_gain'][zid] - (z['cooling'] if zid in selected else 0)
            period_exposure = max(0, after - z['maximum_temperature'])
            exposure[zid] += period_exposure
            temps[zid] = after
            zone_detail[zid] = {
                'before_temperature': before, 'after_temperature': after,
                'heat_gain': window['heat_gain'][zid],
                'cooling_applied': z['cooling'] if zid in selected else 0,
                'exposure_period': period_exposure,
                'exposure_cumulative': exposure[zid],
                'power_kw': z['power_kw'] if zid in selected else 0,
            }
        cost = _money(total_kw, window['price_cents_per_kwh'])
        energy_total += cost
        baseline = sum(z['power_kw'] for z in zones)
        periods_out.append({
            'period': period, 'loads': list(selected), 'grid_kw': total_kw,
            'shed_kw': baseline - total_kw, 'energy_cents': cost,
            'grid_cap_kw': window['grid_cap_kw'],
            'minimum_shed_kw': doc['minimum_shed_kw_each_period'],
            'price_cents_per_kwh': window['price_cents_per_kwh'],
            'zones': zone_detail, 'status': PLAN_PENDING,
        })
    objective = {
        'contract': contract,
        'energy_cents_total': energy_total,
        'zone_exposure_total': {zid: exposure[zid] for zid in zids},
        'max_zone_exposure': max(exposure.values()),
        'total_exposure': sum(exposure.values()),
        'vector': list(best['vector']),
    }
    proof['basis'] = (
        'all ' + str(proof['feasible_sequences']) + ' feasible load sequences enumerated; '
        'dominance memo and monotone exposure bounds only discard provably non-better branches'
    )
    return {'objective': objective, 'proof': proof, 'periods': periods_out}, proof


# --------------------------------------------------------------------------- #
# Plan persistence
# --------------------------------------------------------------------------- #
def _executed_exposure(db, case_id, zids):
    done = {zid: 0 for zid in zids}
    for row in db.execute(
        'SELECT result FROM dr_action WHERE case_id=? ORDER BY period', (case_id,)
    ):
        result = json.loads(row['result'])
        for zid, detail in result['zones'].items():
            done[zid] += detail['exposure']
    return done


def plan(db_path, case_id, contract, plan_id=None, start_period=None):
    """Compute and persist an exact optimum; never mutates physical state."""
    db = connect(db_path)
    try:
        row = db.execute('SELECT payload,digest FROM dr_case WHERE id=?', (case_id,)).fetchone()
        if not row:
            raise ValueError('unknown_demand_response_case')
        doc = json.loads(row['payload'])
        validate_case(doc)
        if contract not in CONTRACTS:
            raise ValueError('bad_contract')
        for zone in doc['zones']:
            _check_zone_source(db, zone)
        _check_timezone_alignment(db, doc)
        zids = [z['id'] for z in doc['zones']]
        live = {r['id']: r for r in db.execute(
            'SELECT id,temperature,period FROM dr_zone WHERE case_id=?', (case_id,))}
        current_period = max(r['period'] for r in live.values())
        if any(r['period'] != current_period for r in live.values()):
            raise ValueError('period_sequence_conflict')
        start = current_period if start_period is None else start_period
        if start < current_period or start > len(doc['periods']):
            raise ValueError('period_out_of_range')
        start_temps = {zid: live[zid]['temperature'] for zid in zids}
        exposure_already = _executed_exposure(db, case_id, zids)
        if start >= len(doc['periods']):
            # Nothing left to schedule: the plan is an auditable completed record.
            total = sum(exposure_already.values())
            if total > doc['max_total_exposure'] or any(
                v > doc['max_zone_exposure'] for v in exposure_already.values()):
                raise InfeasiblePlan('dr_executed_exposure_over_limit')
            payload = {
                'case_id': case_id, 'contract': contract, 'status': PLAN_DONE,
                'start_period': start, 'horizon_periods': len(doc['periods']),
                'start_temperatures': start_temps, 'exposure_already': exposure_already,
                'objective': {
                    'contract': contract, 'energy_cents_total': 0,
                    'zone_exposure_total': exposure_already,
                    'max_zone_exposure': max(exposure_already.values()),
                    'total_exposure': total, 'vector': [0, max(exposure_already.values())],
                },
                'proof': {'algorithm': 'no_remaining_periods', 'feasible_sequences': 1},
                'periods': [], 'source_summary': source_summary(db, doc),
                'case_digest': row['digest'],
            }
            return _persist_plan(db, case_id, contract, start, payload, plan_id)

        outcome, proof = plan_exact(doc, contract, start_temps, exposure_already, start)
        executed = [json.loads(r['result']) for r in db.execute(
            'SELECT result FROM dr_action WHERE case_id=? AND period<? ORDER BY period',
            (case_id, start),
        )]
        provenance = source_summary(db, doc)
        if outcome is None:
            payload = {
                'case_id': case_id, 'contract': contract, 'status': PLAN_HOLD,
                'start_period': start, 'start_temperatures': start_temps,
                'exposure_already': exposure_already, 'proof': proof,
                'source_summary': provenance, 'case_digest': row['digest'],
                'reason': 'no feasible schedule within grid/shed/exposure limits',
            }
            return _persist_plan(db, case_id, contract, start, payload, plan_id, infeasible=True)

        payload = {
            'case_id': case_id, 'contract': contract, 'status': PLAN_PENDING,
            'start_period': start, 'horizon_periods': len(doc['periods']),
            'start_temperatures': start_temps, 'exposure_already': exposure_already,
            'objective': outcome['objective'], 'proof': outcome['proof'],
            'limits': {
                'grid_cap_kw': [w['grid_cap_kw'] for w in doc['periods']],
                'minimum_shed_kw_each_period': doc['minimum_shed_kw_each_period'],
                'max_total_exposure': doc['max_total_exposure'],
                'max_zone_exposure': doc['max_zone_exposure'],
            },
            'executed_periods': [
                {'period': r['period'], 'loads': r['loads'], 'grid_kw': r['grid_kw'],
                 'shed_kw': r['shed_kw'], 'energy_cents': r['energy_cents'],
                 'zones': r['zones'], 'status': PLAN_DONE}
                for r in executed
            ],
            'periods': outcome['periods'], 'source_summary': provenance,
            'case_digest': row['digest'],
        }
        return _persist_plan(db, case_id, contract, start, payload, plan_id)
    finally:
        db.close()


def _persist_plan(db, case_id, contract, start, payload, plan_id, infeasible=False):
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    payload['digest'] = digest
    if plan_id is None:
        plan_id = f'{case_id}:{contract}:p{start}:{digest[:12]}'
    existing = db.execute('SELECT digest,status,payload FROM dr_plan WHERE id=?', (plan_id,)).fetchone()
    if existing:
        if existing['digest'] != digest:
            raise ValueError('dr_plan_conflict')
        return {'plan_id': plan_id, 'status': existing['status'], 'replayed': True,
                'digest': digest}
    with db:
        db.execute(
            'INSERT INTO dr_plan(id,case_id,contract,start_period,status,payload,digest,created_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (plan_id, case_id, contract, start, payload['status'],
             json.dumps(payload, ensure_ascii=False, sort_keys=True), digest,
             datetime.utcnow().isoformat(timespec='seconds') + 'Z'),
        )
        if not infeasible:
            for entry in payload['periods']:
                db.execute(
                    'INSERT INTO dr_command VALUES (?,?,?,?,?,?,?)',
                    (f'{plan_id}:p{entry["period"]}', plan_id, entry['period'],
                     json.dumps(entry['loads']), PLAN_PENDING, None,
                     json.dumps(entry, ensure_ascii=False, sort_keys=True)),
                )
    if infeasible:
        raise InfeasiblePlan('dr_no_feasible_plan')
    return {'plan_id': plan_id, 'status': payload['status'], 'replayed': False,
            'digest': digest, 'objective': payload.get('objective')}


# --------------------------------------------------------------------------- #
# Execution: planned commands versus physical dispatch
# --------------------------------------------------------------------------- #
def execute(db_path, plan_id, period, action_id=None):
    """Dispatch one planned period. Repeating a request replays, never re-advances."""
    db = connect(db_path)
    try:
        plan_row = db.execute('SELECT * FROM dr_plan WHERE id=?', (plan_id,)).fetchone()
        if not plan_row:
            raise ValueError('unknown_demand_response_plan')
        command = db.execute(
            'SELECT * FROM dr_command WHERE plan_id=? AND period=?', (plan_id, period)
        ).fetchone()
        if not command:
            raise ValueError('unknown_dr_command')
        if command['status'] == PLAN_DONE:
            return {'status': 'replayed', 'plan_id': plan_id, 'period': period,
                    'record': json.loads(command['detail'])}
        if command['status'] == PLAN_HOLD:
            raise ValueError('dr_command_on_hold')
        case_id = plan_row['case_id']
        case = db.execute('SELECT payload FROM dr_case WHERE id=?', (case_id,)).fetchone()
        doc = json.loads(case['payload'])
        zids = [z['id'] for z in doc['zones']]
        for zone in doc['zones']:
            _check_zone_source(db, zone)
        live = {r['id']: dict(r) for r in db.execute(
            'SELECT id,temperature,period FROM dr_zone WHERE case_id=?', (case_id,))}
        current = next(iter(live.values()))['period']
        if any(r['period'] != current for r in live.values()) or current != period:
            _hold_command(db, command, 'period_sequence_conflict')
            raise ValueError('period_sequence_conflict')
        planned = json.loads(command['detail'])
        for zid in zids:
            if abs(live[zid]['temperature'] - planned['zones'][zid]['before_temperature']) > TEMP_TOLERANCE:
                _hold_command(db, command, 'dr_plan_drift')
                raise ValueError('dr_plan_drift')
        window = doc['periods'][period]
        equipment = {z['id']: z for z in doc['zones']}
        planned_loads = set(json.loads(command['loads']))
        current_grid = sum(equipment[zid]['power_kw'] for zid in planned_loads)
        capacity_changed = (
            planned['grid_cap_kw'] != window['grid_cap_kw']
            or current_grid != planned['grid_kw']
        )
        if capacity_changed:
            _hold_command(db, command, 'dr_equipment_capacity_changed')
            raise ValueError('dr_equipment_capacity_changed')
        loads = json.loads(command['loads'])
        action_id = action_id or f'{plan_id}:p{period}'
        db.commit()  # release the transaction before the nested dispatch transaction
    finally:
        db.close()

    try:
        result = dispatch(db_path, case_id, period, loads, action_id)
    except ValueError as exc:
        _hold_command_by_id(db_path, command['id'], str(exc))
        raise
    if result['status'] == 'replayed':
        record = result['record']
    else:
        record = result
    _finish_command(db_path, plan_id, command['id'], action_id, record)
    return {'status': record['status'], 'plan_id': plan_id, 'period': period,
            'action_id': action_id, 'record': record}


def _hold_command(db, command, code):
    with db:
        db.execute('UPDATE dr_command SET status=?,detail=? WHERE id=?',
                   (PLAN_HOLD, json.dumps({'hold_reason': code}, ensure_ascii=False), command['id']))


def _hold_command_by_id(db_path, command_id, code):
    db = connect(db_path)
    try:
        with db:
            db.execute('UPDATE dr_command SET status=?,detail=? WHERE id=?',
                       (PLAN_HOLD, json.dumps({'hold_reason': code}, ensure_ascii=False), command_id))
    finally:
        db.close()


def _finish_command(db_path, plan_id, command_id, action_id, record):
    db = connect(db_path)
    try:
        with db:
            db.execute(
                'UPDATE dr_command SET status=?,action_id=?,detail=? WHERE id=?',
                (PLAN_DONE, action_id, json.dumps(record, ensure_ascii=False, sort_keys=True),
                 command_id),
            )
            pending = db.execute(
                'SELECT COUNT(*) FROM dr_command WHERE plan_id=? AND status=?',
                (plan_id, PLAN_PENDING),
            ).fetchone()[0]
            held = db.execute(
                'SELECT COUNT(*) FROM dr_command WHERE plan_id=? AND status=?',
                (plan_id, PLAN_HOLD),
            ).fetchone()[0]
            status = PLAN_HOLD if held else (PLAN_DONE if pending == 0 else PLAN_PARTIAL)
            db.execute('UPDATE dr_plan SET status=? WHERE id=?', (status, plan_id))
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Late temperature feedback: corrects future inputs only
# --------------------------------------------------------------------------- #
def feedback(db_path, case_id, period, zone_id, observed_temperature, source_sha256, observed_at):
    """Absorb a measured temperature; executed periods are never rewritten."""
    dt = at(observed_at) if isinstance(observed_at, str) else observed_at
    if dt.tzinfo is None:
        raise ValueError('timezone_required')
    db = connect(db_path)
    try:
        case = db.execute('SELECT payload FROM dr_case WHERE id=?', (case_id,)).fetchone()
        if not case:
            raise ValueError('unknown_demand_response_case')
        doc = json.loads(case['payload'])
        zone = next((z for z in doc['zones'] if z['id'] == zone_id), None)
        if zone is None or not 0 <= period < len(doc['periods']):
            raise ValueError('bad_feedback')
        if not isinstance(observed_temperature, (int, float)):
            raise ValueError('bad_feedback')
        duplicate = db.execute(
            'SELECT * FROM dr_feedback WHERE case_id=? AND period=? AND zone_id=? ORDER BY id DESC LIMIT 1',
            (case_id, period, zone_id),
        ).fetchone()
        if duplicate:
            same = (
                duplicate['observed_temperature'] == observed_temperature
                and duplicate['source_sha256'] == source_sha256
                and duplicate['at'] == observed_at
            )
            if same:
                return {'status': duplicate['status'], 'replayed': True,
                        'detail': json.loads(duplicate['detail'])}
            return _record_feedback(db, case_id, period, zone_id, observed_temperature,
                                    source_sha256, observed_at, 'dr_feedback_conflict')
        offsets = {at(r['at']).utcoffset() for r in _zone_readings(db, zone)}
        if dt.utcoffset() not in offsets:
            return _record_feedback(db, case_id, period, zone_id, observed_temperature,
                                    source_sha256, observed_at, 'dr_timezone_misaligned')
        if source_sha256 != zone['reading_source_sha256']:
            return _record_feedback(db, case_id, period, zone_id, observed_temperature,
                                    source_sha256, observed_at,
                                    'demand_response_meter_source_missing')
        current = db.execute(
            'SELECT period FROM dr_zone WHERE case_id=? AND id=?', (case_id, zone_id)
        ).fetchone()['period']
        if period > current:
            return _record_feedback(db, case_id, period, zone_id, observed_temperature,
                                    source_sha256, observed_at, 'dr_feedback_out_of_sequence')
        if period < current:
            # The period is physically closed: exposure already banked, no rewrite.
            return _record_feedback(db, case_id, period, zone_id, observed_temperature,
                                    source_sha256, observed_at, 'late_feedback_immutable')
        with db:
            db.execute(
                'UPDATE dr_zone SET temperature=? WHERE case_id=? AND id=?',
                (observed_temperature, case_id, zone_id),
            )
        zids = [z['id'] for z in doc['zones']]
        done = _executed_exposure(db, case_id, zids)
        remaining_zone = doc['max_zone_exposure'] - done[zone_id]
        remaining_total = doc['max_total_exposure'] - sum(done.values())
        detail = {'code': 'accepted', 'period': period, 'zone_id': zone_id,
                  'observed_temperature': observed_temperature,
                  'remaining_zone_exposure': remaining_zone,
                  'remaining_total_exposure': remaining_total,
                  'note': 'feedback corrects future planning origins only'}
        over_budget = remaining_zone < 0 or remaining_total < 0
        with db:
            db.execute(
                'INSERT INTO dr_feedback VALUES (NULL,?,?,?,?,?,?,?,?)',
                (case_id, period, zone_id, observed_temperature, source_sha256,
                 observed_at, PLAN_HOLD if over_budget else '已接收',
                 json.dumps(detail, ensure_ascii=False, sort_keys=True)),
            )
            # The observed origin diverges from every pending plan: hold their
            # unexecuted commands so a stale plan can never be force-sent.
            db.execute(
                "UPDATE dr_command SET status=? WHERE plan_id IN "
                "(SELECT id FROM dr_plan WHERE case_id=? AND start_period<=?) "
                "AND status=?",
                (PLAN_HOLD, case_id, period, PLAN_PENDING),
            )
            if over_budget:
                db.execute(
                    "UPDATE dr_plan SET status=? WHERE case_id=? AND status IN (?,?)",
                    (PLAN_HOLD, case_id, PLAN_PENDING, PLAN_PARTIAL),
                )
            else:
                db.execute(
                    "UPDATE dr_plan SET status=? WHERE case_id=? AND status=?",
                    (PLAN_HOLD, case_id, PLAN_PENDING),
                )
        return {'status': PLAN_HOLD if over_budget else '已接收', 'replayed': False,
                'detail': detail}
    finally:
        db.close()


def _record_feedback(db, case_id, period, zone_id, observed_temperature, source_sha256,
                     observed_at, code):
    detail = {'code': code, 'period': period, 'zone_id': zone_id,
              'observed_temperature': observed_temperature}
    with db:
        db.execute(
            'INSERT INTO dr_feedback VALUES (NULL,?,?,?,?,?,?,?,?)',
            (case_id, period, zone_id, observed_temperature, source_sha256,
             observed_at, PLAN_HOLD, json.dumps(detail, ensure_ascii=False, sort_keys=True)),
        )
    return {'status': PLAN_HOLD, 'replayed': False, 'detail': detail}


# --------------------------------------------------------------------------- #
# JSON control evidence
# --------------------------------------------------------------------------- #
def evidence_payload(db_path, plan_id):
    db = connect(db_path)
    try:
        plan_row = db.execute('SELECT * FROM dr_plan WHERE id=?', (plan_id,)).fetchone()
        if not plan_row:
            raise ValueError('unknown_demand_response_plan')
        payload = json.loads(plan_row['payload'])
        payload['commands'] = [
            {
                'command_id': r['id'], 'period': r['period'],
                'loads': json.loads(r['loads']), 'status': r['status'],
                'action_id': r['action_id'], 'detail': json.loads(r['detail']),
            }
            for r in db.execute(
                'SELECT * FROM dr_command WHERE plan_id=? ORDER BY period', (plan_id,))
        ]
        payload['feedback'] = [
            dict(r) for r in db.execute(
                'SELECT * FROM dr_feedback WHERE case_id=? ORDER BY id',
                (plan_row['case_id'],))
        ]
        return payload
    finally:
        db.close()


def write_evidence(db_path, plan_id, out_path):
    payload = evidence_payload(db_path, plan_id)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding='utf-8')
    return path

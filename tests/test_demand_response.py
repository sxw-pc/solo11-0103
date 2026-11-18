import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_RUNNER = Path(tempfile.mkdtemp(prefix='meterround-cli-')) / 'run_meterround.py'
_RUNNER.write_text(
    'import sys\n'
    f"sys.path.insert(0, {str(REPO)!r})\n"
    "from meterround.__main__ import main\n"
    'main()\n'
)
# -I ignores PYTHONPATH/cwd entries, so invoke an isolated runner script instead.
_ENV = {**os.environ, 'PYTHONPATH': ''}

from meterround.__main__ import initialize
from meterround.demand_response import (
    CONTRACTS,
    InfeasiblePlan,
    PLAN_DONE,
    PLAN_HOLD,
    PLAN_PARTIAL,
    PLAN_PENDING,
    dispatch,
    evidence_payload,
    execute,
    feedback,
    plan,
    plan_exact,
    register,
    state,
)
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
SHA = '27feea1484717c00f10a346c723643e6d4db3385f57b57e10a9569d258be5de9'


def prepared(tmp_path, name):
    db = tmp_path / name
    initialize(db, FIX / 'dr_config.json')
    assert ingest(FIX / 'dr_readings.csv', db)['imported'] == 4
    assert set(evaluate(db).values()) == {'可结算'}
    assert register(db, 'dr-1', FIX / 'dr_case.json')['zones'] == 2
    return db


def _periods(payload):
    return {e['period']: e for e in payload['periods']}


# --------------------------------------------------------------------------- #
# Original physical-control tests (unchanged behaviour)
# --------------------------------------------------------------------------- #
def test_two_real_control_outcomes(tmp_path):
    cheap = prepared(tmp_path, 'cheap.db')
    first = dispatch(cheap, 'dr-1', 0, ['A'], 'cheap-0')
    second = dispatch(cheap, 'dr-1', 1, [], 'cheap-1')
    assert first['grid_kw'] == 2 and first['shed_kw'] == 1
    assert first['energy_cents'] + second['energy_cents'] == 200
    assert first['zones']['A']['exposure'] + second['zones']['A']['exposure'] == 2
    assert first['zones']['B']['exposure'] + second['zones']['B']['exposure'] == 4
    assert [x['temperature'] for x in state(cheap, 'dr-1')] == [24, 24]
    assert dispatch(cheap, 'dr-1', 0, ['A'], 'cheap-0')['status'] == 'replayed'

    cooler = prepared(tmp_path, 'cooler.db')
    first = dispatch(cooler, 'dr-1', 0, ['A'], 'cool-0')
    second = dispatch(cooler, 'dr-1', 1, ['B'], 'cool-1')
    assert first['energy_cents'] + second['energy_cents'] == 1200
    assert first['zones']['A']['exposure'] + second['zones']['A']['exposure'] == 2
    assert first['zones']['B']['exposure'] + second['zones']['B']['exposure'] == 2
    assert [x['temperature'] for x in state(cooler, 'dr-1')] == [24, 22]


def test_guardrails(tmp_path):
    db = prepared(tmp_path, 'guard.db')
    with pytest.raises(ValueError, match='grid_cap_exceeded'):
        dispatch(db, 'dr-1', 0, ['A', 'B'], 'too-much')
    with pytest.raises(ValueError, match='period_sequence_conflict'):
        dispatch(db, 'dr-1', 1, [], 'out-of-order')
    assert state(db, 'dr-1')[0]['period'] == 0


def test_source_is_required(tmp_path):
    db = tmp_path / 'empty.db'
    initialize(db, FIX / 'dr_config.json')
    with pytest.raises(ValueError, match='demand_response_meter_source_missing'):
        register(db, 'dr-1', FIX / 'dr_case.json')


# --------------------------------------------------------------------------- #
# Exact planner: both pre-authorised contracts
# --------------------------------------------------------------------------- #
def test_planner_min_energy_contract(tmp_path):
    db = prepared(tmp_path, 'me.db')
    rec = plan(db, 'dr-1', 'min_energy', plan_id='plan-me')
    assert rec['status'] == PLAN_PENDING
    payload = evidence_payload(db, 'plan-me')
    periods = _periods(payload)
    assert periods[0]['loads'] == ['A']
    assert periods[1]['loads'] == []
    assert payload['objective']['energy_cents_total'] == 200
    assert payload['objective']['zone_exposure_total'] == {'A': 2, 'B': 4}
    assert payload['objective']['max_zone_exposure'] == 4
    assert payload['objective']['total_exposure'] == 6
    p0, p1 = periods[0], periods[1]
    assert p0['zones']['A']['after_temperature'] == 21
    assert p0['zones']['B']['after_temperature'] == 22
    assert p1['zones']['A']['after_temperature'] == 24
    assert p1['zones']['B']['after_temperature'] == 24
    for entry in periods.values():
        assert entry['shed_kw'] >= 1
        assert entry['grid_kw'] <= entry['grid_cap_kw']
    # Provenance is carried on every plan.
    assert payload['source_summary']['A']['device'] == 'H1'
    assert payload['source_summary']['A']['readings'][0]['source_sha256'] == SHA
    assert payload['case_digest']
    # Optimality proof is internally consistent.
    proof = payload['proof']
    assert proof['feasible_sequences'] >= 1
    assert proof['transitions_tested'] == proof['infeasible_transitions'] + (
        proof['nodes_visited'] - 1
    )


def test_planner_min_max_zone_exposure_contract(tmp_path):
    db = prepared(tmp_path, 'mm.db')
    plan(db, 'dr-1', 'min_max_zone_exposure', plan_id='plan-mm')
    payload = evidence_payload(db, 'plan-mm')
    periods = _periods(payload)
    assert periods[0]['loads'] == ['A']
    assert periods[1]['loads'] == ['B']
    assert payload['objective']['max_zone_exposure'] == 2
    assert payload['objective']['zone_exposure_total'] == {'A': 2, 'B': 2}
    assert payload['objective']['energy_cents_total'] == 1200
    assert periods[1]['zones']['A']['after_temperature'] == 24
    assert periods[1]['zones']['B']['after_temperature'] == 22
    assert periods[1]['zones']['A']['exposure_period'] == 2
    assert periods[1]['zones']['B']['exposure_period'] == 1


def test_planner_is_globally_optimal_by_independent_brute_force(tmp_path):
    """Cross-check the memoised DFS against an independent raw enumeration."""
    db = prepared(tmp_path, 'bf.db')
    doc = json.loads((FIX / 'dr_case.json').read_text())
    zones = doc['zones']
    zids = [z['id'] for z in zones]
    equip = {z['id']: z for z in zones}
    baseline = sum(z['power_kw'] for z in zones)

    def brute(contract):
        best = None
        sequences = 0
        for m0 in range(1 << len(zones)):
            for m1 in range(1 << len(zones)):
                masks = (m0, m1)
                temps = {zid: equip[zid]['initial_temperature'] for zid in zids}
                exposed = {zid: 0 for zid in zids}
                energy = 0
                feasible = True
                for period, mask in enumerate(masks):
                    selected = {zids[i] for i in range(len(zids)) if mask & (1 << i)}
                    window = doc['periods'][period]
                    grid = sum(equip[x]['power_kw'] for x in selected)
                    if grid > window['grid_cap_kw'] or baseline - grid < doc['minimum_shed_kw_each_period']:
                        feasible = False
                        break
                    energy += grid * window['price_cents_per_kwh']
                    for zid in zids:
                        z = equip[zid]
                        after = temps[zid] + window['heat_gain'][zid] - (
                            z['cooling'] if zid in selected else 0)
                        if after < z['minimum_temperature']:
                            feasible = False
                            break
                        exposed[zid] += max(0, after - z['maximum_temperature'])
                        temps[zid] = after
                if not feasible:
                    continue
                if sum(exposed.values()) > doc['max_total_exposure'] or any(
                    v > doc['max_zone_exposure'] for v in exposed.values()
                ):
                    continue
                sequences += 1
                vector = (energy, max(exposed.values())) if contract == 'min_energy' else (
                    max(exposed.values()), energy)
                if best is None or vector < best[0]:
                    best = (vector, masks)
        assert sequences == 4
        return best

    for contract in CONTRACTS:
        rec = plan(db, 'dr-1', contract, plan_id='bf-' + contract)
        payload = evidence_payload(db, rec['plan_id'])
        vector, masks = brute(contract)
        assert tuple(payload['objective']['vector']) == vector
        chosen = tuple(
            sum(1 << zids.index(z) for z in e['loads']) for e in payload['periods']
        )
        assert chosen == masks


def test_exposure_is_monotone_never_reversed_by_cooling(tmp_path):
    """A hotter fixture where late cooling must not erase banked exposure."""
    db = prepared(tmp_path, 'mono.db')
    doc = json.loads((FIX / 'dr_case.json').read_text())
    doc['zones'][0]['initial_temperature'] = 23  # A starts above its 22 cap
    doc['max_total_exposure'] = 100
    doc['max_zone_exposure'] = 100
    outcome, _ = plan_exact(
        doc, 'min_energy',
        {z['id']: z['initial_temperature'] for z in doc['zones']},
        {z['id']: 0 for z in doc['zones']},
    )
    a = [e['zones']['A'] for e in outcome['periods']]
    assert a[0]['exposure_cumulative'] >= a[0]['exposure_period'] > 0
    assert a[1]['exposure_cumulative'] >= a[0]['exposure_cumulative']
    # Cooling A in period 1 still keeps the period-0 exposure on the books.
    assert a[1]['exposure_cumulative'] == a[0]['exposure_cumulative'] + a[1]['exposure_period']


def test_infeasible_case_is_held_without_commands(tmp_path):
    db = prepared(tmp_path, 'inf.db')
    doc_row = connect(db).execute("SELECT payload FROM dr_case WHERE id='dr-1'").fetchone()
    doc = json.loads(doc_row['payload'])
    doc['minimum_shed_kw_each_period'] = 99  # nothing can shed this much
    with connect(db) as w:
        w.execute('UPDATE dr_case SET payload=?,digest=? WHERE id=?',
                  (json.dumps(doc, sort_keys=True), 'forced-infeasible', 'dr-1'))
    with pytest.raises(InfeasiblePlan, match='dr_no_feasible_plan'):
        plan(db, 'dr-1', 'min_energy', plan_id='plan-inf')
    held = connect(db).execute(
        "SELECT status FROM dr_plan WHERE id='plan-inf'").fetchone()
    assert held['status'] == PLAN_HOLD
    assert connect(db).execute(
        'SELECT COUNT(*) AS n FROM dr_command WHERE plan_id=?', ('plan-inf',)
    ).fetchone()['n'] == 0
    # Physical state untouched by planning.
    assert [x['period'] for x in state(db, 'dr-1')] == [0, 0]


def test_zone_exposure_bound_rejects_otherwise_cheap_plans(tmp_path):
    db = prepared(tmp_path, 'cap.db')
    with connect(db) as w:
        row = w.execute("SELECT payload FROM dr_case WHERE id='dr-1'").fetchone()
        doc = json.loads(row['payload'])
        doc['max_zone_exposure'] = 3  # 200-cent plan leaves B at 4, so it is excluded
        w.execute('UPDATE dr_case SET payload=?,digest=? WHERE id=?',
                  (json.dumps(doc, sort_keys=True), 'tight-cap', 'dr-1'))
    # The planner must abandon the cheap trajectory and pick the next feasible one.
    rec = plan(db, 'dr-1', 'min_energy', plan_id='plan-cap')
    payload = evidence_payload(db, rec['plan_id'])
    assert payload['objective']['energy_cents_total'] == 1200
    assert payload['objective']['max_zone_exposure'] == 2
    assert all(
        v <= 3 for v in payload['objective']['zone_exposure_total'].values())
    # A bound below even the best contract leaves an 待核 audit row only.
    with connect(db) as w:
        row = w.execute("SELECT payload FROM dr_case WHERE id='dr-1'").fetchone()
        doc = json.loads(row['payload'])
        doc['max_zone_exposure'] = 1
        w.execute('UPDATE dr_case SET payload=? WHERE id=?',
                  (json.dumps(doc, sort_keys=True), 'dr-1'))
    with pytest.raises(InfeasiblePlan):
        plan(db, 'dr-1', 'min_max_zone_exposure', plan_id='plan-cap3')
    assert connect(db).execute(
        'SELECT COUNT(*) AS n FROM dr_command WHERE plan_id=?', ('plan-cap3',)
    ).fetchone()['n'] == 0


# --------------------------------------------------------------------------- #
# Planned command execution versus physical dispatch
# --------------------------------------------------------------------------- #
def test_execute_plan_drives_real_physical_trajectory(tmp_path):
    db = prepared(tmp_path, 'exec.db')
    plan(db, 'dr-1', 'min_max_zone_exposure', plan_id='plan-x')
    r0 = execute(db, 'plan-x', 0)
    assert r0['status'] == 'applied' and r0['record']['loads'] == ['A']
    r1 = execute(db, 'plan-x', 1)
    assert r1['status'] == 'applied' and r1['record']['loads'] == ['B']
    assert [x['temperature'] for x in state(db, 'dr-1')] == [24, 22]
    payload = evidence_payload(db, 'plan-x')
    assert all(c['status'] == PLAN_DONE and c['action_id'] for c in payload['commands'])
    # Repeating the same execution request must not advance physical periods again.
    again = execute(db, 'plan-x', 0)
    assert again['status'] == 'replayed'
    assert [x['period'] for x in state(db, 'dr-1')] == [2, 2]
    plan_row = connect(db).execute(
        'SELECT status FROM dr_plan WHERE id=?', ('plan-x',)).fetchone()
    assert plan_row['status'] == PLAN_DONE


def test_execute_idempotent_under_duplicate_action_identity(tmp_path):
    db = prepared(tmp_path, 'idem.db')
    plan(db, 'dr-1', 'min_energy', plan_id='plan-i')
    execute(db, 'plan-i', 0, action_id='fixed-a')
    replay = execute(db, 'plan-i', 0, action_id='fixed-a')
    assert replay['status'] == 'replayed'
    actions = connect(db).execute(
        'SELECT COUNT(*) AS n FROM dr_action WHERE id=?', ('fixed-a',)).fetchone()['n']
    assert actions == 1


def test_partial_execution_status(tmp_path):
    db = prepared(tmp_path, 'part.db')
    plan(db, 'dr-1', 'min_energy', plan_id='plan-p')
    execute(db, 'plan-p', 0)
    assert connect(db).execute(
        'SELECT status FROM dr_plan WHERE id=?', ('plan-p',)).fetchone()['status'] == PLAN_PARTIAL


def test_execution_holds_on_equipment_capacity_change_and_temperature_drift(tmp_path):
    db = prepared(tmp_path, 'hold.db')
    plan(db, 'dr-1', 'min_energy', plan_id='plan-h')
    with connect(db) as w:
        row = w.execute("SELECT payload FROM dr_case WHERE id='dr-1'").fetchone()
        doc = json.loads(row['payload'])
        doc['zones'][0]['power_kw'] = 3  # A's equipment rating changed after planning
        w.execute('UPDATE dr_case SET payload=? WHERE id=?',
                  (json.dumps(doc, sort_keys=True), 'dr-1'))
    with pytest.raises(ValueError, match='dr_equipment_capacity_changed'):
        execute(db, 'plan-h', 0)
    assert connect(db).execute(
        "SELECT status FROM dr_command WHERE plan_id='plan-h' AND period=0"
    ).fetchone()['status'] == PLAN_HOLD
    assert [x['period'] for x in state(db, 'dr-1')] == [0, 0]

    db2 = prepared(tmp_path, 'drift.db')
    plan(db2, 'dr-1', 'min_energy', plan_id='plan-d')
    feedback(db2, 'dr-1', 0, 'A', 19.0, SHA, '2026-01-01T00:30:00+00:00')
    # The pending command is held immediately; executing it must never go out.
    with pytest.raises(ValueError, match='dr_command_on_hold'):
        execute(db2, 'plan-d', 0)
    held = connect(db2).execute(
        "SELECT status FROM dr_command WHERE plan_id='plan-d' AND period=0"
    ).fetchone()['status']
    assert held == PLAN_HOLD
    assert [x['period'] for x in state(db2, 'dr-1')] == [0, 0]
    # A stale plan must not be force-sent; a fresh plan absorbs the new origin.
    rec = plan(db2, 'dr-1', 'min_energy', plan_id='plan-d2')
    r = execute(db2, rec['plan_id'], 0)
    assert r['status'] == 'applied'
    # The replanner exploits the cooler A origin and picks the cheaper B load.
    assert r['record']['loads'] == ['B']
    assert r['record']['zones']['A']['temperature'] == 19 + 3
    assert r['record']['zones']['B']['temperature'] == 20 + 2 - 2


def test_execution_holds_when_grid_cap_would_be_breached(tmp_path):
    db = prepared(tmp_path, 'grid.db')
    plan(db, 'dr-1', 'min_energy', plan_id='plan-g')
    # Tamper the case so the planned loads now breach the grid cap.
    with connect(db) as w:
        row = w.execute("SELECT payload FROM dr_case WHERE id='dr-1'").fetchone()
        doc = json.loads(row['payload'])
        doc['periods'][0]['grid_cap_kw'] = 1
        w.execute('UPDATE dr_case SET payload=? WHERE id=?',
                  (json.dumps(doc, sort_keys=True), 'dr-1'))
    with pytest.raises(ValueError, match='dr_equipment_capacity_changed'):
        execute(db, 'plan-g', 0)
    command = connect(db).execute(
        'SELECT status,detail FROM dr_command WHERE plan_id=? AND period=0',
        ('plan-g',)).fetchone()
    assert command['status'] == PLAN_HOLD
    assert json.loads(command['detail'])['hold_reason'] == 'dr_equipment_capacity_changed'
    # Nothing physical moved.
    assert [x['period'] for x in state(db, 'dr-1')] == [0, 0]

    # The dispatch layer itself still enforces the hard cap as defence in depth.
    db2 = prepared(tmp_path, 'grid2.db')
    with pytest.raises(ValueError, match='grid_cap_exceeded'):
        dispatch(db2, 'dr-1', 0, ['A', 'B'], 'raw-overcap')


# --------------------------------------------------------------------------- #
# Late feedback: future-only correction, executed periods immutable
# --------------------------------------------------------------------------- #
def test_late_feedback_does_not_rewrite_executed_periods(tmp_path):
    db = prepared(tmp_path, 'fb.db')
    plan(db, 'dr-1', 'min_energy', plan_id='plan-fb')
    execute(db, 'plan-fb', 0)
    before = {r['id']: r['temperature'] for r in state(db, 'dr-1')}
    note = feedback(db, 'dr-1', 0, 'A', 18.0, SHA, '2026-01-01T01:00:00+00:00')
    assert note['status'] == PLAN_HOLD
    assert note['detail']['code'] == 'late_feedback_immutable'
    after = {r['id']: r['temperature'] for r in state(db, 'dr-1')}
    assert before == after  # closed period untouched
    # The on-time feedback for the current period corrects future origins.
    note = feedback(db, 'dr-1', 1, 'A', 23.0, SHA, '2026-01-01T02:00:00+00:00')
    assert note['status'] == '已接收'
    live = {r['id']: r['temperature'] for r in state(db, 'dr-1')}
    assert live['A'] == 23 and live['B'] == before['B']
    assert note['detail']['remaining_zone_exposure'] == 4 - 0  # A banked 0 in period 0
    # Re-sending identical feedback is a replay, not a second physical correction.
    again = feedback(db, 'dr-1', 1, 'A', 23.0, SHA, '2026-01-01T02:00:00+00:00')
    assert again['replayed'] is True
    assert connect(db).execute(
        'SELECT COUNT(*) AS n FROM dr_feedback WHERE zone_id=?', ('A',)).fetchone()['n'] == 2


def test_feedback_over_budget_holds_pending_commands(tmp_path):
    db = prepared(tmp_path, 'fb2.db')
    plan(db, 'dr-1', 'min_energy', plan_id='plan-fb2')
    execute(db, 'plan-fb2', 0)  # B banks exposure 1 in period 0
    # A high measured temperature at the start of period 1 dooms the remaining budget.
    note = feedback(db, 'dr-1', 1, 'B', 30.0, SHA, '2026-01-01T02:00:00+00:00')
    # Re-plan with the corrected origin: no command can keep B within cap 4.
    with pytest.raises(InfeasiblePlan):
        plan(db, 'dr-1', 'min_energy', plan_id='plan-fb2b')
    held_plan = evidence_payload(db, 'plan-fb2b')
    assert held_plan['status'] == PLAN_HOLD and not held_plan['commands']
    # Executed history stays intact and is reported as done on the held plan context.
    assert [x['period'] for x in state(db, 'dr-1')] == [1, 1]


def test_feedback_rejects_timezone_misalignment_and_bad_sha(tmp_path):
    db = prepared(tmp_path, 'fb3.db')
    note = feedback(db, 'dr-1', 0, 'A', 21.0, SHA, '2026-01-01T01:00:00+05:00')
    assert note['status'] == PLAN_HOLD and note['detail']['code'] == 'dr_timezone_misaligned'
    note = feedback(db, 'dr-1', 0, 'B', 21.0, 'deadbeef', '2026-01-01T01:00:00+00:00')
    assert note['status'] == PLAN_HOLD
    assert note['detail']['code'] == 'demand_response_meter_source_missing'
    with pytest.raises(ValueError, match='timezone_required'):
        feedback(db, 'dr-1', 0, 'A', 21.0, SHA, '2026-01-01T01:00:00')
    # A conflicting second submission for the same period/zone is held, not overwritten.
    conflict = feedback(db, 'dr-1', 0, 'B', 19.0, SHA, '2026-01-01T01:30:00+00:00')
    assert conflict['status'] == PLAN_HOLD
    assert conflict['detail']['code'] == 'dr_feedback_conflict'


def test_register_detects_timezone_misalignment(tmp_path):
    db = prepared(tmp_path, 'tz.db')
    # Replace one reading's timestamp with a different offset; identity changes too,
    # so rebuild the reading rows directly.
    with connect(db) as w:
        w.execute("UPDATE reading SET at=? WHERE device='H2' AND seq=1",
                  ('2026-01-01T05:00:00+05:00',))
    shutil.copy(FIX / 'dr_case.json', tmp_path / 'case2.json')
    with pytest.raises(ValueError, match='dr_timezone_misaligned'):
        register(db, 'dr-2', tmp_path / 'case2.json')


def test_source_sha_conflict_blocks_planning(tmp_path):
    db = prepared(tmp_path, 'sha.db')
    with connect(db) as w:
        w.execute("UPDATE source SET sha256='tampered' WHERE id=1")
    with pytest.raises(ValueError, match='demand_response_meter_source_missing'):
        plan(db, 'dr-1', 'min_energy', plan_id='plan-sha')
    assert connect(db).execute(
        'SELECT COUNT(*) AS n FROM dr_plan').fetchone()['n'] == 0


def test_missing_inputs_are_rejected(tmp_path):
    db = prepared(tmp_path, 'miss.db')
    bad = tmp_path / 'bad.json'
    doc = json.loads((FIX / 'dr_case.json').read_text())
    del doc['zones'][0]['power_kw']
    bad.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='bad_demand_response_case'):
        register(db, 'dr-x', bad)


# --------------------------------------------------------------------------- #
# Local size bound and evidence file / CLI
# --------------------------------------------------------------------------- #
def test_period_without_any_feasible_action_is_held(tmp_path):
    db = prepared(tmp_path, 'noact.db')
    with connect(db) as w:
        row = w.execute("SELECT payload FROM dr_case WHERE id='dr-1'").fetchone()
        doc = json.loads(row['payload'])
        # Even switching everything off sheds only the 3 kW baseline: demanding
        # more leaves zero legal actions in period 1.
        doc['minimum_shed_kw_each_period'] = 4
        w.execute('UPDATE dr_case SET payload=? WHERE id=?',
                  (json.dumps(doc, sort_keys=True), 'dr-1'))
    with pytest.raises(InfeasiblePlan):
        plan(db, 'dr-1', 'min_energy', plan_id='plan-noact')
    proof = json.loads(connect(db).execute(
        "SELECT payload FROM dr_plan WHERE id='plan-noact'").fetchone()['payload'])['proof']
    assert proof['basis'].startswith('every branch pruned')


def test_plan_after_all_periods_executed_is_completed_record(tmp_path):
    db = prepared(tmp_path, 'done.db')
    plan(db, 'dr-1', 'min_max_zone_exposure', plan_id='plan-fin')
    execute(db, 'plan-fin', 0)
    execute(db, 'plan-fin', 1)
    rec = plan(db, 'dr-1', 'min_max_zone_exposure', plan_id='plan-fin2')
    payload = evidence_payload(db, rec['plan_id'])
    assert rec['status'] == PLAN_DONE and payload['periods'] == []
    assert payload['objective']['total_exposure'] == 4


def test_four_zones_eight_periods_exact_optimum_performance(tmp_path):
    import time
    doc = {
        'zones': [
            {'id': f'Z{i}', 'device': ('H1' if i % 2 == 0 else 'H2'),
             'slot': ('A-01' if i % 2 == 0 else 'B-01'),
             'reading_source_sha256': SHA, 'initial_temperature': 20,
             'minimum_temperature': 14, 'maximum_temperature': 22 + i,
             'power_kw': 1 + i % 2, 'cooling': 2}
            for i in range(4)
        ],
        'periods': [
            {'grid_cap_kw': 3, 'price_cents_per_kwh': (100 if p % 2 == 0 else 800),
             'heat_gain': {f'Z{i}': 1 + i % 3 for i in range(4)}}
            for p in range(8)
        ],
        'max_total_exposure': 40, 'max_zone_exposure': 12,
        'minimum_shed_kw_each_period': 1,
        'contracts': list(CONTRACTS),
    }
    fixture = tmp_path / 'case4x8.json'
    fixture.write_text(json.dumps(doc))
    db = tmp_path / 'big.db'
    initialize(db, FIX / 'dr_config.json')
    ingest(FIX / 'dr_readings.csv', db)
    register(db, 'dr-4x8', fixture)
    started = time.monotonic()
    rec = plan(db, 'dr-4x8', 'min_energy', plan_id='plan-4x8')
    elapsed = time.monotonic() - started
    assert elapsed < 10
    payload = evidence_payload(db, rec['plan_id'])
    assert len(payload['periods']) == 8
    proof = payload['proof']
    assert proof['nodes_visited'] > 0 and proof['feasible_sequences'] >= 1
    # Every emitted period honours every hard constraint.
    for entry in payload['periods']:
        assert entry['grid_kw'] <= entry['grid_cap_kw']
        assert entry['shed_kw'] >= entry['minimum_shed_kw']
    assert payload['objective']['total_exposure'] <= 40
    assert payload['objective']['max_zone_exposure'] <= 12


def test_local_scope_limited_to_four_zones_eight_periods(tmp_path):
    doc = json.loads((FIX / 'dr_case.json').read_text())
    doc['zones'] = doc['zones'] * 3  # 6 zones
    bad = tmp_path / 'big.json'
    bad.write_text(json.dumps(doc))
    db = tmp_path / 'big.db'
    initialize(db, FIX / 'dr_config.json')
    with pytest.raises(ValueError, match='dr_case_out_of_local_scope'):
        register(db, 'dr-big', bad)


def test_memoised_search_matches_brute_force_on_random_cases():
    """Dominance memo must never change the exact optimum."""
    import random
    from meterround.demand_response import _enumerate_actions

    rng = random.Random(20261118)
    for _ in range(60):
        n_zones = rng.randint(1, 4)
        n_periods = rng.randint(1, 8)
        zones = []
        for i in range(n_zones):
            cap = rng.randint(20, 24)
            zones.append({
                'id': f'Z{i}', 'device': f'D{i}', 'slot': f'S{i}',
                'reading_source_sha256': 'x',
                'initial_temperature': rng.randint(18, 22),
                'minimum_temperature': 12, 'maximum_temperature': cap,
                'power_kw': rng.randint(1, 3), 'cooling': rng.randint(1, 3),
            })
        periods = []
        for _p in range(n_periods):
            periods.append({
                'grid_cap_kw': rng.randint(0, sum(z['power_kw'] for z in zones)),
                'price_cents_per_kwh': rng.choice((1, 10, 50, 100, 1000)),
                'heat_gain': {z['id']: rng.randint(0, 4) for z in zones},
            })
        doc = {
            'zones': zones, 'periods': periods,
            'max_total_exposure': rng.randint(0, 12),
            'max_zone_exposure': rng.randint(0, 6),
            'minimum_shed_kw_each_period': rng.randint(0, 3),
            'contracts': list(CONTRACTS),
        }
        zids = [z['id'] for z in zones]
        temps0 = {z['id']: z['initial_temperature'] for z in zones}
        exp0 = {z['id']: 0 for z in zones}

        def brute(contract):
            best = None
            def walk(p, temps, expo, spent):
                nonlocal best
                if p == n_periods:
                    if sum(expo) > doc['max_total_exposure'] or any(
                        v > doc['max_zone_exposure'] for v in expo):
                        return
                    vector = (spent, max(expo)) if contract == 'min_energy' else (
                        max(expo), spent)
                    if best is None or vector < best:
                        best = vector
                    return
                window = doc['periods'][p]
                for selected, total_kw in _enumerate_actions(doc, p):
                    nt, ne, ok = [], [], True
                    for i, zid in enumerate(zids):
                        z = next(x for x in zones if x['id'] == zid)
                        after = temps[i] + window['heat_gain'][zid] - (
                            z['cooling'] if zid in selected else 0)
                        if after < z['minimum_temperature']:
                            ok = False
                            break
                        nt.append(after)
                        ne.append(expo[i] + max(0, after - z['maximum_temperature']))
                    if ok:
                        cost = total_kw * window['price_cents_per_kwh']
                        walk(p + 1, tuple(nt), tuple(ne), spent + cost)
            walk(0, tuple(temps0[z] for z in zids), tuple(0 for _ in zids), 0)
            return best

        for contract in CONTRACTS:
            outcome, _proof = plan_exact(doc, contract, temps0, exp0)
            expected = brute(contract)
            if expected is None:
                assert outcome is None
            else:
                assert outcome is not None
                assert tuple(outcome['objective']['vector']) == expected


def test_evidence_json_and_cli(tmp_path):
    db = prepared(tmp_path, 'cli.db')
    from meterround.demand_response import write_evidence
    plan(db, 'dr-1', 'min_energy', plan_id='plan-cli')
    out = tmp_path / 'evidence.json'
    write_evidence(db, 'plan-cli', out)
    payload = json.loads(out.read_text())
    assert payload['contract'] == 'min_energy'
    assert payload['proof']['algorithm'] == 'exhaustive_dfs_dominance_memo'
    assert len(payload['commands']) == 2
    assert payload['source_summary']['B']['slot'] == 'B-01'

    # Offline CLI end-to-end (isolated mode, no network, stdlib only).
    proc = subprocess.run(
        [sys.executable, '-I', str(_RUNNER), 'dr-plan', '--db', str(db),
         '--case-id', 'dr-1', '--contract', 'min_max_zone_exposure',
         '--plan-id', 'plan-cli2'],
        cwd=REPO, env=_ENV,
        capture_output=True, text=True, check=True,
    )
    cli = json.loads(proc.stdout)
    assert cli['status'] == PLAN_PENDING
    proc = subprocess.run(
        [sys.executable, '-I', str(_RUNNER), 'dr-execute', '--db', str(db),
         '--plan-id', 'plan-cli2', '--period', '0'],
        cwd=REPO, env=_ENV,
        capture_output=True, text=True, check=True,
    )
    assert json.loads(proc.stdout)['status'] == 'applied'
    # Replaying through the CLI must not re-advance anything.
    proc = subprocess.run(
        [sys.executable, '-I', str(_RUNNER), 'dr-execute', '--db', str(db),
         '--plan-id', 'plan-cli2', '--period', '0'],
        cwd=REPO, env=_ENV,
        capture_output=True, text=True, check=True,
    )
    assert json.loads(proc.stdout)['status'] == 'replayed'

    # An infeasible plan still lands as an 待核 evidence file, never a command.
    with connect(db) as w:
        row = w.execute("SELECT payload FROM dr_case WHERE id='dr-1'").fetchone()
        doc = json.loads(row['payload'])
        doc['minimum_shed_kw_each_period'] = 99
        w.execute('UPDATE dr_case SET payload=? WHERE id=?',
                  (json.dumps(doc, sort_keys=True), 'dr-1'))
    held_out = tmp_path / 'held.json'
    proc = subprocess.run(
        [sys.executable, '-I', str(_RUNNER), 'dr-plan', '--db', str(db),
         '--case-id', 'dr-1', '--contract', 'min_energy',
         '--plan-id', 'plan-cli-held', '--out', str(held_out)],
        cwd=REPO, env=_ENV, capture_output=True, text=True, check=True,
    )
    assert json.loads(proc.stdout)['status'] == PLAN_HOLD
    held_payload = json.loads(held_out.read_text())
    assert held_payload['status'] == PLAN_HOLD and held_payload['commands'] == []
    assert held_payload['proof']['basis'].startswith('every branch pruned')


def test_settlement_and_green_attribute_tables_untouched(tmp_path):
    db = prepared(tmp_path, 'iso.db')
    before = {r['slot']: dict(r) for r in connect(db).execute('SELECT * FROM decision')}
    plan(db, 'dr-1', 'min_energy', plan_id='plan-iso')
    execute(db, 'plan-iso', 0)
    after = {r['slot']: dict(r) for r in connect(db).execute('SELECT * FROM decision')}
    assert before == after
    readings_before = [dict(r) for r in connect(db).execute('SELECT * FROM reading ORDER BY id')]
    assert len(readings_before) == 4

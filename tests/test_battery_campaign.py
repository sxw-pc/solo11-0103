import json
import sqlite3
import time
from pathlib import Path

import pytest

from meterround.__main__ import battery_run, initialize
from meterround.battery_waste import (
    MAX_BOXES,
    MAX_PACKS,
    CampaignInterrupted,
    _exact_solve,
    campaign_state,
    execute_campaign,
    load,
    plan_campaign,
    process,
    register,
    ship,
    state,
)
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def prepared(tmp_path, name='waste.db'):
    db = tmp_path / name
    initialize(db, FIX / 'battery_config.json')
    assert ingest(FIX / 'battery_readings.csv', db)['imported'] == 4
    assert set(evaluate(db).values()) == {'可结算'}
    register(db, 'waste-1', FIX / 'battery_case.json')
    return db


def snapshot(db, case='waste-1'):
    return {p['id']: p for p in state(db, case)['packs']}


def operations(db):
    con = connect(db)
    try:
        return [dict(r) for r in con.execute(
            'SELECT * FROM battery_operation ORDER BY rowid')]
    finally:
        con.close()


def shipped_operation_count(db, container):
    return sum(1 for row in operations(db)
               if row['kind'] == 'ship' and json.loads(row['result'])['container'] == container)


def processed_operation_count(db, container):
    return sum(1 for row in operations(db)
               if row['kind'] == 'process' and json.loads(row['result'])['container'] == container)


# ---------------------------------------------------------------------------
# Evidence traceability and registration
# ---------------------------------------------------------------------------

def test_register_links_each_pack_to_its_handset_reading_evidence(tmp_path):
    db = prepared(tmp_path)
    packs = snapshot(db)
    for pack_id, device, slot in (('P1', 'H1', 'A-01'), ('P2', 'H2', 'A-02')):
        pack = packs[pack_id]
        assert pack['device'] == device and pack['slot'] == slot
        source = pack['source']
        assert source['source_sha256'] == '9fe83545c36639902213c97f1b9d2567495e9a2e9acc5df4a2270b001eb65f64'
        assert source['reading_count'] == 2
        assert len(source['reading_ids']) == 2
        assert len(set(source['evidence_ids'])) == 2
        con = connect(db)
        try:
            joined = con.execute(
                '''SELECT e.source_sha256,e.payload_sha256,r.sha256,s.sha256,r.device,r.slot
                   FROM battery_pack_evidence e
                   JOIN reading r ON r.id=e.reading_id
                   JOIN evidence ev ON ev.id=e.evidence_id
                   JOIN source s ON s.id=r.source_id
                   WHERE e.pack_id=?''', (pack_id,)).fetchall()
            assert len(joined) == 2
            for row in joined:
                assert row[0] == row[3] == pack['source_sha256']
                assert row[1] == row[2]
                assert row[4] == device and row[5] == slot
        finally:
            con.close()


def test_campaign_does_not_rewrite_meter_usage_or_evidence(tmp_path):
    db = prepared(tmp_path)
    before = evaluate(db)
    execute_campaign(db, 'waste-1', 'maximize_recovery')
    after = evaluate(db)
    assert before == after == {'A-01': '可结算', 'A-02': '可结算'}
    con = connect(db)
    try:
        assert con.execute('SELECT count(*) FROM evidence').fetchone()[0] == 4
        assert con.execute('SELECT count(*) FROM source').fetchone()[0] == 1
    finally:
        con.close()


def test_re_register_same_case_is_idempotent_and_changed_case_conflicts(tmp_path):
    db = prepared(tmp_path)
    again = register(db, 'waste-1', FIX / 'battery_case.json')
    assert again['packs'] == 2
    changed = tmp_path / 'changed_case.json'
    doc = json.loads((FIX / 'battery_case.json').read_text(encoding='utf-8'))
    doc['packs'][0]['chemistry'] = 'LCO'
    changed.write_text(json.dumps(doc), encoding='utf-8')
    with pytest.raises(ValueError, match='battery_case_conflict'):
        register(db, 'waste-1', changed)


# ---------------------------------------------------------------------------
# Exact planning under both contracts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('contract, boxes, recovered, neutralized, shipments', [
    ('maximize_recovery', [('C5', ['P1'], 4, 'recover'), ('C9', ['P2'], 5, 'recover')], 7, 2, 2),
    ('minimize_shipments', [('C9', ['P1', 'P2'], 9, 'thermal')], 0, 9, 1),
])
def test_plan_is_exact_and_contract_specific(tmp_path, contract, boxes, recovered,
                                              neutralized, shipments):
    db = prepared(tmp_path)
    plan = plan_campaign(db, 'waste-1', contract)
    assert plan['status'] == 'planned'
    assert plan['contract'] == contract
    assert [(b['container_id'], b['pack_ids'], b['mass_kg'], b['route'])
            for b in plan['boxes']] == boxes
    assert plan['predicted'] == {
        'recovered_kg': recovered,
        'neutralized_kg': neutralized,
        'mass_kg': 9,
        'shipments': shipments,
    }
    assert plan['optimality']['method'] == 'exhaustive_subset_dp'
    assert plan['optimality']['chosen'] == {'recovered_kg': recovered,
                                            'shipments': shipments}
    assert plan['optimality']['contract_comparison'] == {
        'maximize_recovery': {'recovered_kg': 7, 'shipments': 2},
        'minimize_shipments': {'recovered_kg': 0, 'shipments': 1},
    }


def test_maximize_recovery_breaks_ties_on_fewer_shipments():
    packs = [
        {'id': 'A', 'mass_kg': 1, 'metal_kg': 0, 'chem_index': 0},
        {'id': 'B', 'mass_kg': 1, 'metal_kg': 0, 'chem_index': 0},
    ]
    containers = [{'id': 'X', 'capacity_kg': 2}, {'id': 'Y', 'capacity_kg': 2}]
    result, _ = _exact_solve(packs, containers, 'maximize_recovery')
    assert result[0] == 0 and result[1] == 1


def test_planner_is_exact_within_twelve_packs_and_six_boxes():
    packs = [
        {'id': f'P{i}', 'mass_kg': (i % 5) + 1, 'metal_kg': (i % 3),
         'chem_index': i % 2}
        for i in range(MAX_PACKS)
    ]
    containers = [{'id': f'C{i}', 'capacity_kg': 8} for i in range(MAX_BOXES)]
    started = time.time()
    result, stats = _exact_solve(packs, containers, 'maximize_recovery')
    elapsed = time.time() - started
    assert result is not None
    assert result[1] <= MAX_BOXES
    assert elapsed < 30
    assert stats['states'] > 0 and stats['transitions'] > 0


# ---------------------------------------------------------------------------
# Physical endpoints and capacity guards
# ---------------------------------------------------------------------------

def test_container_exactly_full_and_over_capacity(tmp_path):
    db = prepared(tmp_path)
    load(db, 'waste-1', 'P1', 'C9', 'l1')
    load(db, 'waste-1', 'P2', 'C9', 'l2')
    box = next(c for c in state(db, 'waste-1')['containers'] if c['id'] == 'C9')
    assert box['capacity_kg'] == 9
    assert sum(p['mass_kg'] for p in snapshot(db).values()) == 9
    assert ship(db, 'waste-1', 'C9', 's1')['mass_kg'] == 9
    other = prepared(tmp_path, 'other.db')
    load(other, 'waste-1', 'P1', 'C5', 'l1')
    with pytest.raises(ValueError, match='container_capacity_exceeded'):
        load(other, 'waste-1', 'P2', 'C5', 'l2')


def test_mixed_box_cannot_recover_and_thermal_reports_no_metal(tmp_path):
    db = prepared(tmp_path)
    load(db, 'waste-1', 'P1', 'C9', 'l1')
    load(db, 'waste-1', 'P2', 'C9', 'l2')
    ship(db, 'waste-1', 'C9', 's1')
    with pytest.raises(ValueError, match='mixed_chemistry_cannot_recover'):
        process(db, 'waste-1', 'C9', 'recover', 'p1')
    result = process(db, 'waste-1', 'C9', 'thermal', 'p2')
    assert result['recovered_kg'] == 0
    assert result['neutralized_kg'] == 9
    stored = connect(db)
    try:
        assert stored.execute('SELECT count(*) FROM battery_pack_result '
                              'WHERE route="recover"').fetchone()[0] == 0
    finally:
        stored.close()


def test_endpoint_order_cannot_be_reversed(tmp_path):
    db = prepared(tmp_path)
    with pytest.raises(ValueError, match='container_not_shippable'):
        ship(db, 'waste-1', 'C9', 's0')
    with pytest.raises(ValueError, match='container_not_processable'):
        process(db, 'waste-1', 'C9', 'thermal', 'p0')
    load(db, 'waste-1', 'P1', 'C5', 'l1')
    with pytest.raises(ValueError, match='container_not_processable'):
        process(db, 'waste-1', 'C5', 'recover', 'p0')
    ship(db, 'waste-1', 'C5', 's1')
    with pytest.raises(ValueError, match='unavailable_pack_or_container'):
        load(db, 'waste-1', 'P2', 'C5', 'l2')
    assert process(db, 'waste-1', 'C5', 'recover', 'p1')['recovered_kg'] == 3
    with pytest.raises(ValueError, match='container_not_processable'):
        process(db, 'waste-1', 'C5', 'recover', 'p2')
    with pytest.raises(ValueError, match='container_not_shippable'):
        ship(db, 'waste-1', 'C5', 's2')


def test_duplicate_operation_id_replays_and_conflicting_reuse_is_refused(tmp_path):
    db = prepared(tmp_path)
    assert load(db, 'waste-1', 'P1', 'C5', 'same-op')['status'] == 'loaded'
    replay = load(db, 'waste-1', 'P1', 'C5', 'same-op')
    assert replay['status'] == 'replayed'
    assert replay['record']['pack'] == 'P1'
    with pytest.raises(ValueError, match='operation_id_conflict'):
        load(db, 'waste-1', 'P2', 'C9', 'same-op')
    with pytest.raises(ValueError, match='operation_id_conflict'):
        ship(db, 'waste-1', 'C5', 'same-op')
    assert snapshot(db)['P1']['container'] == 'C5'
    assert snapshot(db)['P2']['container'] is None


# ---------------------------------------------------------------------------
# Crash / restart recovery
# ---------------------------------------------------------------------------

def test_restart_between_two_loads_of_one_box_does_not_repeat(tmp_path):
    db = prepared(tmp_path)
    plan = plan_campaign(db, 'waste-1', 'minimize_shipments')
    assert plan['boxes'][0]['pack_ids'] == ['P1', 'P2']
    with pytest.raises(CampaignInterrupted):
        execute_campaign(db, 'waste-1', 'minimize_shipments', crash_after='box1:load')
    mid = campaign_state(db, 'waste-1', 'campaign:waste-1:minimize_shipments')
    assert mid['campaign']['status'] == 'running'
    packs = snapshot(db)
    assert packs['P1']['container'] == 'C9'
    assert packs['P2']['container'] is None
    assert packs['P1']['location'] == packs['P2']['location'] == 'field'
    loaded = [s for s in mid['steps'] if s['kind'] == 'load' and s['status'] == 'done']
    assert len(loaded) == 1
    result = execute_campaign(db, 'waste-1', 'minimize_shipments')
    assert result['status'] == 'completed'
    assert result['totals']['shipments'] == 1
    assert result['totals']['neutralized_kg'] == 9
    assert shipped_operation_count(db, 'C9') == 1
    assert processed_operation_count(db, 'C9') == 1
    # Every operation id was executed once; the second load was not replayed
    # and P1 was not loaded twice.
    loads = [row for row in operations(db) if row['kind'] == 'load']
    assert len(loads) == 2
    assert len({json.loads(row['result'])['pack'] for row in loads}) == 2


@pytest.mark.parametrize('crash_after', ['box1:ship', 'box1:process'])
def test_restart_after_committed_step_does_not_repeat_physical_work(tmp_path, crash_after):
    db = prepared(tmp_path)
    with pytest.raises(CampaignInterrupted):
        execute_campaign(db, 'waste-1', 'maximize_recovery', crash_after=crash_after)
    if crash_after == 'box1:ship':
        packs = snapshot(db)
        assert packs['P1']['location'] == 'facility'
        assert packs['P1']['disposition'] == 'unprocessed'
    else:
        assert snapshot(db)['P1']['disposition'] == 'recover'
    result = execute_campaign(db, 'waste-1', 'maximize_recovery')
    assert result['status'] == 'completed'
    assert result['totals'] == {
        'mass_kg': 9, 'processed_mass_kg': 9, 'unprocessed_mass_kg': 0,
        'shipped_mass_kg': 9, 'recovered_kg': 7, 'neutralized_kg': 2,
        'loss_kg': 0, 'shipments': 2, 'box_count': 2, 'packs_processed': 2,
        'packs_unprocessed': 0, 'packs_missing': 0, 'missing_mass_kg': 0,
        'recovered_from_mixed_kg': 0, 'unaccounted_mass_kg': 0,
    }
    assert shipped_operation_count(db, 'C5') == 1
    assert shipped_operation_count(db, 'C9') == 1
    assert processed_operation_count(db, 'C5') == 1
    assert processed_operation_count(db, 'C9') == 1
    assert all(p['disposition'] == 'recover' for p in snapshot(db).values())


def test_repaired_source_resumes_partial_campaign_without_reprocessing(tmp_path):
    db = prepared(tmp_path)
    con = connect(db)
    con.execute("UPDATE battery_pack_source SET source_sha256='bad' WHERE pack_id='P2'")
    con.commit()
    con.close()
    first = execute_campaign(db, 'waste-1', 'maximize_recovery')
    assert first['status'] == 'partial'
    assert first['totals']['recovered_kg'] == 3
    con = connect(db)
    # Restore the real digest from the frozen case payload.
    payload = json.loads(con.execute("SELECT payload FROM battery_case WHERE id='waste-1'").fetchone()[0])
    digest = next(p['reading_source_sha256'] for p in payload['packs'] if p['id'] == 'P2')
    con.execute("UPDATE battery_pack_source SET source_sha256=? WHERE pack_id='P2'", (digest,))
    con.commit()
    con.close()
    second = execute_campaign(db, 'waste-1', 'maximize_recovery')
    assert second['status'] == 'completed'
    assert second['totals']['recovered_kg'] == 7
    assert second['totals']['neutralized_kg'] == 2
    assert processed_operation_count(db, 'C5') == 1
    assert shipped_operation_count(db, 'C5') == 1


def test_repaired_capacity_resumes_previously_infeasible_campaign(tmp_path):
    db = prepared(tmp_path)
    con = connect(db)
    con.execute('UPDATE battery_container SET capacity_kg=3')
    con.commit()
    con.close()
    assert execute_campaign(db, 'waste-1', 'maximize_recovery')['status'] == 'infeasible'
    con = connect(db)
    con.execute("UPDATE battery_container SET capacity_kg=CASE id WHEN 'C9' THEN 9 ELSE 5 END")
    con.commit()
    con.close()
    result = execute_campaign(db, 'waste-1', 'maximize_recovery')
    assert result['status'] == 'completed'
    assert result['totals']['recovered_kg'] == 7
    assert result['reason'] is None


def test_contract_must_be_one_of_the_authorised_contracts(tmp_path):
    db = prepared(tmp_path)
    assert json.loads((FIX / 'battery_case.json').read_text(encoding='utf-8'))['contracts'] == [
        'maximize_recovery', 'minimize_shipments']
    with pytest.raises(ValueError, match='bad_battery_contract'):
        plan_campaign(db, 'waste-1', 'cheapest')
    doc = json.loads((FIX / 'battery_case.json').read_text(encoding='utf-8'))
    doc['contracts'] = ['minimize_shipments']
    restricted = tmp_path / 'restricted.json'
    restricted.write_text(json.dumps(doc), encoding='utf-8')
    other = tmp_path / 'restricted.db'
    initialize(other, FIX / 'battery_config.json')
    ingest(FIX / 'battery_readings.csv', other)
    evaluate(other)
    register(other, 'waste-2', restricted)
    with pytest.raises(ValueError, match='battery_contract_not_authorized'):
        plan_campaign(other, 'waste-2', 'maximize_recovery')
    assert plan_campaign(other, 'waste-2', 'minimize_shipments')['status'] == 'planned'


def test_repeating_completed_campaign_replays_without_new_operations(tmp_path):
    db = prepared(tmp_path)
    first = execute_campaign(db, 'waste-1', 'maximize_recovery')
    count = len(first['operations'])
    second = execute_campaign(db, 'waste-1', 'maximize_recovery')
    assert second['replayed'] is True
    assert second['totals'] == first['totals']
    assert len(operations(db)) == count
    assert all(s['status'] in ('done',) for s in
               campaign_state(db, 'waste-1', 'campaign:waste-1:maximize_recovery')['steps'])


# ---------------------------------------------------------------------------
# Isolation and remaining executable plans
# ---------------------------------------------------------------------------

def test_missing_pack_is_isolated_remainder_is_still_delivered(tmp_path):
    db = prepared(tmp_path)
    con = connect(db)
    con.execute("DELETE FROM battery_pack_evidence WHERE pack_id='P1'")
    con.execute("DELETE FROM battery_pack_source WHERE pack_id='P1'")
    con.execute("DELETE FROM battery_pack WHERE id='P1'")
    con.commit()
    con.close()
    result = execute_campaign(db, 'waste-1', 'maximize_recovery')
    assert result['status'] == 'partial'
    assert result['reason'] == 'battery_pack_missing'
    assert [(b['container_id'], b['pack_ids']) for b in result['boxes'] if b['pack_ids']] == [('C5', ['P2'])]
    assert result['totals']['recovered_kg'] == 4
    assert result['totals']['neutralized_kg'] == 1
    assert result['totals']['missing_mass_kg'] == 4
    assert result['totals']['packs_missing'] == 1
    assert result['totals']['recovered_from_mixed_kg'] == 0
    assert {(i['scope'], i['subject'], i['reason']) for i in result['isolated']} == {
        ('pack', 'P1', 'battery_pack_missing')}
    assert result['conservation']['ok'] is True
    assert result['packs'][0]['disposition'] == 'missing'


def test_changed_source_digest_is_isolated_and_not_recovered(tmp_path):
    db = prepared(tmp_path)
    con = connect(db)
    con.execute("UPDATE battery_pack_source SET source_sha256='0'*64 WHERE pack_id='P2'")
    con.commit()
    con.close()
    result = execute_campaign(db, 'waste-1', 'maximize_recovery')
    assert result['status'] == 'partial'
    assert result['reason'] == 'battery_pack_source_changed'
    assert result['totals']['recovered_kg'] == 3
    assert result['totals']['unprocessed_mass_kg'] == 5
    assert result['totals']['recovered_from_mixed_kg'] == 0
    assert {p['id']: p['disposition'] for p in result['packs']} == {
        'P1': 'recover', 'P2': 'unprocessed'}
    assert any(i['subject'] == 'P2' and i['reason'] == 'battery_pack_source_changed'
               for i in result['isolated'])


def test_tampered_source_blob_is_isolated(tmp_path):
    db = prepared(tmp_path)
    con = connect(db)
    con.execute("UPDATE source SET raw=X'00'")
    con.commit()
    con.close()
    result = execute_campaign(db, 'waste-1', 'maximize_recovery')
    assert result['status'] == 'infeasible'
    assert result['reason'] == 'battery_all_packs_isolated'
    assert result['totals']['recovered_kg'] == 0
    assert {i['reason'] for i in result['isolated']} == {'battery_pack_source_changed'}


def test_frozen_pack_mismatch_is_isolated_and_unships_nothing(tmp_path):
    db = prepared(tmp_path)
    con = connect(db)
    con.execute("UPDATE battery_pack SET metal_kg=3.5, mass_kg=4.5 WHERE id='P1'")
    con.commit()
    con.close()
    result = execute_campaign(db, 'waste-1', 'maximize_recovery')
    assert result['status'] == 'partial'
    assert result['reason'] == 'battery_pack_frozen_mismatch'
    assert any(i['subject'] == 'P1' and i['reason'] == 'battery_pack_frozen_mismatch'
               for i in result['isolated'])
    assert result['totals']['recovered_kg'] == 4
    assert result['totals']['mass_kg'] == 9
    assert result['totals']['unprocessed_mass_kg'] == 4
    p1 = next(p for p in result['packs'] if p['id'] == 'P1')
    assert p1['mass_kg'] == 4
    assert p1['ledger'] == {'mass_kg': 4.5, 'metal_kg': 3.5, 'fluid_kg': 1, 'chemistry': 'LFP'}


def test_capacity_shortfall_yields_explicit_no_solution(tmp_path):
    db = prepared(tmp_path)
    con = connect(db)
    con.execute('UPDATE battery_container SET capacity_kg=3')
    con.commit()
    con.close()
    result = execute_campaign(db, 'waste-1', 'maximize_recovery')
    assert result['status'] == 'infeasible'
    assert result['reason'] == 'battery_no_usable_container'
    assert result['totals']['recovered_kg'] == 0
    assert result['totals']['unprocessed_mass_kg'] == 9
    assert {i['reason'] for i in result['isolated']} == {'battery_container_capacity_changed'}
    assert all(b['pack_ids'] == [] for b in result['boxes'])


def test_endpoint_refusal_isolates_only_the_affected_box(tmp_path, monkeypatch):
    db = prepared(tmp_path)
    import meterround.battery_waste as bw
    original = bw.load
    refused = {'done': False}

    def flaky_load(db_path, case_id, pack_id, container_id, operation_id, campaign_id=None):
        if pack_id == 'P1' and not refused['done']:
            refused['done'] = True
            raise ValueError('container_capacity_exceeded')
        return original(db_path, case_id, pack_id, container_id, operation_id, campaign_id)

    monkeypatch.setattr(bw, 'load', flaky_load)
    result = execute_campaign(db, 'waste-1', 'maximize_recovery')
    assert refused['done'] is True
    assert result['status'] == 'partial'
    assert result['reason'].endswith('container_capacity_exceeded')
    # P1 was refused and isolated; P2 is still executable and really shipped.
    remaining = [(b['pack_ids'], b['mass_kg'], b['route']) for b in result['boxes'] if b['pack_ids']]
    assert remaining == [(['P2'], 5, 'recover')]
    assert result['totals']['recovered_kg'] == 4
    assert result['totals']['unprocessed_mass_kg'] == 4
    assert any(i['subject'] == 'P1' and 'container_capacity_exceeded' in i['reason']
               for i in result['isolated'])
    assert any(s['status'] == 'failed' and s['error'] == 'container_capacity_exceeded'
               for s in campaign_state(db, 'waste-1',
                                       'campaign:waste-1:maximize_recovery')['steps'])


# ---------------------------------------------------------------------------
# Conservation, report and CLI
# ---------------------------------------------------------------------------

def test_conservation_is_checked_per_pack_box_and_batch(tmp_path):
    db = prepared(tmp_path)
    result = execute_campaign(db, 'waste-1', 'minimize_shipments')
    conservation = result['conservation']
    assert conservation['ok'] is True
    assert all(check['ok'] for check in conservation['packs'])
    assert all(check['ok'] for check in conservation['boxes'])
    assert conservation['batch']['ok'] is True
    assert conservation['batch']['total_mass_kg'] == 9
    assert conservation['batch']['recovered_kg'] == 0
    assert conservation['batch']['neutralized_kg'] == 9
    assert conservation['batch']['recovered_from_mixed_kg'] == 0
    assert conservation['batch']['unprocessed_not_recovered'] is True
    box = next(b for b in conservation['boxes'] if b['container_id'] == 'C9')
    assert box['mixed_chemistry'] is True and box['route'] == 'thermal'
    assert box['recovered_kg'] == 0


def test_report_matches_actual_endpoint_results_not_plan_quantities(tmp_path):
    db = prepared(tmp_path)
    result = execute_campaign(db, 'waste-1', 'maximize_recovery')
    report = result['report']
    assert report['optimality']['planned']['recovered_kg'] == 7
    assert report['optimality']['actual']['recovered_kg'] == 7
    assert report['optimality']['matches_plan'] is True
    per_box = {box['container_id']: box for box in report['boxes'] if box['pack_ids']}
    assert per_box['C5']['recovered_kg'] == 3
    assert per_box['C5']['neutralized_kg'] == 1
    assert per_box['C9']['recovered_kg'] == 4
    assert per_box['C9']['neutralized_kg'] == 1
    assert all(p['source']['evidence_ids'] for p in report['packs'])
    assert report['packs'][0]['box_no'] == 1
    assert report['packs'][1]['box_no'] == 2
    assert report['packs'][0]['planned_route'] == 'recover'
    assert report['packs'][0]['operation_id'] == next(
        op['id'] for op in report['operations']
        if op['kind'] == 'process' and op['result']['container'] == 'C5')
    assert [step['kind'] for step in report['steps']] == [
        'load', 'ship', 'process', 'load', 'ship', 'process']
    assert all(step['status'] == 'done' for step in report['steps'])
    # A process step can never precede the shipment of its own box.
    kinds = [(step['box_no'], step['kind']) for step in report['steps']]
    for box_no in (1, 2):
        box_steps = [kind for number, kind in kinds if number == box_no]
        assert box_steps == ['load', 'ship', 'process']


def test_cli_battery_flow_writes_json_handover_report(tmp_path):
    db = prepared(tmp_path)
    out = tmp_path / 'handover.json'
    kind, result = battery_run(db, 'waste-1', 'minimize_shipments', out=out)
    assert kind == 'report'
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['contract'] == 'minimize_shipments'
    assert data['totals']['shipments'] == 1
    assert data['totals']['neutralized_kg'] == 9
    assert data['totals']['recovered_kg'] == 0
    assert data['conservation']['ok'] is True
    assert data['campaign']['status'] == 'completed'
    assert data['operations'] and data['steps']
    assert {p['id'] for p in data['packs']} == {'P1', 'P2'}
    # A second CLI run over the same campaign is a replay, not a second shipment.
    kind, replay = battery_run(db, 'waste-1', 'minimize_shipments', out=out)
    data2 = json.loads(out.read_text(encoding='utf-8'))
    assert data2['totals']['shipments'] == 1
    assert shipped_operation_count(db, 'C9') == 1


def test_old_database_is_migrated_without_losing_rows(tmp_path):
    db = tmp_path / 'legacy.db'
    con = sqlite3.connect(db)
    con.executescript(
        '''CREATE TABLE battery_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
           CREATE TABLE battery_pack(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,
               chemistry TEXT NOT NULL,mass_kg REAL NOT NULL,metal_kg REAL NOT NULL,
               fluid_kg REAL NOT NULL,container TEXT,location TEXT NOT NULL,
               disposition TEXT NOT NULL);
           CREATE TABLE battery_container(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,
               capacity_kg REAL NOT NULL,location TEXT NOT NULL);
           CREATE TABLE battery_operation(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,
               kind TEXT NOT NULL,result TEXT NOT NULL);
           INSERT INTO battery_case VALUES ('old','{}','digest');
           INSERT INTO battery_pack VALUES ('P0','old','LFP',4,3,1,NULL,'field','unprocessed');''')
    con.commit()
    con.close()
    con = connect(db)
    try:
        columns = {row[1] for row in con.execute('PRAGMA table_info(battery_pack)')}
        assert {'device', 'slot', 'source_sha256', 'reading_id', 'isolated_reason'} <= columns
        assert con.execute('SELECT count(*) FROM battery_pack').fetchone()[0] == 1
        assert con.execute('SELECT count(*) FROM battery_plan').fetchone()[0] == 0
    finally:
        con.close()

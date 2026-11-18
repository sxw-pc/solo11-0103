import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from meterround import report as report_module
from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.report import (
    export_external_bundle,
    publish_disclosures,
    recover_disclosures,
    report,
    uniquely_revealed_slots,
)
from meterround.rules import evaluate
from meterround.store import connect

RUN = Path(__file__).resolve().parent.parent
FIX = RUN / 'fixtures'
EXTERNAL_KEYS = {'batch', 'version', 'publication_id', 'slots', 'h2_terminal_slot_count'}
FORBIDDEN_TOKENS = ('H1-', 'H2-', 'M-A', '"device"', '"meter"', '"raw"', '"source"', '"usage"', '"reading"')


# ------------------------------------------------------------------ helpers
def build_db(tmp_path, h2map, name='round.db'):
    """按表位→是否 H2 的映射造一个可结算批次；终端前缀与真实采录一致。"""
    slots = list(h2map)
    config = {
        'batch': [{'id': 'b', 'start': '2026-01-01T00:00:00+00:00', 'end': '2026-02-01T00:00:00+00:00',
                   'window_hours': 24, 'spike_limit': 10000}],
        'slot': [{'id': slot, 'batch_id': 'b'} for slot in slots],
        'assignment': [{'slot': slot, 'meter': f'M-{slot}', 'start': '2025-01-01T00:00:00+00:00',
                        'end': '2027-01-01T00:00:00+00:00', 'capacity': 10000} for slot in slots],
        'multiplier': [{'slot': slot, 'start': '2025-01-01T00:00:00+00:00',
                        'end': '2027-01-01T00:00:00+00:00', 'value': 1} for slot in slots],
        'replacement': [],
    }
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps(config), encoding='utf-8')
    lines = ['device,seq,slot,meter,at,value']
    seq = 1
    for slot, is_h2 in h2map.items():
        device = f'H{2 if is_h2 else 1}-{slot}'
        lines.append(f'{device},1,{slot},M-{slot},2026-01-01T00:00:00+00:00,100')
        lines.append(f'{device},2,{slot},M-{slot},2026-02-01T00:00:00+00:00,150')
        seq += 1
    csv = tmp_path / 'readings.csv'
    csv.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    db = tmp_path / name
    initialize(db, cfg)
    assert ingest(csv, db)['imported'] == 2 * len(slots)
    evaluate(db)
    return db


def requests_file(tmp_path, name, *, audience, universe, allowed=(), previous=None,
                  new=None, extra=(), previous_extra=(), policies=('deny_unsafe', 'minimum_safe_coarsening')):
    data = {'audience': audience, 'metric': 'H2_terminal_slot_count', 'universe': list(universe),
            'allowed_expansion': list(allowed), 'policies': list(policies)}
    if previous is not None:
        data['previous_release'] = previous
    if new is not None:
        data['new_request'] = new
    if extra:
        data['requests'] = list(extra)
    if previous_extra:
        data['previous_releases'] = list(previous_extra)
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def published_files(outdir):
    return sorted(Path(outdir).glob('*.json'))


def publication_rows(db, audience=None):
    con = connect(db)
    try:
        rows = con.execute('SELECT * FROM disclosure_publication' + ('' if audience is None else ' WHERE audience=?'),
                           () if audience is None else (audience,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def refusal_rows(db, audience=None):
    con = connect(db)
    try:
        rows = con.execute('SELECT * FROM disclosure_refusal' + ('' if audience is None else ' WHERE audience=?'),
                           () if audience is None else (audience,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ------------------------------------------------- fixture-level publication
def test_internal_report_keeps_full_evidence_but_external_output_does_not(tmp_path):
    db = tmp_path / 'round.db'
    initialize(db, FIX / 'terminal_disclosure_config.json')
    ingest(FIX / 'terminal_disclosure_readings.csv', db)
    evaluate(db)
    internal = json.loads(report(db, tmp_path / 'internal.json').read_text(encoding='utf-8'))
    assert {'H2-A1', 'H1-A2', 'H2-A3', 'H1-A4'} <= {r['device'] for r in internal['reading']}
    assert all('raw' in r and r['raw'] for r in internal['reading'])
    assert internal['source'][0]['name'].endswith('terminal_disclosure_readings.csv')

    result = publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', tmp_path / 'out', 'minimum_safe_coarsening')
    assert result['results'][0]['status'] == 'published'
    files = published_files(tmp_path / 'out')
    assert len(files) == 1
    text = files[0].read_text(encoding='utf-8')
    payload = json.loads(text)
    assert set(payload) == EXTERNAL_KEYS
    assert payload['slots'] == ['A1', 'A2', 'A3', 'A4']
    assert payload['h2_terminal_slot_count'] == 2
    assert payload['batch'] == 'terminal-round'
    for token in FORBIDDEN_TOKENS:
        assert token not in text


def test_deny_unsafe_refuses_because_rel01_plus_req02_names_a3(tmp_path):
    db = tmp_path / 'round.db'
    initialize(db, FIX / 'terminal_disclosure_config.json')
    ingest(FIX / 'terminal_disclosure_readings.csv', db)
    evaluate(db)
    out = tmp_path / 'out'
    result = publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', out, 'deny_unsafe')
    assert result['results'] == [{'request_id': 'REQ-02', 'status': 'refused', 'policy': 'deny_unsafe', 'reason': 'inference'}]
    assert published_files(out) == []
    assert not out.exists() or not any(out.iterdir())
    refusals = refusal_rows(db)
    assert len(refusals) == 1 and refusals[0]['request_id'] == 'REQ-02'
    assert 'A3' in refusals[0]['detail']  # 内部审计可复核被推出的表位
    # 拒绝不会进入外部可见知识：该受众台账里只有已发布的历史 REL-01。
    assert [r['publication_id'] for r in publication_rows(db, 'external-contractor')] == ['REL-01']
    # 拒绝重试幂等，不重复记账。
    again = publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', out, 'deny_unsafe')
    assert again['results'][0]['status'] == 'refused'
    assert len(refusal_rows(db)) == 1


def test_minimum_safe_coarsening_expands_to_four_slots_and_records_actual_set(tmp_path):
    db = tmp_path / 'round.db'
    initialize(db, FIX / 'terminal_disclosure_config.json')
    ingest(FIX / 'terminal_disclosure_readings.csv', db)
    evaluate(db)
    result = publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', tmp_path / 'out', 'minimum_safe_coarsening')
    item = result['results'][0]
    assert item['status'] == 'published' and item['expanded'] is True
    assert item['slots'] == ['A1', 'A2', 'A3', 'A4'] and item['h2_terminal_slot_count'] == 2
    rows = publication_rows(db)
    assert len(rows) == 2  # REL-01 历史 + REQ-02 实际发布
    published = [r for r in rows if r['request_id'] == 'REQ-02'][0]
    assert json.loads(published['slots']) == ['A1', 'A2', 'A3', 'A4']  # 台账写实际发布集合
    assert published['status'] == 'published'
    # 历史 REL-01 也必须独立落账。
    rows = [r for r in publication_rows(db) if r['publication_id'] == 'REL-01']
    assert len(rows) == 1 and json.loads(rows[0]['slots']) == ['A1', 'A2'] and rows[0]['h2_count'] == 1
    # 承包方仍只能知道 A3/A4 中一处是 H2。
    contractors_view = uniquely_revealed_slots(
        [{'id': 'REL-01', 'slots': ['A1', 'A2'], 'count': 1},
         {'id': 'REQ-02', 'slots': ['A1', 'A2', 'A3', 'A4'], 'count': 2}],
        ['A1', 'A2', 'A3', 'A4'], {'A1': 1, 'A2': 0, 'A3': 1, 'A4': 0})
    assert contractors_view == []


def test_retry_same_request_id_does_not_publish_twice(tmp_path):
    db = tmp_path / 'round.db'
    initialize(db, FIX / 'terminal_disclosure_config.json')
    ingest(FIX / 'terminal_disclosure_readings.csv', db)
    evaluate(db)
    first = publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', tmp_path / 'out1', 'minimum_safe_coarsening')
    second = publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', tmp_path / 'out2', 'minimum_safe_coarsening')
    assert first['results'][0]['publication_id'] == second['results'][0]['publication_id']
    assert second['results'][0]['status'] == 'already_published'
    rows = [r for r in publication_rows(db) if r['request_id'] == 'REQ-02']
    assert len(rows) == 1
    assert len(published_files(tmp_path / 'out1')) == 1
    assert published_files(tmp_path / 'out2') == []  # 不重复发布，也不在新目录投放第二份


def test_dry_run_leaves_no_ledger_or_file(tmp_path):
    db = tmp_path / 'round.db'
    initialize(db, FIX / 'terminal_disclosure_config.json')
    ingest(FIX / 'terminal_disclosure_readings.csv', db)
    evaluate(db)
    result = publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', tmp_path / 'out', 'minimum_safe_coarsening', dry_run=True)
    assert result['dry_run'] is True and result['results'][0]['status'] == 'published'
    assert publication_rows(db) == [] and refusal_rows(db) == []
    assert not (tmp_path / 'out').exists()


# ------------------------------------------------------- inference analysis
def test_three_overlapping_groups_solve_every_slot():
    h2 = {'A1': 1, 'A2': 0, 'A3': 1, 'A4': 0}
    equations = [{'slots': ['A1', 'A2', 'A3'], 'count': 2},
                 {'slots': ['A1', 'A2', 'A4'], 'count': 1},
                 {'slots': ['A2', 'A3', 'A4'], 'count': 1}]
    assert uniquely_revealed_slots(equations, list(h2), h2) == ['A1', 'A2', 'A3', 'A4']


def test_pairwise_size_two_groups_can_still_name_one_slot():
    h2 = {'A1': 1, 'A2': 0, 'A3': 0}
    equations = [{'slots': ['A1', 'A2'], 'count': 1},
                 {'slots': ['A2', 'A3'], 'count': 0},
                 {'slots': ['A1', 'A3'], 'count': 1}]
    assert uniquely_revealed_slots(equations, list(h2), h2) == ['A1', 'A2', 'A3']


def test_single_two_slot_group_does_not_reveal_and_extremes_do():
    h2 = {'A1': 1, 'A2': 0, 'A3': 0}
    assert uniquely_revealed_slots([{'slots': ['A1', 'A2'], 'count': 1}], list(h2), h2) == []
    assert uniquely_revealed_slots([{'slots': ['A2', 'A3'], 'count': 0}], list(h2), h2) == ['A2', 'A3']
    h2_all = {'A1': 1, 'A2': 1}
    assert uniquely_revealed_slots([{'slots': ['A1', 'A2'], 'count': 2}], list(h2_all), h2_all) == ['A1', 'A2']
    single = {'A1': 1}
    assert uniquely_revealed_slots([{'slots': ['A1'], 'count': 1}], list(single), single) == ['A1']


def test_inconsistent_history_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        uniquely_revealed_slots([{'slots': ['A1'], 'count': 1}, {'slots': ['A1'], 'count': 0}],
                                ['A1'], {'A1': 1})


def test_zero_count_request_is_treated_as_extreme_and_expanded(tmp_path):
    db = build_db(tmp_path, {'A1': 1, 'A2': 0, 'A3': 0, 'A4': 1})
    req = requests_file(tmp_path, 'req.json', audience='ext', universe=['A1', 'A2', 'A3', 'A4'],
                        allowed=['A4'], new={'id': 'ZERO', 'slots': ['A2', 'A3']})
    denied = publish_disclosures(db, req, tmp_path / 'deny', 'deny_unsafe')
    assert denied['results'][0]['status'] == 'refused'
    allowed = publish_disclosures(db, req, tmp_path / 'min', 'minimum_safe_coarsening')
    item = allowed['results'][0]
    assert item['status'] == 'published' and item['slots'] == ['A2', 'A3', 'A4']
    assert item['h2_terminal_slot_count'] == 1


def test_all_one_request_is_extreme_and_expanded(tmp_path):
    db = build_db(tmp_path, {'A1': 1, 'A2': 1, 'A3': 0})
    req = requests_file(tmp_path, 'req.json', audience='ext', universe=['A1', 'A2', 'A3'],
                        allowed=['A3'], new={'id': 'ONE', 'slots': ['A1', 'A2']})
    assert publish_disclosures(db, req, tmp_path / 'deny', 'deny_unsafe')['results'][0]['status'] == 'refused'
    item = publish_disclosures(db, req, tmp_path / 'min', 'minimum_safe_coarsening')['results'][0]
    assert item['slots'] == ['A1', 'A2', 'A3'] and item['h2_terminal_slot_count'] == 2


# ----------------------------------------------------- minimal coarsening
def test_expansion_picks_smallest_safe_addition_not_first_candidate(tmp_path):
    # REL-01={A1,A2}=1 已发布。加入 A4 会让 {A3,A4} 全为 H2 而被推出；
    # 加入 A5（H1）只给出 {A3,A5} 中一处 H2，是合法的最小扩展。
    h2 = {'A1': 1, 'A2': 0, 'A3': 1, 'A4': 1, 'A5': 0}
    db = build_db(tmp_path, h2)
    req = requests_file(tmp_path, 'req.json', audience='ext', universe=list(h2),
                        allowed=['A4', 'A5'], previous={'id': 'REL-01', 'slots': ['A1', 'A2'], 'count': 1},
                        new={'id': 'REQ-02', 'slots': ['A1', 'A2', 'A3']})
    item = publish_disclosures(db, req, tmp_path / 'out', 'minimum_safe_coarsening')['results'][0]
    assert item['status'] == 'published'
    assert item['slots'] == ['A1', 'A2', 'A3', 'A5']  # 跳过候选 A4，选择安全的 A5
    assert item['h2_terminal_slot_count'] == 2
    assert item['added'] == ['A5']


def test_expansion_tries_larger_combinations_when_single_ones_fail(tmp_path):
    # 历史 {A,E}=1、{A,F}=1 已发布。请求 {A,B}（=0）单独加入任何候选都会
    # 新增可识别表位；只有同时加入 E、F 才抵消。
    h2 = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'E': 1, 'F': 1}
    db = build_db(tmp_path, h2)
    req = requests_file(tmp_path, 'req.json', audience='ext', universe=list(h2),
                        allowed=['E', 'F'],
                        previous={'id': 'REL-A', 'slots': ['A', 'E'], 'count': 1},
                        new={'id': 'REQ-X', 'slots': ['A', 'B']},
                        previous_extra=[{'id': 'REL-C', 'slots': ['A', 'F'], 'count': 1}])
    item = publish_disclosures(db, req, tmp_path / 'out', 'minimum_safe_coarsening')['results'][0]
    assert item['status'] == 'published'
    assert item['added'] == ['E', 'F']
    assert item['slots'] == ['A', 'B', 'E', 'F']
    assert item['h2_terminal_slot_count'] == 2
    assert uniquely_revealed_slots(
        [{'slots': ['A', 'E'], 'count': 1}, {'slots': ['A', 'F'], 'count': 1},
         {'slots': ['A', 'B', 'E', 'F'], 'count': 2}], list(h2), h2) == []


def test_prior_release_that_already_names_a_slot_does_not_block_new_safe_release(tmp_path):
    # {A,B}=0 是受众既有知识，A、B 已被历史推出；新请求 {C,D}=1 相对历史不新增
    # 任何可识别表位，应当允许发布，而不是因为“受众已知 A、B”而拒绝。
    h2 = {'A': 0, 'B': 0, 'C': 1, 'D': 0}
    db = build_db(tmp_path, h2)
    req = requests_file(tmp_path, 'req.json', audience='ext', universe=list(h2), allowed=[],
                        previous={'id': 'REL-01', 'slots': ['A', 'B'], 'count': 0},
                        new={'id': 'REQ-02', 'slots': ['C', 'D']})
    item = publish_disclosures(db, req, tmp_path / 'out', 'deny_unsafe')['results'][0]
    assert item['status'] == 'published'
    assert item['slots'] == ['C', 'D'] and item['h2_terminal_slot_count'] == 1


def test_no_safe_expansion_refuses_instead_of_compounding(tmp_path):
    # 历史 {A,B}=1 已发布，请求 {A,C}（=0）会新增推出 C；允许集合只有 D，
    # 加入 D 也仍推出 C，没有安全扩展，必须拒绝而不是继续加表位。
    h2 = {'A': 0, 'B': 1, 'C': 0, 'D': 0}
    db = build_db(tmp_path, h2)
    req = requests_file(tmp_path, 'req.json', audience='ext', universe=list(h2),
                        allowed=['D'], previous={'id': 'REL-01', 'slots': ['A', 'B'], 'count': 1},
                        new={'id': 'REQ-02', 'slots': ['A', 'C']})
    item = publish_disclosures(db, req, tmp_path / 'out', 'minimum_safe_coarsening')['results'][0]
    assert item['status'] == 'refused' and item['reason'] == 'no_safe_expansion'
    assert published_files(tmp_path / 'out') == []


def test_no_allowed_expansion_refuses_instead_of_leaking(tmp_path):
    h2 = {'A1': 1, 'A2': 0, 'A3': 1}
    db = build_db(tmp_path, h2)
    req = requests_file(tmp_path, 'req.json', audience='ext', universe=list(h2), allowed=[],
                        previous={'id': 'REL-01', 'slots': ['A1', 'A2'], 'count': 1},
                        new={'id': 'REQ-02', 'slots': ['A1', 'A2', 'A3']})
    item = publish_disclosures(db, req, tmp_path / 'out', 'minimum_safe_coarsening')['results'][0]
    assert item['status'] == 'refused' and item['reason'] == 'no_safe_expansion'
    assert published_files(tmp_path / 'out') == []


# ------------------------------------------------------ audience isolation
def test_audience_isolation_uses_only_own_history_and_refusals(tmp_path):
    h2 = {'A1': 1, 'A2': 0, 'A3': 1}
    db = build_db(tmp_path, h2)
    unsafe = requests_file(tmp_path, 'a.json', audience='contractor-a', universe=list(h2), allowed=[],
                           previous={'id': 'REL-A', 'slots': ['A1', 'A2'], 'count': 1},
                           new={'id': 'REQ-A', 'slots': ['A1', 'A2', 'A3']})
    fresh = requests_file(tmp_path, 'b.json', audience='contractor-b', universe=list(h2), allowed=[],
                          new={'id': 'REQ-A', 'slots': ['A1', 'A2', 'A3']})
    a = publish_disclosures(db, unsafe, tmp_path / 'out-a', 'deny_unsafe')['results'][0]
    b = publish_disclosures(db, fresh, tmp_path / 'out-b', 'deny_unsafe')['results'][0]
    assert a['status'] == 'refused'
    assert b['status'] == 'published'
    assert json.loads(published_files(tmp_path / 'out-b')[0].read_text())['slots'] == ['A1', 'A2', 'A3']
    rows_a = publication_rows(db, 'contractor-a')
    rows_b = publication_rows(db, 'contractor-b')
    assert [r['publication_id'] for r in rows_a] == ['REL-A']
    assert [r['publication_id'] for r in rows_b] == [b['publication_id']]
    # A 的拒绝知识不影响 B，B 的发布也不进入 A。
    assert [r['request_id'] for r in refusal_rows(db, 'contractor-a')] == ['REQ-A']
    assert refusal_rows(db, 'contractor-b') == []


# ------------------------------------------------------------- concurrency
def test_concurrent_individually_safe_reports_are_serialised_and_only_one_ships(tmp_path):
    # Q1={A1,A2}=1 与 Q2={A1,A2,A3}=2 单独看都安全，合在一起推出 A3 为 H2。
    h2 = {'A1': 1, 'A2': 0, 'A3': 1}
    db = build_db(tmp_path, h2)
    q1 = requests_file(tmp_path, 'q1.json', audience='contractor', universe=list(h2),
                       new={'id': 'Q1', 'slots': ['A1', 'A2']})
    q2 = requests_file(tmp_path, 'q2.json', audience='contractor', universe=list(h2),
                       new={'id': 'Q2', 'slots': ['A1', 'A2', 'A3']})
    barrier = threading.Barrier(2)

    def run(path, out):
        barrier.wait()
        return publish_disclosures(db, path, out, 'minimum_safe_coarsening')['results'][0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(run, q1, tmp_path / 'o1'), pool.submit(run, q2, tmp_path / 'o2')]]
    statuses = sorted(r['status'] for r in results)
    assert statuses == ['published', 'refused'], results
    rows = publication_rows(db, 'contractor')
    assert len(rows) == 1
    assert len(refusal_rows(db, 'contractor')) == 1
    shipped = [r for r in results if r['status'] == 'published'][0]
    shipped_dir = Path(shipped['file']).parent
    other_dir = tmp_path / ('o2' if shipped_dir == tmp_path / 'o1' else 'o1')
    assert len(published_files(shipped_dir)) == 1
    assert published_files(other_dir) == []  # 只有安全者交付
    assert json.loads(Path(shipped['file']).read_text())['slots'] == shipped['slots']


# -------------------------------------------------- interruption / recovery
def test_same_legacy_release_id_is_booked_independently_per_audience(tmp_path):
    # 两个受众各自拿到过 ID 同为 REL-01 但集合不同的历史发布，必须分别落账。
    h2 = {'A1': 1, 'A2': 0, 'A3': 1, 'A4': 0}
    db = build_db(tmp_path, h2)
    first = requests_file(tmp_path, 'first.json', audience='auditor-x', universe=list(h2),
                          previous={'id': 'REL-01', 'slots': ['A1', 'A2'], 'count': 1})
    second = requests_file(tmp_path, 'second.json', audience='auditor-y', universe=list(h2),
                           previous={'id': 'REL-01', 'slots': ['A3', 'A4'], 'count': 1})
    publish_disclosures(db, first, tmp_path / 'o1', 'deny_unsafe')
    publish_disclosures(db, second, tmp_path / 'o2', 'deny_unsafe')
    x = publication_rows(db, 'auditor-x')
    y = publication_rows(db, 'auditor-y')
    assert len(x) == 1 and len(y) == 1
    assert json.loads(x[0]['slots']) == ['A1', 'A2'] and x[0]['h2_count'] == 1
    assert json.loads(y[0]['slots']) == ['A3', 'A4'] and y[0]['h2_count'] == 1


def test_accepted_publication_cannot_be_relabelled_by_a_different_request(tmp_path):
    # 重试必须幂等：同一请求 ID 换一套表位重交，仍返回原发布，不追加第二份。
    h2 = {'A1': 1, 'A2': 0, 'A3': 1, 'A4': 0}
    db = build_db(tmp_path, h2)
    first = requests_file(tmp_path, 'r1.json', audience='c', universe=list(h2),
                          new={'id': 'REQ-9', 'slots': ['A1', 'A2']})
    second = requests_file(tmp_path, 'r2.json', audience='c', universe=list(h2),
                           new={'id': 'REQ-9', 'slots': ['A3', 'A4']})
    one = publish_disclosures(db, first, tmp_path / 'o1', 'deny_unsafe')['results'][0]
    two = publish_disclosures(db, second, tmp_path / 'o2', 'deny_unsafe')['results'][0]
    assert one['publication_id'] == two['publication_id']
    assert two['status'] == 'already_published'
    assert json.loads(publication_rows(db, 'c')[0]['slots']) == ['A1', 'A2']
    assert published_files(tmp_path / 'o2') == []


def test_publication_digest_is_verifiable_from_ledger_alone(tmp_path):
    db = tmp_path / 'round.db'
    initialize(db, FIX / 'terminal_disclosure_config.json')
    ingest(FIX / 'terminal_disclosure_readings.csv', db)
    evaluate(db)
    publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', tmp_path / 'out', 'minimum_safe_coarsening')
    row = [r for r in publication_rows(db) if r['request_id'] == 'REQ-02'][0]
    import hashlib
    assert hashlib.sha256(row['content'].encode('utf-8')).hexdigest() == row['file_sha256']
    assert Path(row['file_path']).read_bytes() == row['content'].encode('utf-8')
    assert row['h2_count'] == 2 and json.loads(row['slots']) == ['A1', 'A2', 'A3', 'A4']


def test_pending_ledger_row_without_file_is_recovered(tmp_path, monkeypatch):
    db = tmp_path / 'round.db'
    initialize(db, FIX / 'terminal_disclosure_config.json')
    ingest(FIX / 'terminal_disclosure_readings.csv', db)
    evaluate(db)
    real_write = report_module._atomic_write

    def fail_write(path, data):
        raise OSError('simulated interruption before delivery')

    monkeypatch.setattr(report_module, '_atomic_write', fail_write)
    with pytest.raises(OSError):
        publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', tmp_path / 'out', 'minimum_safe_coarsening')
    rows = [r for r in publication_rows(db) if r['request_id'] == 'REQ-02']
    assert len(rows) == 1 and rows[0]['status'] == 'pending'
    assert published_files(tmp_path / 'out') == []

    monkeypatch.setattr(report_module, '_atomic_write', real_write)
    recovered = recover_disclosures(db, tmp_path / 'out')
    assert any(a['action'] == 'published' for a in recovered['recovered'])
    files = published_files(tmp_path / 'out')
    assert len(files) == 1
    import hashlib
    assert hashlib.sha256(files[0].read_bytes()).hexdigest() == rows[0]['file_sha256']
    assert [r for r in publication_rows(db) if r['request_id'] == 'REQ-02'][0]['status'] == 'published'


def test_recovery_repairs_tampered_or_missing_delivery_bytes(tmp_path):
    db = tmp_path / 'round.db'
    initialize(db, FIX / 'terminal_disclosure_config.json')
    ingest(FIX / 'terminal_disclosure_readings.csv', db)
    evaluate(db)
    result = publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', tmp_path / 'out', 'minimum_safe_coarsening')
    path = Path(result['results'][0]['file'])
    expected = path.read_bytes()
    import hashlib
    digest = hashlib.sha256(expected).hexdigest()

    path.write_bytes(b'{"tampered":true}\n')
    assert hashlib.sha256(path.read_bytes()).hexdigest() != digest
    actions = recover_disclosures(db, tmp_path / 'out')['recovered']
    assert any(a['action'] == 'restored' for a in actions)
    assert path.read_bytes() == expected and hashlib.sha256(path.read_bytes()).hexdigest() == digest

    path.unlink()
    recover_disclosures(db, tmp_path / 'out')
    assert path.read_bytes() == expected
    # 再恢复一次是幂等的，不产生重复发布。
    recover_disclosures(db, tmp_path / 'out')
    assert len([r for r in publication_rows(db) if r['request_id'] == 'REQ-02']) == 1


def test_ledger_digest_mismatch_blocks_delivery(tmp_path):
    db = tmp_path / 'round.db'
    initialize(db, FIX / 'terminal_disclosure_config.json')
    ingest(FIX / 'terminal_disclosure_readings.csv', db)
    evaluate(db)
    result = publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', tmp_path / 'out', 'minimum_safe_coarsening')
    con = connect(db)
    con.execute("UPDATE disclosure_publication SET content='{}' WHERE publication_id=?", (result['results'][0]['publication_id'],))
    con.commit()
    con.close()
    with pytest.raises(ValueError):
        recover_disclosures(db, tmp_path / 'out')
    with pytest.raises(ValueError):
        export_external_bundle(db, tmp_path / 'copy.json', result['results'][0]['publication_id'])


def test_export_external_bundle_replays_exact_ledger_bytes(tmp_path):
    db = tmp_path / 'round.db'
    initialize(db, FIX / 'terminal_disclosure_config.json')
    ingest(FIX / 'terminal_disclosure_readings.csv', db)
    evaluate(db)
    result = publish_disclosures(db, FIX / 'terminal_disclosure_requests.json', tmp_path / 'out', 'minimum_safe_coarsening')
    original = Path(result['results'][0]['file']).read_bytes()
    exported = export_external_bundle(db, tmp_path / 'copy.json', result['results'][0]['publication_id'])
    assert Path(exported['path']).read_bytes() == original
    assert exported['sha256'] == result['results'][0]['sha256']


# ----------------------------------------------------------------- CLI path
def test_cli_publish_and_recover(tmp_path):
    db = tmp_path / 'round.db'
    initialize(db, FIX / 'terminal_disclosure_config.json')
    ingest(FIX / 'terminal_disclosure_readings.csv', db)
    evaluate(db)
    env = dict(os.environ, PYTHONPATH=str(RUN) + os.pathsep + os.environ.get('PYTHONPATH', ''))
    cmd = [sys.executable, '-B', '-m', 'meterround', 'publish-disclosures', '--db', str(db),
           '--requests', str(FIX / 'terminal_disclosure_requests.json'), '--out', str(tmp_path / 'out'),
           '--policy', 'minimum_safe_coarsening']
    done = subprocess.run(cmd, cwd=RUN, env=env, capture_output=True, text=True, check=True)
    payload = json.loads(done.stdout)
    assert payload['results'][0]['status'] == 'published'
    assert payload['results'][0]['slots'] == ['A1', 'A2', 'A3', 'A4']
    done = subprocess.run([sys.executable, '-B', '-m', 'meterround', 'recover-disclosures', '--db', str(db),
                           '--out', str(tmp_path / 'out')], cwd=RUN, env=env, capture_output=True, text=True, check=True)
    assert 'recovered' in json.loads(done.stdout)

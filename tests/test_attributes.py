"""绿色属性权属与凭证发行台账测试。

覆盖：两种权属合同的张数/权利人差异、能量闭合失败、非整十余量结转、
重复与并发发行、证据修订撤销、已转让证召回、跨期间隔离，以及原始
source/reading/evidence/decision 的不可变性。
"""

import sqlite3
import threading

import pytest

from meterround.__main__ import initialize
from meterround.attributes import (
    LedgerError, complete_recall, issue, ledger, reissue, revoke, summary, transfer)
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect

from attribute_support import (
    CASE, FIX, contract, period2_contract, period2_readings, prepare, rewrite_readings,
    snapshot, two_period_config, write_config)


def fixture_config():
    return FIX / 'solar_attribute_contract.json'


# ---------------------------------------------------------------- 两种权属结果

def test_gross_generation_title_issues_ten_to_site_operator(tmp_path):
    db = prepare(tmp_path)
    result = issue(fixture_config(), 'gross_generation_title', db)
    assert result['status'] == 'issued'
    assert result['holder'] == 'SITE-OPERATOR'
    assert result['certificate_count'] == 10
    assert result['issued_units'] == 100.0
    assert len(set(result['certificates'])) == 10
    data = summary(db, 'solar-period')
    assert data['certificate_count'] == 10
    by_id = {c['certificate_id']: c for c in data['certificates']}
    assert set(by_id) == set(result['certificates'])
    for cert in by_id.values():
        assert cert['holder'] == 'SITE-OPERATOR'
        assert cert['energy_share'] == 10.0
        assert cert['title_mode'] == 'gross_generation_title'
        assert cert['period_start'] == '2026-01-01T00:00:00+00:00'
        assert cert['period_end'] == '2026-02-01T00:00:00+00:00'
        assert cert['source_digest'] and cert['decision_digest']
        assert cert['reading_digest'] and cert['evidence_digest']
    serials = sorted(c['serial'] for c in by_id.values())
    assert serials == list(range(1, 11))


def test_net_export_title_issues_six_to_grid_buyer_without_self_use(tmp_path):
    db = prepare(tmp_path)
    result = issue(fixture_config(), 'net_export_title', db)
    assert result['status'] == 'issued'
    assert result['holder'] == 'GRID-BUYER'
    assert result['certificate_count'] == 6
    assert result['issued_units'] == 60.0
    assert result['eligible_units'] == 60.0
    data = summary(db, 'solar-period')
    assert data['certificate_count'] == 6
    assert {c['holder'] for c in data['certificates']} == {'GRID-BUYER'}
    assert {c['title_mode'] for c in data['certificates']} == {'net_export_title'}
    # 自用 40 单位绝不能冒作购电方权利：6×10=60，不是 10 张。
    assert result['issued_units'] == 60.0


def test_two_title_modes_on_same_batch_cannot_both_be_valid(tmp_path):
    db = prepare(tmp_path)
    issue(fixture_config(), 'gross_generation_title', db)
    with pytest.raises(LedgerError) as err:
        issue(fixture_config(), 'net_export_title', db)
    assert err.value.code == 'conflict'
    data = summary(db, 'solar-period')
    assert data['certificate_count'] == 10
    assert {c['holder'] for c in data['certificates']} == {'SITE-OPERATOR'}


def test_unknown_title_mode_rejected(tmp_path):
    db = prepare(tmp_path)
    with pytest.raises(LedgerError) as err:
        issue(fixture_config(), 'self_use_title', db)
    assert err.value.code == 'unknown_title_mode'


# ------------------------------------------------------------ 闭合与证据不足

def test_balance_not_closed_blocks_issuance(tmp_path):
    readings = rewrite_readings(tmp_path, [(
        'H-PV-EXPORT,2,PV-EXPORT,M-PV-EXPORT,2026-02-01T00:00:00+00:00,160',
        'H-PV-EXPORT,2,PV-EXPORT,M-PV-EXPORT,2026-02-01T00:00:00+00:00,150')])
    db = prepare(tmp_path, readings=readings)
    con = connect(db)
    assert con.execute("SELECT status FROM decision WHERE slot='PV-EXPORT'").fetchone()[0] == '可结算'
    con.close()
    with pytest.raises(LedgerError) as err:
        issue(fixture_config(), 'gross_generation_title', db)
    assert err.value.code == 'balance_not_closed'
    assert summary(db, 'solar-period')['certificate_count'] == 0


def test_unsettleable_slot_blocks_issuance(tmp_path):
    readings = rewrite_readings(tmp_path, [(
        'H-PV-SELF,2,PV-SELF,M-PV-SELF,2026-02-01T00:00:00+00:00,140',
        'H-PV-SELF,2,PV-SELF,M-PV-SELF,2026-02-01T00:00:00+00:00,100')])
    db = prepare(tmp_path, readings=readings)
    con = connect(db)
    assert con.execute("SELECT status FROM decision WHERE slot='PV-SELF'").fetchone()[0] == '待核'
    con.close()
    with pytest.raises(LedgerError) as err:
        issue(fixture_config(), 'gross_generation_title', db)
    assert err.value.code == 'decision_unsettleable'


def test_missing_read_blocks_issuance(tmp_path):
    readings = rewrite_readings(tmp_path, [(
        'H-PV-GEN,2,PV-GEN,M-PV-GEN,2026-02-01T00:00:00+00:00,200\n', '')])
    db = prepare(tmp_path, readings=readings)
    with pytest.raises(LedgerError) as err:
        issue(fixture_config(), 'gross_generation_title', db)
    assert err.value.code in ('decision_unsettleable', 'missing_decision')


def test_reverse_flow_blocks_issuance(tmp_path):
    readings = rewrite_readings(tmp_path, [(
        'H-PV-EXPORT,2,PV-EXPORT,M-PV-EXPORT,2026-02-01T00:00:00+00:00,160',
        'H-PV-EXPORT,2,PV-EXPORT,M-PV-EXPORT,2026-02-01T00:00:00+00:00,80')])
    db = prepare(tmp_path, readings=readings)
    with pytest.raises(LedgerError) as err:
        issue(fixture_config(), 'gross_generation_title', db)
    assert err.value.code in ('reverse_flow', 'decision_unsettleable')


def test_slot_from_other_batch_rejected(tmp_path):
    db = prepare(tmp_path)
    con = connect(db)
    con.execute('PRAGMA foreign_keys=OFF')
    con.execute("UPDATE slot SET batch_id='other' WHERE id='PV-EXPORT'")
    con.commit()
    con.close()
    with pytest.raises(LedgerError) as err:
        issue(fixture_config(), 'gross_generation_title', db)
    assert err.value.code == 'batch_mismatch'


def test_tampered_contract_bytes_rejected(tmp_path):
    contract_path = tmp_path / 'ATTRIBUTE-001.txt'
    contract_path.write_bytes((FIX / CASE['contract_file']).read_bytes() + b'\n')
    spec = contract({'contract_file': 'ATTRIBUTE-001.txt'})
    config = write_config(tmp_path, spec)
    db = prepare(tmp_path)
    with pytest.raises(LedgerError) as err:
        issue(config, 'gross_generation_title', db)
    assert err.value.code == 'contract_hash_mismatch'


# ------------------------------------------------------------------ 余量结转

def test_non_multiple_leftover_carried_not_rounded(tmp_path):
    # 第一期发电 105、自用 45、上网 60：发 10 张、天然余 5 单位。
    # 第二期发电 120 转入上一期 5 单位：125 只发 12 张、余 5，绝不四舍五入到 13。
    config2 = two_period_config(tmp_path)
    db = tmp_path / 'carry.db'
    initialize(db, config2)
    ingest(rewrite_readings(tmp_path, [
        ('H-PV-GEN,2,PV-GEN,M-PV-GEN,2026-02-01T00:00:00+00:00,200',
         'H-PV-GEN,2,PV-GEN,M-PV-GEN,2026-02-01T00:00:00+00:00,205'),
        ('H-PV-SELF,2,PV-SELF,M-PV-SELF,2026-02-01T00:00:00+00:00,140',
         'H-PV-SELF,2,PV-SELF,M-PV-SELF,2026-02-01T00:00:00+00:00,145'),
    ]), db)
    ingest(period2_readings(tmp_path, (220, 145, 175)), db)
    evaluate(db)

    first = write_config(tmp_path, contract({'batch_id': 'solar-period'}), 'first.json')
    r1 = issue(first, 'gross_generation_title', db)
    assert r1['certificate_count'] == 10
    assert r1['period_remainder_units'] == 5.0
    assert r1['closing_carry_units'] == 5.0

    second = write_config(tmp_path, period2_contract(), 'second.json')
    # solar-period-2 用量：GEN 120、SELF 45、EXPORT 75（闭合）。
    result = issue(second, 'gross_generation_title', db)
    assert result['opening_carry_units'] == 5.0
    assert result['certificate_count'] == 12   # (5+120)//10，绝不四舍五入到 13
    assert result['issued_units'] == 120.0
    assert result['period_remainder_units'] == 5.0
    assert result['closing_carry_units'] == 5.0
    assert result['carried_from_id'] == r1['issuance_id']
    data = summary(db, 'solar-period-2')
    assert len({c['certificate_id'] for c in data['certificates']}) == 12
    carry = next(c for c in data['carry'] if c['issuance_id'] == result['issuance_id'])
    assert carry['opening_units'] == 5.0 and carry['closing_units'] == 5.0


def test_remainder_without_opening_carry_stays_traceable(tmp_path):
    # 第二期发电 105、自用 45、上网 60：只发 10 张，余 5 留在本合同/场站下结转。
    config2 = two_period_config(tmp_path)
    db = tmp_path / 'carry2.db'
    initialize(db, config2)
    ingest(FIX / 'solar_readings.csv', db)
    ingest(period2_readings(tmp_path, (205, 145, 160)), db)
    evaluate(db)
    second = write_config(tmp_path, period2_contract(), 'p2.json')
    result = issue(second, 'gross_generation_title', db)
    assert result['opening_carry_units'] == 0.0
    assert result['certificate_count'] == 10
    assert result['issued_units'] == 100.0
    assert result['period_remainder_units'] == 5.0
    con = connect(db)
    row = con.execute('SELECT * FROM attribute_carry WHERE issuance_id=?',
                      (result['issuance_id'],)).fetchone()
    con.close()
    assert row['opening_units'] == 0.0
    assert row['closing_units'] == 5.0
    assert row['status'] == '可结转'
    assert row['carried_from_id'] is None


# ---------------------------------------------------------- 重复与并发发行

def test_same_snapshot_rerun_is_idempotent(tmp_path):
    db = prepare(tmp_path)
    first = issue(fixture_config(), 'gross_generation_title', db)
    second = issue(fixture_config(), 'gross_generation_title', db)
    assert second['status'] == 'unchanged'
    assert second['certificate_count'] == first['certificate_count']
    assert second['certificates'] == first['certificates']
    con = connect(db)
    assert con.execute('SELECT count(*) FROM attribute_certificate').fetchone()[0] == 10
    assert con.execute('SELECT count(*) FROM attribute_issuance').fetchone()[0] == 1
    con.close()


def test_concurrent_issuance_only_one_wins(tmp_path):
    db = prepare(tmp_path)
    barrier = threading.Barrier(3)
    results = []

    def attempt(title):
        try:
            barrier.wait(timeout=10)
            results.append(('ok', issue(fixture_config(), title, db)))
        except LedgerError as exc:
            results.append(('err', exc.code))
        except Exception as exc:  # sqlite locks should also surface as conflict
            results.append(('exc', type(exc).__name__))

    threads = [threading.Thread(target=attempt, args=(title,))
               for title in ('gross_generation_title', 'net_export_title')]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=20)

    outcomes = sorted(kind for kind, _ in results)
    assert outcomes == ['err', 'ok'], results
    winner = next(value for kind, value in results if kind == 'ok')
    loser = next(value for kind, value in results if kind == 'err')
    assert loser == 'conflict'
    data = summary(db, 'solar-period')
    assert data['certificate_count'] == winner['certificate_count']
    assert {c['holder'] for c in data['certificates']} == {winner['holder']}
    con = connect(db)
    assert con.execute('SELECT count(*) FROM attribute_issuance').fetchone()[0] == 1
    con.close()


# ------------------------------------------------------------ 证据修订与召回

def test_evidence_revision_revokes_old_issuance_and_reissues(tmp_path):
    db = prepare(tmp_path)
    old = issue(fixture_config(), 'gross_generation_title', db)
    # 模拟证据修订后的重算：改写 decision 的 usage/rule_version（原始
    # reading/evidence 不被改写），确认旧发行撤销后才按新基础发行。
    con = connect(db)
    con.execute("UPDATE decision SET usage=55,rule_version='2' WHERE slot='PV-EXPORT'")
    con.execute("UPDATE decision SET usage=45,rule_version='2' WHERE slot='PV-SELF'")
    con.commit()
    con.close()
    new = reissue(fixture_config(), 'gross_generation_title', db)
    assert new['status'] == 'issued'
    assert new['revoked_certificates'] == 10
    assert new['recall_tasks'] == 0
    assert new['superseded_status'] == '已撤销'
    assert not set(new['certificates']) & set(old['certificates'])
    data = ledger(db, 'solar-period')
    statuses = {i['status'] for i in data['issuances']}
    assert '已撤销' in statuses and '有效' in statuses
    old_row = next(i for i in data['issuances'] if i['id'] == old['issuance_id'])
    assert old_row['status'] == '已撤销' and old_row['revoked_at']
    for cert in data['certificates']:
        if cert['issuance_id'] == old['issuance_id']:
            assert cert['status'] == '已撤销'


def test_transferred_certificate_becomes_recall_not_silent_reassignment(tmp_path):
    db = prepare(tmp_path)
    result = issue(fixture_config(), 'gross_generation_title', db)
    sold = result['certificates'][0]
    transfer(sold, 'MARKET-BUYER', db)
    moved = next(c for c in summary(db, 'solar-period')['certificates']
                 if c['certificate_id'] == sold)
    assert moved['holder'] == 'MARKET-BUYER' and moved['origin_holder'] == 'SITE-OPERATOR'
    assert moved['status'] == '已转让'
    con = connect(db)
    con.execute("UPDATE decision SET usage=55,rule_version='2' WHERE slot='PV-EXPORT'")
    con.execute("UPDATE decision SET usage=45,rule_version='2' WHERE slot='PV-SELF'")
    con.commit()
    con.close()
    new = reissue(fixture_config(), 'gross_generation_title', db, 'evidence_revised')
    assert new['recall_tasks'] == 1
    assert new['revoked_certificates'] == 9
    assert new['superseded_status'] == '待召回'
    data = ledger(db, 'solar-period')
    old_cert = next(c for c in data['certificates'] if c['certificate_id'] == sold)
    assert old_cert['status'] == '待召回'
    assert old_cert['transferred_to'] == 'MARKET-BUYER'
    assert len(data['recalls']) == 1 and data['recalls'][0]['status'] == '待召回'
    assert not set(new['certificates']) & set(result['certificates'])
    # 召回完成后旧证不再处于可转让状态。
    complete_recall(sold, db)
    data = ledger(db, 'solar-period')
    old_cert = next(c for c in data['certificates'] if c['certificate_id'] == sold)
    assert old_cert['status'] == '已召回'


def test_revision_that_breaks_evidence_still_revokes(tmp_path):
    db = prepare(tmp_path)
    issue(fixture_config(), 'gross_generation_title', db)
    con = connect(db)
    con.execute("UPDATE decision SET status='待核' WHERE slot='PV-SELF'")
    con.commit()
    con.close()
    with pytest.raises(LedgerError) as err:
        reissue(fixture_config(), 'gross_generation_title', db)
    assert err.value.code == 'reissue_blocked'
    data = summary(db, 'solar-period')
    assert data['certificate_count'] == 0
    raw = ledger(db, 'solar-period')
    assert all(i['status'] in ('已撤销', '待召回') for i in raw['issuances'])


def test_reissue_with_same_snapshot_does_not_reissue(tmp_path):
    db = prepare(tmp_path)
    issue(fixture_config(), 'gross_generation_title', db)
    result = reissue(fixture_config(), 'gross_generation_title', db)
    assert result['status'] == 'unchanged'
    con = connect(db)
    assert con.execute('SELECT count(*) FROM attribute_issuance').fetchone()[0] == 1
    con.close()


def test_reissue_can_switch_title_after_revocation(tmp_path):
    db = prepare(tmp_path)
    old = issue(fixture_config(), 'gross_generation_title', db)
    new = reissue(fixture_config(), 'net_export_title', db, 'contract_amended')
    assert new['status'] == 'issued'
    assert new['holder'] == 'GRID-BUYER'
    assert new['certificate_count'] == 6
    assert new['revoked_certificates'] == 10
    assert not set(new['certificates']) & set(old['certificates'])
    data = summary(db, 'solar-period')
    assert data['certificate_count'] == 6
    assert {c['holder'] for c in data['certificates']} == {'GRID-BUYER'}
    raw = ledger(db, 'solar-period')
    old_row = next(i for i in raw['issuances'] if i['id'] == old['issuance_id'])
    assert old_row['status'] == '已撤销'
    assert all(c['status'] == '已撤销' for c in raw['certificates']
               if c['issuance_id'] == old['issuance_id'])


def test_reissue_failure_keeps_old_issuance_valid(tmp_path):
    db = prepare(tmp_path)
    old = issue(fixture_config(), 'gross_generation_title', db)
    con = connect(db)
    con.execute("UPDATE decision SET usage=55,rule_version='2' WHERE slot='PV-EXPORT'")
    con.execute("UPDATE decision SET usage=45,rule_version='2' WHERE slot='PV-SELF'")
    con.execute("CREATE TRIGGER reject_cert BEFORE INSERT ON attribute_certificate "
                "BEGIN SELECT RAISE(ABORT,'injected'); END")
    con.commit()
    con.close()
    with pytest.raises(sqlite3.IntegrityError):
        reissue(fixture_config(), 'gross_generation_title', db)
    data = summary(db, 'solar-period')
    assert data['certificate_count'] == 10
    assert {c['certificate_id'] for c in data['certificates']} == set(old['certificates'])


# -------------------------------------------------------------- 跨期间隔离

def test_cross_period_issuance_isolated(tmp_path):
    config2 = two_period_config(tmp_path)
    db = tmp_path / 'solar2.db'
    initialize(db, config2)
    ingest(FIX / 'solar_readings.csv', db)
    ingest(period2_readings(tmp_path, (250, 220, 130)), db)
    evaluate(db)
    first = write_config(tmp_path, contract({'batch_id': 'solar-period'}), 'p1.json')
    second = write_config(tmp_path, period2_contract(), 'p2.json')
    r1 = issue(first, 'gross_generation_title', db)
    r2 = issue(second, 'net_export_title', db)
    assert r1['certificate_count'] == 10
    assert r2['certificate_count'] == 3          # 上网 30 → 3 张
    assert not set(r1['certificates']) & set(r2['certificates'])
    p1 = summary(db, 'solar-period')
    p2 = summary(db, 'solar-period-2')
    assert {c['period_start'] for c in p1['certificates']} == {'2026-01-01T00:00:00+00:00'}
    assert {c['period_start'] for c in p2['certificates']} == {'2026-02-01T00:00:00+00:00'}
    assert all(c['holder'] == 'SITE-OPERATOR' for c in p1['certificates'])
    assert all(c['holder'] == 'GRID-BUYER' for c in p2['certificates'])


def test_carry_does_not_cross_unrelated_periods(tmp_path):
    # 结转只允许来自结束时间不晚于本期开始的期间，更晚期间不能倒灌进更早期间。
    config2 = two_period_config(tmp_path)
    db = tmp_path / 'iso.db'
    initialize(db, config2)
    first = write_config(tmp_path, contract({'batch_id': 'solar-period'}), 'p1.json')
    con = connect(db)
    for slot in ('PV-GEN', 'PV-SELF', 'PV-EXPORT'):
        con.execute("INSERT INTO decision VALUES (?,?,?,?,?)",
                    (slot, '可结算', {'PV-GEN': 100, 'PV-SELF': 40, 'PV-EXPORT': 60}[slot], '{}', '1'))
    for slot in ('PV-GEN-2', 'PV-SELF-2', 'PV-EXPORT-2'):
        con.execute("INSERT INTO decision VALUES (?,?,?,?,?)",
                    (slot, '可结算', {'PV-GEN-2': 100, 'PV-SELF-2': 40, 'PV-EXPORT-2': 60}[slot],
                     '{}', '1'))
    con.commit()
    con.close()
    r1 = issue(first, 'gross_generation_title', db)
    con = connect(db)
    con.execute("UPDATE attribute_carry SET closing_units=5,status='可结转',"
                "closed_by_issuance_id=NULL WHERE issuance_id=?", (r1['issuance_id'],))
    con.commit()
    con.close()
    second = write_config(tmp_path, period2_contract(), 'p2.json')
    r2 = issue(second, 'gross_generation_title', db)
    assert r2['opening_carry_units'] == 5.0       # 第一期确实已结束，允许结转
    assert r2['certificate_count'] == 10          # 5+100=105 → 10 张，余 5
    assert r2['period_remainder_units'] == 5.0

    # 把第二期的余量重新打开，再撤销并按第一期重发：更晚期间不得倒灌回更早期间。
    con = connect(db)
    con.execute("UPDATE attribute_carry SET status='可结转',closed_by_issuance_id=NULL "
                "WHERE issuance_id=?", (r2['issuance_id'],))
    con.commit()
    con.close()
    revoke(first, db, 'rebuild')
    r1b = issue(first, 'gross_generation_title', db)
    assert r1b['opening_carry_units'] == 0.0
    assert r1b['certificate_count'] == 10


# ---------------------------------------------------------------- 不可变性

def test_issue_does_not_mutate_original_evidence(tmp_path):
    db = prepare(tmp_path)
    before = snapshot(db)
    issue(fixture_config(), 'gross_generation_title', db)
    transfer(summary(db, 'solar-period')['certificates'][0]['certificate_id'], 'X', db)
    reissue(fixture_config(), 'gross_generation_title', db, 'evidence_revised')
    assert snapshot(db) == before


def test_issue_rolls_back_entirely_on_error(tmp_path):
    db = prepare(tmp_path)
    con = connect(db)
    con.execute("CREATE TRIGGER reject_cert BEFORE INSERT ON attribute_certificate "
                "BEGIN SELECT RAISE(ABORT,'injected'); END")
    con.commit()
    con.close()
    with pytest.raises(sqlite3.IntegrityError):
        issue(fixture_config(), 'gross_generation_title', db)
    con = connect(db)
    assert con.execute('SELECT count(*) FROM attribute_issuance').fetchone()[0] == 0
    assert con.execute('SELECT count(*) FROM attribute_certificate').fetchone()[0] == 0
    assert con.execute('SELECT count(*) FROM attribute_carry').fetchone()[0] == 0
    con.close()


def test_ledger_records_source_digests_and_rule_versions(tmp_path):
    db = prepare(tmp_path)
    issue(fixture_config(), 'net_export_title', db)
    data = summary(db, 'solar-period')
    issuance = data['issuances'][0]
    assert issuance['source']['rule_versions'] == {'PV-GEN': '1', 'PV-SELF': '1', 'PV-EXPORT': '1'}
    assert issuance['source']['usage'] == {'generation': 100.0, 'self_use': 40.0, 'export': 60.0}
    assert issuance['source']['decision_digest']
    assert issuance['source']['source_digest']
    contracts = ledger(db)['contracts']
    assert contracts and contracts[0]['sha256'] == CASE['contract_sha256']
    assert contracts[0]['holder'] == 'GRID-BUYER'

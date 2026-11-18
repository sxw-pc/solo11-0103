import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from meterround.__main__ import initialize
from meterround.custody import (
    WAREHOUSE,
    CustodyError,
    custody_view,
    issue_handoff,
    retire_meter,
    seal_meter,
    submit_credential,
)
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
HANDOFF = FIX / 'retired_meter_handoff.json'
OWNER_WIN = ('2026-02-02T08:00:00Z', '2026-02-02T09:00:00Z')
LAB_WIN = ('2026-02-02T10:00:00Z', '2026-02-02T11:00:00Z')
OWNER_ORDER = 'HO-M1-return_to_owner'
LAB_ORDER = 'HO-M1-quarantine_at_lab'


def build_db(tmp, config='retirement_config.json', readings='retirement_readings.csv'):
    db = tmp / 'custody.db'
    initialize(db, FIX / config)
    assert ingest(FIX / readings, db)['imported'] == 2
    assert evaluate(db)['A-01'] == '可结算'
    return db


def retired_sealed(db, request_seq):
    """已拆下 -> 已封存 的标准前置。"""
    r = retire_meter(db, HANDOFF, FIX, f'req-{request_seq}-retire', occurred_at='2026-01-20T08:00:00Z')
    assert r['state'] == '已拆下'
    s = seal_meter(db, HANDOFF, FIX, f'req-{request_seq}-seal', occurred_at='2026-01-20T09:00:00Z')
    assert s['state'] == '已封存'
    return r, s


def dispatch_and_receipt(db, order_id, consignee, req_prefix, at_receipt, at_dispatch=None):
    submit_credential(db, HANDOFF, FIX, order_id, 'dispatch', f'{req_prefix}-d',
                      occurred_at=at_dispatch or at_receipt)
    return submit_credential(db, HANDOFF, FIX, order_id, 'receipt', f'{req_prefix}-r',
                             occurred_at=at_receipt)


def test_happy_path_owner_takes_custody_with_full_chain(tmp_path):
    db = build_db(tmp_path)
    retired_sealed(db, 1)
    issue = issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-1-issue',
                          occurred_at='2026-02-02T07:30:00Z')
    assert issue['order_id'] == OWNER_ORDER and issue['state'] == '待交接'
    # 仅发单、未闭合前，实物仍由仓管持有，收件方名称不等于保管权
    view = custody_view(db, 'M1')
    assert view['holder'] == WAREHOUSE and view['state'] == '待交接'
    result = dispatch_and_receipt(db, OWNER_ORDER, 'ASSET-OWNER', 'req-1', OWNER_WIN[0])
    assert result['closed'] and result['state'] == '已签收' and result['holder'] == 'ASSET-OWNER'
    view = custody_view(db, 'M1')
    assert view['holder'] == 'ASSET-OWNER' and view['quarantine'] is False
    assert view['owner_can_collect'] is True
    chain = [(e['action'], e['state_from'], e['state_to']) for e in view['chain']]
    assert chain == [
        ('retire', None, '已拆下'),
        ('seal', '已拆下', '已封存'),
        ('issue_handoff', '已封存', '待交接'),
        ('credential', '待交接', '待交接'),
        ('signoff', '待交接', '已签收'),
    ]
    for event in view['chain']:
        assert event['actor'] and event['occurred_at'] and event['source'] and event['request_id']
    assert len(view['credentials']) == 2
    assert {c['direction'] for c in view['credentials']} == {'dispatch', 'receipt'}
    assert all(c['seal_id'] == 'SEAL-001' for c in view['credentials'])


def test_tampered_seal_is_rejected(tmp_path):
    db = build_db(tmp_path)
    retire_meter(db, HANDOFF, FIX, 'req-2-retire', occurred_at='2026-01-20T08:00:00Z')
    bad = tmp_path / 'meter_seals'
    bad.mkdir()
    tampered = bad / 'SEAL-001.txt'
    original = (FIX / 'meter_seals' / 'SEAL-001.txt').read_bytes()
    tampered.write_bytes(original + b'tampered')
    with pytest.raises(CustodyError) as exc:
        seal_meter(db, HANDOFF, tmp_path, 'req-2-seal-bad', occurred_at='2026-01-20T09:00:00Z')
    assert exc.value.reason == 'seal_hash_mismatch'
    view = custody_view(db, 'M1')
    assert view['state'] == '已拆下' and view['holder'] == WAREHOUSE
    assert [e['action'] for e in view['chain']] == ['retire']
    # 失败请求重放得到相同拒绝，且不产生新流水
    with pytest.raises(CustodyError):
        seal_meter(db, HANDOFF, tmp_path, 'req-2-seal-bad', occurred_at='2026-01-20T09:00:00Z')
    assert len(custody_view(db, 'M1')['chain']) == 1


def test_missing_seal_file_is_rejected(tmp_path):
    db = build_db(tmp_path)
    retire_meter(db, HANDOFF, FIX, 'req-3-retire', occurred_at='2026-01-20T08:00:00Z')
    empty = tmp_path / 'no_seals'
    empty.mkdir()
    with pytest.raises(CustodyError) as exc:
        seal_meter(db, HANDOFF, empty, 'req-3-seal', occurred_at='2026-01-20T09:00:00Z')
    assert exc.value.reason == 'seal_file_missing'
    assert custody_view(db, 'M1')['state'] == '已拆下'


def test_wrong_serial_binding_is_rejected(tmp_path):
    db = build_db(tmp_path)
    retire_meter(db, HANDOFF, FIX, 'req-4-retire', occurred_at='2026-01-20T08:00:00Z')
    # 封存后签收，凭据声明的封条号与旧表绑定不一致
    seal_meter(db, HANDOFF, FIX, 'req-4-seal', occurred_at='2026-01-20T09:00:00Z')
    issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-4-issue', occurred_at='2026-02-02T07:30:00Z')
    with pytest.raises(CustodyError) as exc2:
        submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'receipt', 'req-4-r',
                          actor='ASSET-OWNER', occurred_at=OWNER_WIN[0], seal_id='SEAL-999')
    assert exc2.value.reason == 'seal_serial_mismatch'
    view = custody_view(db, 'M1')
    assert view['holder'] == WAREHOUSE and view['state'] == '待交接'
    assert db_connect_count(db, 'handoff_credential') == 0


def db_connect_count(db_path, table):
    con = connect(db_path)
    n = con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
    con.close()
    return n


def test_state_skip_is_rejected(tmp_path):
    db = build_db(tmp_path)
    retire_meter(db, HANDOFF, FIX, 'req-5-retire', occurred_at='2026-01-20T08:00:00Z')
    # 未封存直接发交接单：跳步
    with pytest.raises(CustodyError) as exc:
        issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-5-issue',
                      occurred_at='2026-02-02T07:30:00Z')
    assert exc.value.reason == 'illegal_transition'
    assert custody_view(db, 'M1')['state'] == '已拆下'
    with pytest.raises(CustodyError):
        submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'receipt', 'req-5-r',
                          occurred_at=OWNER_WIN[0])


def test_receipt_outside_window_is_rejected(tmp_path):
    db = build_db(tmp_path)
    retired_sealed(db, 6)
    issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-6-issue', occurred_at='2026-02-02T07:30:00Z')
    with pytest.raises(CustodyError) as exc:
        submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'receipt', 'req-6-late',
                          actor='ASSET-OWNER', occurred_at='2026-02-02T09:30:00Z')
    assert exc.value.reason == 'outside_handoff_window'
    view = custody_view(db, 'M1')
    assert view['holder'] == WAREHOUSE
    assert db_connect_count(db, 'handoff_credential') == 0


def test_old_meter_still_installed_cannot_retire(tmp_path):
    db = build_db(tmp_path)
    con = connect(db)
    # 让 M1 的 assignment 在换表时刻之后仍生效
    con.execute("UPDATE assignment SET end='2027-01-01T00:00:00+00:00' WHERE meter='M1'")
    con.commit()
    con.close()
    with pytest.raises(CustodyError) as exc:
        retire_meter(db, HANDOFF, FIX, 'req-7-retire', occurred_at='2026-01-20T08:00:00Z')
    assert exc.value.reason == 'old_meter_still_installed'
    con = connect(db)
    assert con.execute('SELECT count(*) FROM retired_meter').fetchone()[0] == 0
    assert con.execute('SELECT count(*) FROM custody_event').fetchone()[0] == 0
    # 失败请求本身仍留痕，重放同样失败
    assert con.execute("SELECT ok FROM custody_request WHERE request_id='req-7-retire'").fetchone()[0] == 0
    con.close()
    with pytest.raises(CustodyError):
        retire_meter(db, HANDOFF, FIX, 'req-7-retire', occurred_at='2026-01-20T08:00:00Z')


def test_half_closed_credentials_keep_meter_with_warehouse(tmp_path):
    db = build_db(tmp_path)
    retired_sealed(db, 8)
    issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-8-issue', occurred_at='2026-02-02T07:30:00Z')
    # 只有仓管发出凭据，缺接收方签收凭据
    submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'dispatch', 'req-8-d', occurred_at=OWNER_WIN[0])
    view = custody_view(db, 'M1')
    assert view['state'] == '待交接' and view['holder'] == WAREHOUSE
    # 错的接收方身份不能闭合
    with pytest.raises(CustodyError) as exc:
        submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'receipt', 'req-8-r-wrong',
                          actor='METROLOGY-LAB', occurred_at=OWNER_WIN[0])
    assert exc.value.reason == 'actor_not_authorized'
    assert custody_view(db, 'M1')['holder'] == WAREHOUSE


def test_signoff_failure_is_atomic_and_recoverable(tmp_path):
    db = build_db(tmp_path)
    retired_sealed(db, 9)
    issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-9-issue', occurred_at='2026-02-02T07:30:00Z')
    submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'dispatch', 'req-9-d', occurred_at=OWNER_WIN[0])
    con = connect(db)
    con.execute("CREATE TRIGGER reject_signoff BEFORE INSERT ON custody_event "
                "WHEN NEW.action='signoff' BEGIN SELECT RAISE(ABORT,'injected'); END")
    con.commit()
    con.close()
    with pytest.raises(CustodyError):
        submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'receipt', 'req-9-r-fail',
                          actor='ASSET-OWNER', occurred_at=OWNER_WIN[0])
    con = connect(db)
    # 签收事务整体回滚：无残留凭据/流水/请求，旧表仍在仓管
    assert con.execute("SELECT count(*) FROM handoff_credential WHERE direction='receipt'").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM custody_request WHERE request_id='req-9-r-fail'").fetchone()[0] == 0
    assert con.execute("SELECT status FROM handoff_order WHERE id=?", (OWNER_ORDER,)).fetchone()[0] == '待交接'
    con.execute('DROP TRIGGER reject_signoff')
    con.commit()
    con.close()
    # 同一请求恢复重放后成功闭合
    result = submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'receipt', 'req-9-r-fail',
                               actor='ASSET-OWNER', occurred_at=OWNER_WIN[0])
    assert result['closed'] and custody_view(db, 'M1')['holder'] == 'ASSET-OWNER'


def test_same_request_replays_are_idempotent(tmp_path):
    db = build_db(tmp_path)
    r1 = retire_meter(db, HANDOFF, FIX, 'req-10-retire', occurred_at='2026-01-20T08:00:00Z')
    r2 = retire_meter(db, HANDOFF, FIX, 'req-10-retire', occurred_at='2026-01-20T08:00:00Z')
    assert r1 == r2
    s1 = seal_meter(db, HANDOFF, FIX, 'req-10-seal', occurred_at='2026-01-20T09:00:00Z')
    s2 = seal_meter(db, HANDOFF, FIX, 'req-10-seal', occurred_at='2026-01-20T09:00:00Z')
    assert s1 == s2
    issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-10-issue', occurred_at='2026-02-02T07:30:00Z')
    dispatch_and_receipt(db, OWNER_ORDER, 'ASSET-OWNER', 'req-10', OWNER_WIN[1])
    replay = submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'receipt', 'req-10-r',
                               actor='ASSET-OWNER', occurred_at=OWNER_WIN[1])
    assert replay['closed'] is True
    con = connect(db)
    assert con.execute('SELECT count(*) FROM retired_meter').fetchone()[0] == 1
    assert con.execute('SELECT count(*) FROM custody_event').fetchone()[0] == 5
    assert con.execute('SELECT count(*) FROM handoff_credential').fetchone()[0] == 2
    assert con.execute('SELECT count(*) FROM custody_request').fetchone()[0] == 5
    con.close()


def test_concurrent_claims_only_one_commits(tmp_path):
    db = build_db(tmp_path)
    retired_sealed(db, 11)
    issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-11-issue-o', occurred_at='2026-02-02T07:30:00Z')
    issue_handoff(db, HANDOFF, FIX, 'quarantine_at_lab', 'req-11-issue-l', occurred_at='2026-02-02T07:31:00Z')
    submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'dispatch', 'req-11-d-o', occurred_at=OWNER_WIN[0])
    submit_credential(db, HANDOFF, FIX, LAB_ORDER, 'dispatch', 'req-11-d-l', occurred_at=LAB_WIN[0])
    barrier = __import__('threading').Barrier(2)
    outcomes = []

    def claim(order_id, actor, when, request_id):
        barrier.wait()
        try:
            submit_credential(db, HANDOFF, FIX, order_id, 'receipt', request_id,
                              actor=actor, occurred_at=when)
            outcomes.append(('ok', order_id))
        except CustodyError as exc:
            outcomes.append((exc.reason, order_id))

    t1 = __import__('threading').Thread(
        target=claim, args=(OWNER_ORDER, 'ASSET-OWNER', OWNER_WIN[0], 'req-11-r-o'))
    t2 = __import__('threading').Thread(
        target=claim, args=(LAB_ORDER, 'METROLOGY-LAB', LAB_WIN[0], 'req-11-r-l'))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(outcomes) in (
        [('ok', LAB_ORDER), ('order_superseded', OWNER_ORDER)],
        [('ok', LAB_ORDER), ('signoff_lost', OWNER_ORDER)],
        [('ok', OWNER_ORDER), ('order_superseded', LAB_ORDER)],
        [('ok', OWNER_ORDER), ('signoff_lost', LAB_ORDER)],
    )
    view = custody_view(db, 'M1')
    assert view['state'] == '已签收'
    assert view['holder'] in ('ASSET-OWNER', 'METROLOGY-LAB')
    # 终态唯一：只有一个 signoff 事件、一条冲突记录、失败方凭据未入库
    assert len([e for e in view['chain'] if e['action'] == 'signoff']) == 1
    assert len(view['conflicts']) == 1 and view['conflicts'][0]['reason'] == 'signoff_lost'
    con = connect(db)
    assert con.execute("SELECT count(*) FROM handoff_credential WHERE direction='receipt'").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM custody_event WHERE action='signoff'").fetchone()[0] == 1
    winning_order = view['chain'][-1]['detail']['order_id']
    assert con.execute('SELECT status FROM handoff_order WHERE id=?', (winning_order,)).fetchone()[0] == '已签收'
    assert con.execute("SELECT count(*) FROM handoff_order WHERE status='已作废'").fetchone()[0] == 1
    con.close()
    # 失败方后续任何凭据都不能再改变持有人
    with pytest.raises(CustodyError) as exc:
        submit_credential(db, HANDOFF, FIX,
                          LAB_ORDER if winning_order == OWNER_ORDER else OWNER_ORDER,
                          'receipt', 'req-11-r-retry', occurred_at=LAB_WIN[1])
    assert exc.value.reason in ('signoff_lost', 'order_superseded')
    assert custody_view(db, 'M1')['holder'] == view['holder']


def test_lab_contract_ends_in_quarantine_owner_cannot_collect(tmp_path):
    db = build_db(tmp_path)
    retired_sealed(db, 12)
    issue_handoff(db, HANDOFF, FIX, 'quarantine_at_lab', 'req-12-issue', occurred_at='2026-02-02T09:30:00Z')
    # 业主合同的交接单虽同时存在，但终态由实到签收凭据决定，不由收件方名称决定
    issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-12-issue-o', occurred_at='2026-02-02T09:31:00Z')
    result = dispatch_and_receipt(db, LAB_ORDER, 'METROLOGY-LAB', 'req-12-lab', LAB_WIN[0])
    assert result['holder'] == 'METROLOGY-LAB'
    view = custody_view(db, 'M1')
    assert view['holder'] == 'METROLOGY-LAB' and view['quarantine'] is True
    assert view['owner_can_collect'] is False
    # 业主在 lab 闭合后才送达凭据：窗口通过也不能改变持有人，领取被拒并留冲突记录
    with pytest.raises(CustodyError) as exc:
        submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'receipt', 'req-12-r',
                          actor='ASSET-OWNER', occurred_at=LAB_WIN[0])
    assert exc.value.reason in ('signoff_lost', 'order_superseded')
    final_view = custody_view(db, 'M1')
    assert final_view['holder'] == 'METROLOGY-LAB'
    assert len(final_view['conflicts']) == 1
    assert final_view['conflicts'][0]['actor'] == 'ASSET-OWNER'


def test_contract_window_choice_determines_holder(tmp_path):
    # 同一 M1/SEAL-001：08-09 点窗口交业主成功；10-11 点窗口的 lab 单在业主窗口内签收即越窗
    db = build_db(tmp_path)
    retired_sealed(db, 13)
    issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-13-issue-o', occurred_at='2026-02-02T07:30:00Z')
    issue_handoff(db, HANDOFF, FIX, 'quarantine_at_lab', 'req-13-issue-l', occurred_at='2026-02-02T07:31:00Z')
    with pytest.raises(CustodyError) as exc:
        submit_credential(db, HANDOFF, FIX, LAB_ORDER, 'receipt', 'req-13-r-lab-early',
                          actor='METROLOGY-LAB', occurred_at=OWNER_WIN[0])
    assert exc.value.reason == 'outside_handoff_window'
    result = dispatch_and_receipt(db, OWNER_ORDER, 'ASSET-OWNER', 'req-13', OWNER_WIN[1])
    assert result['holder'] == 'ASSET-OWNER'
    view = custody_view(db, 'M1')
    assert view['holder'] == 'ASSET-OWNER' and view['quarantine'] is False


def test_custody_ledger_does_not_touch_settlement_data(tmp_path):
    db = build_db(tmp_path)
    con = connect(db)

    def snapshot():
        return {t: [tuple(r) for r in con.execute(f'SELECT * FROM {t} ORDER BY 1')]
                for t in ('replacement', 'reading', 'source', 'evidence', 'decision', 'assignment', 'run')}

    before = snapshot()
    con.close()
    retired_sealed(db, 14)
    issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-14-issue', occurred_at='2026-02-02T07:30:00Z')
    dispatch_and_receipt(db, OWNER_ORDER, 'ASSET-OWNER', 'req-14', OWNER_WIN[0])
    con = connect(db)
    after = snapshot()
    assert before == after
    # 保管流水只追加：不存在 UPDATE/DELETE 路径可改写历史事件
    assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    con.close()


def test_seal_hash_bound_in_asset_must_match_manifest(tmp_path):
    db = build_db(tmp_path)
    retire_meter(db, HANDOFF, FIX, 'req-15-retire', occurred_at='2026-01-20T08:00:00Z')
    con = connect(db)
    row = con.execute('SELECT seal_id,seal_sha256 FROM retired_meter WHERE old_meter=?', ('M1',)).fetchone()
    manifest = json.loads(HANDOFF.read_text(encoding='utf-8'))
    assert row['seal_id'] == manifest['seal_id']
    assert row['seal_sha256'] == manifest['seal_sha256']
    con.close()
    # 封条号唯一绑定：数据库层拒绝第二块表复用同一封条
    con = connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            'INSERT INTO retired_meter(slot,old_meter,new_meter,replacement_at,seal_id,seal_sha256,seal_file,created_at,created_by,source)'
            " VALUES ('A-02','M9','M10','2026-01-15T00:00:00+00:00','SEAL-001','x','f','t','u','test')")
    con.rollback()
    con.close()


def test_cli_end_to_end(tmp_path):
    db = build_db(tmp_path)
    env_root = Path(__file__).resolve().parent.parent
    base = [sys.executable, '-B', '-m', 'meterround']

    def run(*args, expect=0):
        proc = subprocess.run(base + list(args), cwd=env_root, capture_output=True, text=True)
        assert proc.returncode == expect, proc.stderr + proc.stdout
        return json.loads(proc.stdout)

    common = ['--db', str(db), '--handoff', str(HANDOFF), '--fixtures-dir', str(FIX)]
    run('retire', *common, '--request-id', 'cli-retire', '--at', '2026-01-20T08:00:00Z')
    run('seal', *common, '--request-id', 'cli-seal', '--at', '2026-01-20T09:00:00Z')
    run('handoff-issue', *common, '--contract', 'return_to_owner', '--request-id', 'cli-issue',
        '--at', '2026-02-02T07:30:00Z')
    run('handoff-sign', *common, '--order-id', OWNER_ORDER, '--direction', 'dispatch',
        '--request-id', 'cli-d', '--at', OWNER_WIN[0])
    signed = run('handoff-sign', *common, '--order-id', OWNER_ORDER, '--direction', 'receipt',
                 '--request-id', 'cli-r', '--actor', 'ASSET-OWNER', '--at', OWNER_WIN[0])
    assert signed['holder'] == 'ASSET-OWNER'
    view = run('custody-view', '--db', str(db), '--old-meter', 'M1')
    assert view['holder'] == 'ASSET-OWNER' and len(view['chain']) == 5
    # 越窗签收在 CLI 上以非零码和原因码失败
    bad = run('handoff-sign', *common, '--order-id', OWNER_ORDER, '--direction', 'receipt',
              '--request-id', 'cli-late', '--actor', 'ASSET-OWNER',
              '--at', '2026-02-02T12:00:00Z', expect=1)
    assert bad['reason'] == 'signoff_lost'


def test_window_boundary_minute_inside_is_accepted(tmp_path):
    db = build_db(tmp_path)
    retired_sealed(db, 16)
    issue_handoff(db, HANDOFF, FIX, 'return_to_owner', 'req-16-issue', occurred_at='2026-02-02T07:30:00Z')
    submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'dispatch', 'req-16-d', occurred_at=OWNER_WIN[0])
    # 窗口闭区间端点 09:00:00 仍有效
    result = submit_credential(db, HANDOFF, FIX, OWNER_ORDER, 'receipt', 'req-16-r',
                               actor='ASSET-OWNER', occurred_at=OWNER_WIN[1])
    assert result['closed'] and custody_view(db, 'M1')['holder'] == 'ASSET-OWNER'

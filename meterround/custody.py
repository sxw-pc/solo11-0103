"""退役旧表实物保管交接流水。

保管状态机：已拆下 -> 已封存 -> 待交接 -> 已签收。
所有保管记录只追加，不更新、不删除；replacement/reading/source/evidence/decision
等结算数据在本模块中保持只读。
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .ingest import at
from .store import connect

WAREHOUSE = 'WAREHOUSE'
STATES = ('已拆下', '已封存', '待交接', '已签收')
# 合法状态推进顺序；签收为终态
NEXT_STATE = {
    None: ('已拆下',),
    '已拆下': ('已封存',),
    '已封存': ('待交接',),
    '待交接': ('已签收',),
    '已签收': (),
}
DIRECTIONS = ('dispatch', 'receipt')


class CustodyError(Exception):
    """保管规则拒绝或交接冲突；reason 为机器可读原因码。"""

    def __init__(self, reason, detail=''):
        super().__init__(reason + (('：' + detail) if detail else ''))
        self.reason = reason
        self.detail = detail


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _load_manifest(handoff_config):
    return json.loads(Path(handoff_config).read_text(encoding='utf-8'))


def _resolve_seal(manifest, fixtures_dir):
    return Path(fixtures_dir) / manifest['seal_file']


def _record_request(db, request_id, retired_id, operation, ok, result):
    db.execute(
        'INSERT INTO custody_request(request_id,retired_id,operation,ok,result,recorded_at) VALUES (?,?,?,?,?,?)',
        (request_id, retired_id, operation, 1 if ok else 0, json.dumps(result, ensure_ascii=False), utcnow_iso()))


def _replay(db, request_id):
    row = db.execute('SELECT * FROM custody_request WHERE request_id=?', (request_id,)).fetchone()
    if row is None:
        return None
    result = json.loads(row['result'])
    if not row['ok']:
        raise CustodyError(result.get('reason', 'replayed_failure'), result.get('detail', ''))
    return result


def _asset(db, retired_id):
    row = db.execute('SELECT * FROM retired_meter WHERE id=?', (retired_id,)).fetchone()
    if row is None:
        raise CustodyError('asset_not_found', str(retired_id))
    return dict(row)


def _asset_by_meter(db, old_meter):
    return db.execute('SELECT * FROM retired_meter WHERE old_meter=?', (old_meter,)).fetchone()


def _current_state(db, retired_id):
    row = db.execute('SELECT state_to FROM custody_event WHERE retired_id=? ORDER BY seq DESC LIMIT 1', (retired_id,)).fetchone()
    return row['state_to'] if row else None


def _next_seq(db, retired_id):
    row = db.execute('SELECT COALESCE(max(seq),0)+1 AS s FROM custody_event WHERE retired_id=?', (retired_id,)).fetchone()
    return row['s']


def _emit(db, retired_id, action, state_from, state_to, actor, occurred_at, source, request_id, detail):
    db.execute(
        'INSERT INTO custody_event(retired_id,seq,action,state_from,state_to,actor,occurred_at,source,request_id,detail)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?)',
        (retired_id, _next_seq(db, retired_id), action, state_from, state_to, actor, occurred_at, source,
         request_id, json.dumps(detail, ensure_ascii=False, sort_keys=True)))


def _fail(db, request_id, retired_id, operation, reason, detail):
    result = {'ok': False, 'reason': reason, 'detail': detail}
    _record_request(db, request_id, retired_id, operation, False, result)
    db.commit()
    raise CustodyError(reason, detail)


def _check_eligibility(db, manifest):
    """只有旧表确实在指定时刻被新表取代、且无仍在生效的旧表 assignment 才可退役。"""
    slot, old_meter, new_meter = manifest['slot'], manifest['old_meter'], manifest['new_meter']
    replacement_at = at(manifest['replacement_at'])
    change = db.execute(
        'SELECT * FROM replacement WHERE slot=? AND old_meter=? AND new_meter=?',
        (slot, old_meter, new_meter)).fetchall()
    if len(change) != 1:
        raise CustodyError('replacement_not_found', f'{slot}:{old_meter}->{new_meter}')
    if at(change[0]['at']) != replacement_at:
        raise CustodyError('replacement_time_mismatch', change[0]['at'])
    active_old = db.execute(
        'SELECT * FROM assignment WHERE slot=? AND meter=? AND start<=? AND end>?',
        (slot, old_meter, manifest['replacement_at'], manifest['replacement_at'])).fetchall()
    if active_old:
        raise CustodyError('old_meter_still_installed', old_meter)
    active_new = db.execute(
        'SELECT * FROM assignment WHERE slot=? AND meter=? AND start<=? AND end>?',
        (slot, new_meter, manifest['replacement_at'], manifest['replacement_at'])).fetchall()
    if not active_new:
        raise CustodyError('new_meter_not_active', new_meter)
    return dict(change[0])


def _contract_window(manifest, contract):
    if contract not in manifest.get('contracts', {}):
        raise CustodyError('unknown_contract', contract)
    window_key = 'owner' if contract == 'return_to_owner' else 'lab'
    windows = manifest.get('handoff_windows', {}).get(window_key)
    if not windows or len(windows) != 2:
        raise CustodyError('window_not_configured', contract)
    return at(windows[0]), at(windows[1])


def retire_meter(db_path, handoff_config, fixtures_dir, request_id, actor=WAREHOUSE, occurred_at=None, source='cli'):
    """建立退役资产（状态：已拆下）。验证换表事实、assignment 与封条声明的唯一绑定。"""
    manifest = _load_manifest(handoff_config)
    occurred_at = (occurred_at or utcnow_iso())
    db = connect(db_path)
    db.isolation_level = None
    db.execute('BEGIN IMMEDIATE')
    try:
        replayed = _replay(db, request_id)
        if replayed is not None:
            db.commit()
            return replayed
        retired_id = None
        try:
            _check_eligibility(db, manifest)
        except CustodyError as exc:
            _fail(db, request_id, None, 'retire', exc.reason, exc.detail)
        existing = _asset_by_meter(db, manifest['old_meter'])
        if existing is not None:
            _fail(db, request_id, existing['id'], 'retire', 'asset_already_retired', manifest['old_meter'])
        seal_row = db.execute('SELECT 1 FROM retired_meter WHERE seal_id=?', (manifest['seal_id'],)).fetchone()
        if seal_row is not None:
            _fail(db, request_id, None, 'retire', 'seal_already_bound', manifest['seal_id'])
        cur = db.execute(
            'INSERT INTO retired_meter(slot,old_meter,new_meter,replacement_at,seal_id,seal_sha256,seal_file,created_at,created_by,source)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?)',
            (manifest['slot'], manifest['old_meter'], manifest['new_meter'], manifest['replacement_at'],
             manifest['seal_id'], manifest['seal_sha256'], manifest['seal_file'], occurred_at, actor, source))
        retired_id = cur.lastrowid
        _emit(db, retired_id, 'retire', None, '已拆下', actor, occurred_at, source, request_id,
              {'replacement_at': manifest['replacement_at'], 'seal_id': manifest['seal_id'],
               'seal_sha256': manifest['seal_sha256']})
        result = {'ok': True, 'operation': 'retire', 'retired_id': retired_id, 'old_meter': manifest['old_meter'],
                  'state': '已拆下', 'request_id': request_id, 'replayed': False}
        _record_request(db, request_id, retired_id, 'retire', True, result)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def seal_meter(db_path, handoff_config, fixtures_dir, request_id, actor=WAREHOUSE, occurred_at=None, source='cli'):
    """封存：验证真实封条文件字节哈希、封条号与旧表序列号唯一绑定（状态：已封存）。"""
    manifest = _load_manifest(handoff_config)
    occurred_at = (occurred_at or utcnow_iso())
    seal_path = _resolve_seal(manifest, fixtures_dir)
    db = connect(db_path)
    db.isolation_level = None
    db.execute('BEGIN IMMEDIATE')
    try:
        replayed = _replay(db, request_id)
        if replayed is not None:
            db.commit()
            return replayed
        asset_row = _asset_by_meter(db, manifest['old_meter'])
        if asset_row is None:
            _fail(db, request_id, None, 'seal', 'asset_not_found', manifest['old_meter'])
        asset = dict(asset_row)
        state = _current_state(db, asset['id'])
        if state != '已拆下':
            _fail(db, request_id, asset['id'], 'seal', 'illegal_transition', f'{state}->已封存')
        if not seal_path.is_file():
            _fail(db, request_id, asset['id'], 'seal', 'seal_file_missing', str(seal_path))
        digest = hashlib.sha256(seal_path.read_bytes()).hexdigest()
        if digest != manifest['seal_sha256']:
            _fail(db, request_id, asset['id'], 'seal', 'seal_hash_mismatch', digest)
        if digest != asset['seal_sha256'] or manifest['seal_sha256'] != asset['seal_sha256']:
            _fail(db, request_id, asset['id'], 'seal', 'seal_hash_mismatch', 'asset binding differs')
        if manifest['seal_id'] != asset['seal_id']:
            _fail(db, request_id, asset['id'], 'seal', 'seal_serial_mismatch', manifest['seal_id'])
        other = db.execute('SELECT 1 FROM retired_meter WHERE seal_id=? AND id<>?',
                           (manifest['seal_id'], asset['id'])).fetchone()
        if other is not None:
            _fail(db, request_id, asset['id'], 'seal', 'seal_already_bound', manifest['seal_id'])
        _emit(db, asset['id'], 'seal', '已拆下', '已封存', actor, occurred_at, source, request_id,
              {'seal_id': asset['seal_id'], 'seal_sha256': digest, 'seal_file': str(seal_path)})
        result = {'ok': True, 'operation': 'seal', 'retired_id': asset['id'], 'old_meter': asset['old_meter'],
                  'state': '已封存', 'seal_id': asset['seal_id'], 'seal_sha256': digest,
                  'request_id': request_id, 'replayed': False}
        _record_request(db, request_id, asset['id'], 'seal', True, result)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def issue_handoff(db_path, handoff_config, fixtures_dir, contract, request_id, actor=WAREHOUSE,
                  occurred_at=None, source='cli'):
    """发出交接单（两步交接的第一步），状态推进为待交接。仅发单不转移保管权。"""
    manifest = _load_manifest(handoff_config)
    occurred_at = (occurred_at or utcnow_iso())
    db = connect(db_path)
    db.isolation_level = None
    db.execute('BEGIN IMMEDIATE')
    try:
        replayed = _replay(db, request_id)
        if replayed is not None:
            db.commit()
            return replayed
        asset_row = _asset_by_meter(db, manifest['old_meter'])
        if asset_row is None:
            _fail(db, request_id, None, 'issue_handoff', 'asset_not_found', manifest['old_meter'])
        asset = dict(asset_row)
        state = _current_state(db, asset['id'])
        if state == '已签收':
            _fail(db, request_id, asset['id'], 'issue_handoff', 'asset_already_signed', asset['old_meter'])
        if state not in ('已封存', '待交接'):
            _fail(db, request_id, asset['id'], 'issue_handoff', 'illegal_transition', f'{state}->待交接')
        window_start, window_end = _contract_window(manifest, contract)
        consignee = manifest['contracts'][contract]['consignee']
        order_id = f"HO-{asset['old_meter']}-{contract}"
        existing_order = db.execute('SELECT * FROM handoff_order WHERE id=?', (order_id,)).fetchone()
        if existing_order is not None and existing_order['status'] != '待交接':
            _fail(db, request_id, asset['id'], 'issue_handoff', 'order_closed', order_id)
        if existing_order is None:
            db.execute(
                'INSERT INTO handoff_order(id,retired_id,contract,consignee,window_start,window_end,seal_id,seal_sha256,status,issued_at,issued_by,source)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (order_id, asset['id'], contract, consignee, window_start.isoformat(), window_end.isoformat(),
                 asset['seal_id'], asset['seal_sha256'], '待交接', occurred_at, actor, source))
        if state == '已封存':
            _emit(db, asset['id'], 'issue_handoff', '已封存', '待交接', actor, occurred_at, source, request_id,
                  {'order_id': order_id, 'contract': contract, 'consignee': consignee,
                   'window': [window_start.isoformat(), window_end.isoformat()]})
        result = {'ok': True, 'operation': 'issue_handoff', 'retired_id': asset['id'],
                  'old_meter': asset['old_meter'], 'order_id': order_id, 'contract': contract,
                  'consignee': consignee, 'state': _current_state(db, asset['id']),
                  'request_id': request_id, 'replayed': False}
        _record_request(db, request_id, asset['id'], 'issue_handoff', True, result)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def submit_credential(db_path, handoff_config, fixtures_dir, order_id, direction, request_id,
                      actor=None, occurred_at=None, seal_id=None, seal_sha256=None, source='cli'):
    """提交交接凭据。dispatch=仓管发出凭据，receipt=接收方签收凭据，双向闭合才转移保管权。"""
    manifest = _load_manifest(handoff_config)
    occurred_at = (occurred_at or utcnow_iso())
    if direction not in DIRECTIONS:
        raise CustodyError('unknown_direction', direction)
    db = connect(db_path)
    db.isolation_level = None
    db.execute('BEGIN IMMEDIATE')
    try:
        replayed = _replay(db, request_id)
        if replayed is not None:
            db.commit()
            return replayed
        order_row = db.execute('SELECT * FROM handoff_order WHERE id=?', (order_id,)).fetchone()
        if order_row is None:
            _fail(db, request_id, None, 'submit_credential', 'order_not_found', order_id)
        order = dict(order_row)
        asset = _asset(db, order['retired_id'])
        state = _current_state(db, asset['id'])
        # 终态判定先于一切写操作：已闭合后任何凭据都不能改变持有人
        if order['status'] == '已作废' and state == '已签收':
            # 竞领失败：竞争单已被胜出的签收闭合作废，败方留冲突记录
            detail = {'order_id': order_id, 'direction': direction, 'actor': actor}
            db.execute(
                'INSERT INTO custody_conflict(retired_id,order_id,request_id,actor,contract,reason,occurred_at,detail)'
                ' VALUES (?,?,?,?,?,?,?,?)',
                (asset['id'], order_id, request_id, actor or 'UNKNOWN', order['contract'],
                 'signoff_lost', occurred_at, json.dumps(detail, ensure_ascii=False, sort_keys=True)))
            _record_request(db, request_id, asset['id'], 'submit_credential', False,
                            {'ok': False, 'reason': 'order_superseded', 'detail': order_id})
            db.commit()
            raise CustodyError('order_superseded', order_id)
        if order['status'] == '已作废':
            _fail(db, request_id, asset['id'], 'submit_credential', 'order_superseded', order_id)
        if state == '已签收':
            detail = {'order_id': order_id, 'direction': direction, 'actor': actor, 'winner_order': order_id}
            db.execute(
                'INSERT INTO custody_conflict(retired_id,order_id,request_id,actor,contract,reason,occurred_at,detail)'
                ' VALUES (?,?,?,?,?,?,?,?)',
                (asset['id'], order_id, request_id, actor or 'UNKNOWN', order['contract'],
                 'signoff_lost', occurred_at, json.dumps(detail, ensure_ascii=False, sort_keys=True)))
            _record_request(db, request_id, asset['id'], 'submit_credential', False,
                            {'ok': False, 'reason': 'signoff_lost', 'detail': order_id})
            db.commit()
            raise CustodyError('signoff_lost', order_id)
        if state != '待交接':
            _fail(db, request_id, asset['id'], 'submit_credential', 'illegal_transition', f'{state}->已签收')
        expected_actor = WAREHOUSE if direction == 'dispatch' else order['consignee']
        actor = actor or expected_actor
        if actor != expected_actor:
            _fail(db, request_id, asset['id'], 'submit_credential', 'actor_not_authorized',
                  f'{actor}!={expected_actor}')
        bound_seal = seal_id or asset['seal_id']
        bound_hash = seal_sha256 or asset['seal_sha256']
        if bound_seal != asset['seal_id'] or order['seal_id'] != asset['seal_id']:
            _fail(db, request_id, asset['id'], 'submit_credential', 'seal_serial_mismatch', str(bound_seal))
        if bound_hash != asset['seal_sha256'] or order['seal_sha256'] != asset['seal_sha256']:
            _fail(db, request_id, asset['id'], 'submit_credential', 'seal_hash_mismatch', str(bound_hash))
        arrival = at(occurred_at)
        window_start, window_end = at(order['window_start']), at(order['window_end'])
        if direction == 'receipt' and not (window_start <= arrival <= window_end):
            _fail(db, request_id, asset['id'], 'submit_credential', 'outside_handoff_window', occurred_at)
        dup = db.execute('SELECT 1 FROM handoff_credential WHERE order_id=? AND direction=?',
                         (order_id, direction)).fetchone()
        if dup is not None:
            _fail(db, request_id, asset['id'], 'submit_credential', 'credential_already_recorded',
                  f'{order_id}:{direction}')
        db.execute(
            'INSERT INTO handoff_credential(order_id,retired_id,direction,actor,seal_id,seal_sha256,arrived_at,request_id)'
            ' VALUES (?,?,?,?,?,?,?,?)',
            (order_id, asset['id'], direction, actor, bound_seal, bound_hash, occurred_at, request_id))
        closed = db.execute(
            'SELECT count(*) AS n FROM handoff_credential WHERE order_id=?', (order_id,)).fetchone()['n'] == 2
        result = {'ok': True, 'operation': 'submit_credential', 'retired_id': asset['id'],
                  'old_meter': asset['old_meter'], 'order_id': order_id, 'direction': direction,
                  'actor': actor, 'closed': closed, 'state': state, 'request_id': request_id, 'replayed': False}
        if closed:
            # 双向凭据在合同时窗内闭合：保管权此刻才转移
            _emit(db, asset['id'], 'signoff', '待交接', '已签收', actor, occurred_at, source, request_id,
                  {'order_id': order_id, 'contract': order['contract'], 'consignee': order['consignee'],
                   'seal_id': bound_seal, 'seal_sha256': bound_hash})
            db.execute("UPDATE handoff_order SET status='已签收' WHERE id=?", (order_id,))
            db.execute("UPDATE handoff_order SET status='已作废' WHERE retired_id=? AND id<>?",
                       (asset['id'], order_id))
            result['state'] = '已签收'
            result['holder'] = order['consignee']
        else:
            _emit(db, asset['id'], 'credential', '待交接', '待交接', actor, occurred_at, source, request_id,
                  {'order_id': order_id, 'direction': direction, 'consignee': order['consignee']})
        _record_request(db, request_id, asset['id'], 'submit_credential', True, result)
        db.commit()
        return result
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise CustodyError('custody_conflict', str(exc))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def custody_view(db_path, old_meter):
    """输出旧表实际持有人与完整转移链（只读取自追加流水）。"""
    db = connect(db_path)
    try:
        asset_row = _asset_by_meter(db, old_meter)
        if asset_row is None:
            raise CustodyError('asset_not_found', old_meter)
        asset = dict(asset_row)
        events = [dict(r) for r in db.execute(
            'SELECT seq,action,state_from,state_to,actor,occurred_at,source,request_id,detail FROM custody_event'
            ' WHERE retired_id=? ORDER BY seq', (asset['id'],))]
        for ev in events:
            ev['detail'] = json.loads(ev['detail'])
        state = _current_state(db, asset['id'])
        orders = [dict(r) for r in db.execute(
            'SELECT id,contract,consignee,window_start,window_end,status,issued_at FROM handoff_order'
            ' WHERE retired_id=? ORDER BY id', (asset['id'],))]
        credentials = [dict(r) for r in db.execute(
            'SELECT order_id,direction,actor,seal_id,seal_sha256,arrived_at FROM handoff_credential'
            ' WHERE retired_id=? ORDER BY id', (asset['id'],))]
        conflicts = [dict(r) for r in db.execute(
            'SELECT order_id,actor,contract,reason,occurred_at,detail FROM custody_conflict'
            ' WHERE retired_id=? ORDER BY id', (asset['id'],))]
        for c in conflicts:
            c['detail'] = json.loads(c['detail'])
        signoff = next((ev for ev in events if ev['action'] == 'signoff'), None)
        holder = WAREHOUSE
        holder_since = events[0]['occurred_at'] if events else None
        quarantine = False
        if signoff is not None:
            holder = signoff['detail']['consignee']
            holder_since = signoff['occurred_at']
            quarantine = signoff['detail']['contract'] == 'quarantine_at_lab'
        return {
            'old_meter': asset['old_meter'], 'slot': asset['slot'], 'new_meter': asset['new_meter'],
            'replacement_at': asset['replacement_at'], 'seal_id': asset['seal_id'],
            'seal_sha256': asset['seal_sha256'], 'state': state, 'holder': holder,
            'holder_since': holder_since, 'quarantine': quarantine,
            'owner_can_collect': holder != 'METROLOGY-LAB' or not quarantine,
            'chain': events, 'orders': orders, 'credentials': credentials, 'conflicts': conflicts,
        }
    finally:
        db.close()

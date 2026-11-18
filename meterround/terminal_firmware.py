"""Local handheld terminal endpoint plus crash-safe fleet rollout coordination.

`register`/`invoke`/`state` are the single-terminal primitives. On top of them,
`rollout` drives a whole case under a chosen recovery contract (`fleet_atomic`
or `cell_isolated`): every dispatch is journaled with a stable operation id and
expected revision before it is sent, lost acknowledgements are verified by
replaying the same operation id against the endpoint and cross-checking field
state, and any crash is resumed from the local journal plus live field
revision/version without re-installing, skipping devices, or missing rollbacks.
"""

import hashlib
import json
from pathlib import Path

from .store import connect


def register(db_path, case_id, fixture):
    path = Path(fixture).resolve()
    raw = path.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('target_version') or not doc.get('devices'):
        raise ValueError('bad_firmware_case')
    db = connect(db_path)
    try:
        with db:
            for item in doc['devices']:
                source = db.execute(
                    '''SELECT s.sha256 FROM reading r JOIN source s ON s.id=r.source_id
                       WHERE r.device=? ORDER BY r.seq LIMIT 1''', (item['id'],)
                ).fetchone()
                if not source or source['sha256'] != item['reading_source_sha256']:
                    raise ValueError('firmware_reading_source_missing')
                old = db.execute(
                    'SELECT cell,version,revision FROM terminal_firmware_device WHERE id=?',
                    (item['id'],),
                ).fetchone()
                expected = {'cell': item['cell'], 'version': item['current_version'], 'revision': 0}
                if old and dict(old) != expected:
                    raise ValueError('firmware_device_conflict')
                db.execute(
                    'INSERT OR IGNORE INTO terminal_firmware_device VALUES (?,?,?,0)',
                    (item['id'], item['cell'], item['current_version']),
                )
            digest = hashlib.sha256(raw).hexdigest()
            old = db.execute('SELECT digest FROM terminal_firmware_case WHERE id=?', (case_id,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('firmware_case_conflict')
            db.execute(
                'INSERT OR IGNORE INTO terminal_firmware_case VALUES (?,?,?)',
                (case_id, json.dumps(doc, sort_keys=True), digest),
            )
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest, 'devices': len(doc['devices'])}


def state(db_path, device):
    db = connect(db_path)
    try:
        row = db.execute(
            'SELECT id,cell,version,revision FROM terminal_firmware_device WHERE id=?',
            (device,),
        ).fetchone()
        if not row:
            raise ValueError('unknown_firmware_device')
        return dict(row)
    finally:
        db.close()


def invoke(db_path, case_id, device, command, version, operation_id, expected_revision):
    if command not in ('install', 'rollback'):
        raise ValueError('bad_firmware_command')
    db = connect(db_path)
    try:
        with db:
            prior = db.execute(
                'SELECT result FROM terminal_firmware_operation WHERE id=?', (operation_id,)
            ).fetchone()
            if prior:
                return {'status': 'replayed', 'record': json.loads(prior['result'])}
            row = db.execute(
                'SELECT version,revision FROM terminal_firmware_device WHERE id=?', (device,)
            ).fetchone()
            if not row:
                raise ValueError('unknown_firmware_device')
            if row['revision'] != expected_revision:
                return {'status': 'revision_conflict', 'current': dict(row)}
            case = db.execute(
                'SELECT payload FROM terminal_firmware_case WHERE id=?', (case_id,)
            ).fetchone()
            if not case:
                raise ValueError('unknown_firmware_case')
            doc = json.loads(case['payload'])
            spec = next((x for x in doc['devices'] if x['id'] == device), None)
            if not spec:
                raise ValueError('device_not_in_case')
            if command == 'install' and version != doc['target_version']:
                raise ValueError('target_version_mismatch')
            if command == 'rollback' and version != spec['current_version']:
                raise ValueError('rollback_version_mismatch')
            if spec.get('failure') == 'unreachable':
                return {'status': 'unreachable'}
            if command == 'install' and spec.get('failure') == 'reject':
                result = {'status': 'rejected', 'version': row['version'], 'revision': row['revision']}
            else:
                db.execute(
                    'UPDATE terminal_firmware_device SET version=?,revision=revision+1 WHERE id=?',
                    (version, device),
                )
                result = {'status': 'applied', 'version': version, 'revision': row['revision'] + 1}
            db.execute(
                'INSERT INTO terminal_firmware_operation VALUES (?,?,?,?)',
                (operation_id, device, command, json.dumps(result, sort_keys=True)),
            )
            if command == 'install' and spec.get('failure') == 'commit_then_ack_lost':
                return {'status': 'ack_lost'}
            return result
    finally:
        db.close()


CONTRACTS = ('fleet_atomic', 'cell_isolated')
ROUND_FINAL = ('completed', 'partial', 'rolled_back', 'manual_review')
STEP_OPEN = ('pending', 'dispatched', 'ack_lost')
STEP_UPGRADED = ('applied', 'verified')


def _journal(db, round_id, device, event, detail):
    db.execute(
        'INSERT INTO terminal_firmware_journal(round_id,device,event,detail) VALUES (?,?,?,?)',
        (round_id, device, event, json.dumps(detail, ensure_ascii=False, sort_keys=True)),
    )


def _round_row(db, round_id):
    return db.execute('SELECT * FROM terminal_firmware_round WHERE id=?', (round_id,)).fetchone()


def _case_doc(db, case_id):
    row = db.execute(
        'SELECT payload,digest FROM terminal_firmware_case WHERE id=?', (case_id,)
    ).fetchone()
    if not row:
        raise ValueError('unknown_firmware_case')
    return json.loads(row['payload']), row['digest']


def _field_state(db, device):
    row = db.execute(
        'SELECT id,cell,version,revision FROM terminal_firmware_device WHERE id=?', (device,)
    ).fetchone()
    if not row:
        raise ValueError('unknown_firmware_device')
    return dict(row)


def _reading_source(db, device):
    return db.execute(
        '''SELECT r.slot AS slot, s.sha256 AS sha256 FROM reading r
           JOIN source s ON s.id=r.source_id WHERE r.device=? ORDER BY r.seq LIMIT 1''',
        (device,),
    ).fetchone()


def _checkpoint(hook, point, step):
    if hook:
        hook(point, {
            'round_id': step['round_id'], 'device': step['device'], 'phase': step['phase'],
            'command': step['command'], 'operation_id': step['operation_id'],
            'version': step['version'],
        })


def _create_round(db, round_id, case_id, contract):
    doc, digest = _case_doc(db, case_id)
    target = doc['target_version']
    with db:
        revisions = {}
        for item in doc['devices']:
            dev = db.execute(
                'SELECT version,revision FROM terminal_firmware_device WHERE id=?', (item['id'],)
            ).fetchone()
            if not dev:
                raise ValueError('unknown_firmware_device')
            src = _reading_source(db, item['id'])
            if not src or src['sha256'] != item['reading_source_sha256']:
                raise ValueError('firmware_reading_source_missing')
            if dev['version'] != item['current_version']:
                raise ValueError('firmware_plan_conflict')
            revisions[item['id']] = dev['revision']
        db.execute(
            '''INSERT INTO terminal_firmware_round(id,case_id,contract,status,created_at,detail)
               VALUES (?,?,?,'upgrading',CURRENT_TIMESTAMP,?)''',
            (round_id, case_id, contract, json.dumps(
                {'aborted_cells': [], 'fleet_aborted': False, 'target_version': target,
                 'case_digest': digest}, sort_keys=True)),
        )
        for seq, item in enumerate(doc['devices']):
            db.execute(
                '''INSERT INTO terminal_firmware_step
                   (round_id,device,seq,phase,cell,command,version,operation_id,expected_revision,status)
                   VALUES (?,?,?,?,?,?,?,?,?,'pending')''',
                (round_id, item['id'], seq, 'upgrade', item['cell'], 'install', target,
                 f"{round_id}:{item['id']}:upgrade", revisions[item['id']]),
            )
        _journal(db, round_id, None, 'round_planned', {
            'case_id': case_id, 'contract': contract, 'target_version': target,
            'devices': [x['id'] for x in doc['devices']]})


def _abort_scope(db, round_id, contract, step, cause):
    detail = json.loads(_round_row(db, round_id)['detail'])
    if contract == 'fleet_atomic':
        db.execute(
            "UPDATE terminal_firmware_step SET status='skipped' WHERE round_id=? AND phase='upgrade' AND status='pending'",
            (round_id,),
        )
        detail['fleet_aborted'] = True
    else:
        db.execute(
            "UPDATE terminal_firmware_step SET status='skipped' WHERE round_id=? AND phase='upgrade' AND status='pending' AND cell=?",
            (round_id, step['cell']),
        )
        cells = set(detail.get('aborted_cells', []))
        cells.add(step['cell'])
        detail['aborted_cells'] = sorted(cells)
    db.execute('UPDATE terminal_firmware_round SET detail=? WHERE id=?',
               (json.dumps(detail, sort_keys=True), round_id))
    _journal(db, round_id, step['device'], 'abort',
             {'cause': cause, 'contract': contract, 'cell': step['cell']})


def _live_versions(db, case_id):
    doc, _ = _case_doc(db, case_id)
    return {item['id']: {'version': _field_state(db, item['id'])['version'],
                         'revision': _field_state(db, item['id'])['revision']}
            for item in doc['devices']}


def _halt_manual(db, round_id, reason):
    detail = json.loads(_round_row(db, round_id)['detail'])
    detail['final_versions'] = _live_versions(db, _round_row(db, round_id)['case_id'])
    detail['manual_reason'] = reason
    db.execute(
        "UPDATE terminal_firmware_round SET status='manual_review',ended_at=CURRENT_TIMESTAMP,detail=? WHERE id=?",
        (json.dumps(detail, sort_keys=True), round_id),
    )
    _journal(db, round_id, None, 'manual_review', {'reason': reason})


def _record_response(db_path, rnd, step, response, recovered):
    status = response['status']
    verified = None
    note = None
    if status == 'replayed':
        record = response['record']
        field = state(db_path, step['device'])
        verified = {'via': 'operation_replay', 'record': record, 'field': field}
        if (record.get('status') == 'applied' and field['version'] == step['version']
                and field['revision'] == record.get('revision')):
            status, note = 'applied', 'recovered_committed'
        elif record.get('status') == 'rejected':
            status, note = 'rejected', 'recovered_rejected'
        else:
            status, note = 'manual', 'replay_state_mismatch'
    elif status == 'revision_conflict':
        status, note = 'manual', 'revision_conflict'
    elif recovered and status != 'ack_lost':
        note = 'recovered_undelivered'
    db = connect(db_path)
    try:
        with db:
            db.execute(
                'UPDATE terminal_firmware_step SET status=?,response=?,verified=? WHERE id=?',
                (status, json.dumps(response, sort_keys=True),
                 json.dumps(verified, sort_keys=True) if verified else None, step['id']),
            )
            _journal(db, rnd['id'], step['device'], 'response', {
                'phase': step['phase'], 'operation_id': step['operation_id'],
                'status': status, 'note': note, 'response': response})
            if status == 'manual':
                _halt_manual(db, rnd['id'], f"{step['phase']}:{step['device']}:{note}")
            elif status in ('rejected', 'unreachable'):
                if step['phase'] == 'upgrade':
                    _abort_scope(db, rnd['id'], rnd['contract'], step, status)
                else:
                    _halt_manual(db, rnd['id'], f"rollback:{step['device']}:{status}")
    finally:
        db.close()


def _verify_step(db_path, rnd, step, hook):
    _checkpoint(hook, 'verify', step)
    res = invoke(db_path, rnd['case_id'], step['device'], step['command'], step['version'],
                 step['operation_id'], step['expected_revision'])
    _checkpoint(hook, 'verify_response', step)
    field = state(db_path, step['device'])
    if res['status'] == 'replayed':
        record = res['record']
        ok = (record.get('status') == 'applied' and field['version'] == step['version']
              and field['revision'] == record.get('revision'))
        verified = {'via': 'operation_replay', 'record': record, 'field': field}
        new_status = 'verified' if ok else 'manual'
    else:
        verified = {'via': 'operation_replay', 'unexpected': res, 'field': field}
        new_status = 'manual'
    db = connect(db_path)
    try:
        with db:
            db.execute('UPDATE terminal_firmware_step SET status=?,verified=? WHERE id=?',
                       (new_status, json.dumps(verified, sort_keys=True), step['id']))
            _journal(db, rnd['id'], step['device'], 'verify', {
                'operation_id': step['operation_id'], 'status': new_status, 'verified': verified})
            if new_status == 'manual':
                _halt_manual(db, rnd['id'], f"verify:{step['device']}")
    finally:
        db.close()


def _execute_step(db_path, rnd, step, hook):
    if step['status'] == 'pending':
        db = connect(db_path)
        try:
            with db:
                db.execute(
                    "UPDATE terminal_firmware_step SET status='dispatched' WHERE id=? AND status='pending'",
                    (step['id'],),
                )
                _journal(db, rnd['id'], step['device'], 'dispatch', {
                    'phase': step['phase'], 'operation_id': step['operation_id'],
                    'command': step['command'], 'version': step['version'],
                    'expected_revision': step['expected_revision']})
        finally:
            db.close()
        _checkpoint(hook, 'dispatch', step)
        response = invoke(db_path, rnd['case_id'], step['device'], step['command'],
                          step['version'], step['operation_id'], step['expected_revision'])
        _checkpoint(hook, 'response', step)
        _record_response(db_path, rnd, step, response, recovered=False)
    elif step['status'] == 'dispatched':
        db = connect(db_path)
        try:
            with db:
                _journal(db, rnd['id'], step['device'], 'recover_dispatch', {
                    'phase': step['phase'], 'operation_id': step['operation_id']})
        finally:
            db.close()
        response = invoke(db_path, rnd['case_id'], step['device'], step['command'],
                          step['version'], step['operation_id'], step['expected_revision'])
        _checkpoint(hook, 'response', step)
        _record_response(db_path, rnd, step, response, recovered=True)
    elif step['status'] == 'ack_lost':
        _verify_step(db_path, rnd, step, hook)
    else:
        raise ValueError('bad_step_status')


def _conclude_upgrade(db, round_id):
    rnd = _round_row(db, round_id)
    steps = [dict(s) for s in db.execute(
        "SELECT * FROM terminal_firmware_step WHERE round_id=? AND phase='upgrade' ORDER BY seq",
        (round_id,))]
    detail = json.loads(rnd['detail'])
    upgraded = [s for s in steps if s['status'] in STEP_UPGRADED]
    if rnd['contract'] == 'fleet_atomic':
        failed = any(s['status'] not in STEP_UPGRADED for s in steps)
        need = upgraded if failed else []
    else:
        aborted = set(detail.get('aborted_cells', []))
        need = [s for s in upgraded if s['cell'] in aborted]
    if need:
        doc, _ = _case_doc(db, rnd['case_id'])
        base = {x['id']: x['current_version'] for x in doc['devices']}
        for s in need:
            rev = _field_state(db, s['device'])['revision']
            db.execute(
                '''INSERT INTO terminal_firmware_step
                   (round_id,device,seq,phase,cell,command,version,operation_id,expected_revision,status)
                   VALUES (?,?,?,?,?,?,?,?,?,'pending')''',
                (round_id, s['device'], s['seq'], 'rollback', s['cell'], 'rollback',
                 base[s['device']], f"{round_id}:{s['device']}:rollback", rev),
            )
        db.execute("UPDATE terminal_firmware_round SET status='rolling_back' WHERE id=?", (round_id,))
        _journal(db, round_id, None, 'rollback_planned',
                 {'devices': [s['device'] for s in need]})
    else:
        _finalize(db, round_id)


def _finalize(db, round_id):
    rnd = _round_row(db, round_id)
    doc, _ = _case_doc(db, rnd['case_id'])
    detail = json.loads(rnd['detail'])
    live = _live_versions(db, rnd['case_id'])
    detail['final_versions'] = live
    target = doc['target_version']
    steps = [dict(s) for s in db.execute(
        'SELECT * FROM terminal_firmware_step WHERE round_id=?', (round_id,))]
    if any(s['status'] == 'manual' for s in steps):
        status = 'manual_review'
    elif rnd['contract'] == 'fleet_atomic':
        bases = {x['id']: x['current_version'] for x in doc['devices']}
        if {v['version'] for v in live.values()} == {target}:
            status = 'completed'
        elif all(live[d]['version'] == bases[d] for d in live):
            status = 'rolled_back'
        else:
            status = 'manual_review'
    else:
        cells = {}
        for item in doc['devices']:
            cells.setdefault(item['cell'], []).append(item['id'])
        uniform = all(len({live[d]['version'] for d in ids}) == 1 for ids in cells.values())
        if not uniform:
            status = 'manual_review'
        elif all(live[d]['version'] == target for ids in cells.values() for d in ids):
            status = 'completed'
        else:
            status = 'partial'
    db.execute(
        'UPDATE terminal_firmware_round SET status=?,ended_at=CURRENT_TIMESTAMP,detail=? WHERE id=?',
        (status, json.dumps(detail, sort_keys=True), round_id),
    )
    _journal(db, round_id, None, 'finalized', {'status': status, 'final_versions': live})


def _next_step(db, round_id, phase, order):
    return db.execute(
        f'''SELECT * FROM terminal_firmware_step WHERE round_id=? AND phase=?
            AND status IN ('pending','dispatched','ack_lost') ORDER BY seq {order} LIMIT 1''',
        (round_id, phase),
    ).fetchone()


def _step_once(db_path, round_id, case_id, contract, hook):
    db = connect(db_path)
    step = None
    try:
        rnd = _round_row(db, round_id)
        if rnd is None:
            if case_id is None or contract is None:
                raise ValueError('unknown_firmware_round')
            _create_round(db, round_id, case_id, contract)
            return None
        if rnd['status'] in ROUND_FINAL:
            return _build_report(db, rnd)
        if rnd['status'] == 'upgrading':
            step = _next_step(db, round_id, 'upgrade', 'ASC')
            if step is None:
                with db:
                    _conclude_upgrade(db, round_id)
                return None
        elif rnd['status'] == 'rolling_back':
            step = _next_step(db, round_id, 'rollback', 'DESC')
            if step is None:
                with db:
                    _finalize(db, round_id)
                return None
        else:
            raise ValueError('bad_round_status')
    finally:
        db.close()
    _execute_step(db_path, rnd, step, hook)
    return None


def rollout(db_path, case_id=None, contract=None, round_id=None, crash_hook=None):
    if round_id is None:
        if not case_id or contract not in CONTRACTS:
            raise ValueError('bad_rollout_request')
        round_id = f'{case_id}:{contract}'
    while True:
        report = _step_once(db_path, round_id, case_id, contract, crash_hook)
        if report is not None:
            return report


def _step_brief(s):
    if not s:
        return None
    return {
        'operation_id': s['operation_id'],
        'command': s['command'],
        'version': s['version'],
        'expected_revision': s['expected_revision'],
        'status': s['status'],
        'response': json.loads(s['response']) if s['response'] else None,
        'recheck': json.loads(s['verified']) if s['verified'] else None,
    }


def _build_report(db, rnd):
    doc, digest = _case_doc(db, rnd['case_id'])
    steps = [dict(s) for s in db.execute(
        'SELECT * FROM terminal_firmware_step WHERE round_id=? ORDER BY seq,phase', (rnd['id'],))]
    by_device = {}
    for s in steps:
        by_device.setdefault(s['device'], {})[s['phase']] = s
    devices = []
    for item in doc['devices']:
        dev = item['id']
        field = _field_state(db, dev)
        src = _reading_source(db, dev)
        pair = by_device.get(dev, {})
        up, rb = pair.get('upgrade'), pair.get('rollback')
        devices.append({
            'device': dev,
            'slot': src['slot'] if src else None,
            'reading_source_sha256': src['sha256'] if src else None,
            'cell': item['cell'],
            'base_version': item['current_version'],
            'target_version': doc['target_version'],
            'actual_version': field['version'],
            'actual_revision': field['revision'],
            'install': _step_brief(up),
            'rollback': _step_brief(rb),
            'manual': any((x or {}).get('status') == 'manual' for x in (up, rb)),
        })
    cells = []
    grouped = {}
    for item in doc['devices']:
        grouped.setdefault(item['cell'], []).append(item['id'])
    actual = {d['device']: d['actual_version'] for d in devices}
    for cell, ids in grouped.items():
        versions = {actual[d] for d in ids}
        cells.append({
            'cell': cell,
            'devices': ids,
            'uniform': len(versions) == 1,
            'current_version': next(iter(versions)) if len(versions) == 1 else None,
            'complete': versions == {doc['target_version']},
        })
    journal = [dict(j) for j in db.execute(
        'SELECT id,device,event,detail,at FROM terminal_firmware_journal WHERE round_id=? ORDER BY id',
        (rnd['id'],))]
    return {
        'round_id': rnd['id'],
        'case_id': rnd['case_id'],
        'case_digest': digest,
        'contract': rnd['contract'],
        'status': rnd['status'],
        'target_version': doc['target_version'],
        'created_at': rnd['created_at'],
        'ended_at': rnd['ended_at'],
        'detail': json.loads(rnd['detail']),
        'cells': cells,
        'devices': devices,
        'journal': journal,
    }


def firmware_report(db_path, round_id, out=None):
    db = connect(db_path)
    try:
        rnd = _round_row(db, round_id)
        if not rnd:
            raise ValueError('unknown_firmware_round')
        data = _build_report(db, rnd)
    finally:
        db.close()
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return path
    return data

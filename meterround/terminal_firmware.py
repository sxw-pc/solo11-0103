"""Local handheld terminal endpoint; fleet rollout coordination is intentionally absent."""

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

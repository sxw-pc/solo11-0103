"""Freeze overlapping original CSV sources for a storage exercise; no migration."""
import hashlib
import json
from pathlib import Path
from .store import connect


def register(db_path, case_id, source_a, source_b):
    first = Path(source_a).resolve()
    second = Path(source_b).resolve()
    a = first.read_bytes()
    b = second.read_bytes()
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    if len(a_lines) < 10 or len(b_lines) != len(a_lines) + 1:
        raise ValueError('archive_case_shape')
    if a_lines[0] != b_lines[0] or a_lines[1:] != b_lines[2:]:
        raise ValueError('archive_case_shared_records')
    db = connect(db_path)
    try:
        rows = []
        for path, raw in ((first, a), (second, b)):
            digest = hashlib.sha256(raw).hexdigest()
            row = db.execute('SELECT id,name,sha256,length(raw) AS byte_length '
                             'FROM source WHERE sha256=?', (digest,)).fetchone()
            if row is None or Path(row['name']).resolve() != path or row['byte_length'] != len(raw):
                raise ValueError('archive_source_not_ingested')
            evidence = [dict(item) for item in db.execute(
                'SELECT r.id,r.line_no,r.device,r.seq,e.source_sha256 '
                'FROM reading r JOIN evidence e ON e.reading_id=r.id '
                'WHERE r.source_id=? ORDER BY r.line_no', (row['id'],))]
            if not evidence:
                raise ValueError('archive_source_no_evidence')
            rows.append({'source': dict(row), 'readings': evidence})
        payload = json.dumps({'sources': rows, 'shared_records': len(a_lines)-1,
                              'a_path': str(first), 'b_path': str(second)},
                             sort_keys=True, separators=(',', ':'))
        with db:
            old = db.execute('SELECT payload FROM archive_case WHERE id=?', (case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('archive_case_conflict')
            db.execute('INSERT OR IGNORE INTO archive_case VALUES (?,?)', (case_id, payload))
    finally:
        db.close()
    return {'case_id': case_id, 'sources': 2, 'shared_records': len(a_lines)-1}

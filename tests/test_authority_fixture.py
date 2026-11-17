from pathlib import Path
import hashlib
import json

from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.store import connect


def test_authority_fixture_has_real_conflicting_capture(tmp_path):
    p = Path(__file__).parents[1] / 'fixtures'
    case = json.loads((p / 'reading_authority.json').read_text(encoding='utf-8'))
    for source in case['sources']:
        assert hashlib.sha256((p / source['file']).read_bytes()).hexdigest() == source['sha256']
    db = tmp_path / 'capture.db'
    initialize(db, p / 'config.json')
    assert ingest(p / 'capture_original.csv', db)['imported'] == 2
    assert ingest(p / 'capture_correction.csv', db)['imported'] == 0
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM issue WHERE code='identity_conflict'").fetchone()[0] == 1
    assert con.execute('SELECT value FROM reading WHERE device=? AND seq=?', ('H1', 2)).fetchone()[0] == 150.0
    con.close()

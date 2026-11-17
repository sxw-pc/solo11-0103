from pathlib import Path
import hashlib
import json

from meterround.__main__ import initialize
from meterround.store import connect


def test_stock_fixture_links_installed_meters_and_certificates(tmp_path):
    p = Path(__file__).parents[1] / 'fixtures'
    case = json.loads((p / 'stock_program.json').read_text(encoding='utf-8'))
    db = tmp_path / 'stock.db'
    initialize(db, p / 'stock_config.json')
    con = connect(db)
    actual = {(r['slot'], r['meter']) for r in con.execute('SELECT slot,meter FROM assignment')}
    assert {(slot, old) for req in case['requests'] for slot, old in zip(req['slots'], req['old_meters'])} == actual
    con.close()
    for meter in case['stock']:
        raw = (p / 'meter_certificates' / meter['certificate_file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == meter['certificate_sha256']

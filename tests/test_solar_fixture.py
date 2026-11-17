from pathlib import Path
import hashlib
import json

from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect


def test_solar_fixture_has_closed_generation_balance(tmp_path):
    p = Path(__file__).parents[1] / 'fixtures'
    case = json.loads((p / 'solar_attribute_contract.json').read_text(encoding='utf-8'))
    assert hashlib.sha256((p / case['contract_file']).read_bytes()).hexdigest() == case['contract_sha256']
    db = tmp_path / 'solar.db'
    initialize(db, p / 'solar_config.json')
    assert ingest(p / 'solar_readings.csv', db)['imported'] == 6
    evaluate(db)
    con = connect(db)
    usage = {r['slot']: r['usage'] for r in con.execute('SELECT slot,usage FROM decision WHERE status=?', ('可结算',))}
    assert usage == {'PV-GEN': 100.0, 'PV-SELF': 40.0, 'PV-EXPORT': 60.0}
    assert usage['PV-GEN'] == usage['PV-SELF'] + usage['PV-EXPORT']
    con.close()

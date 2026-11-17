from pathlib import Path
import hashlib
import json

from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect


def test_retired_meter_fixture_has_real_replacement_and_seal(tmp_path):
    p = Path(__file__).parents[1] / 'fixtures'
    case = json.loads((p / 'retired_meter_handoff.json').read_text(encoding='utf-8'))
    assert hashlib.sha256((p / case['seal_file']).read_bytes()).hexdigest() == case['seal_sha256']
    db = tmp_path / 'round.db'
    initialize(db, p / 'retirement_config.json')
    assert ingest(p / 'retirement_readings.csv', db)['imported'] == 2
    assert evaluate(db)['A-01'] == '可结算'
    con = connect(db)
    row = con.execute('SELECT old_meter,new_meter,at FROM replacement').fetchone()
    assert (row['old_meter'], row['new_meter'], row['at']) == ('M1', 'M2', case['replacement_at'])
    con.close()

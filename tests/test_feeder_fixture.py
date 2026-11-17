from pathlib import Path
import json

from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect


def test_feeder_fixture_loads(tmp_path):
    p = Path(__file__).parents[1] / 'fixtures'
    topology = json.loads((p / 'feeder_topology.json').read_text(encoding='utf-8'))
    assert len(topology['edges']) == 4
    db = tmp_path / 'feeder.db'
    initialize(db, p / 'feeder_config.json')
    assert ingest(p / 'feeder_readings.csv', db)['imported'] == 10
    assert len(evaluate(db)) == 5
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 10
    con.close()

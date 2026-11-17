from pathlib import Path
import json

from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.report import report


def test_terminal_disclosure_fixture_exposes_device_ids(tmp_path):
    p = Path(__file__).parents[1] / 'fixtures'
    request = json.loads((p / 'terminal_disclosure_requests.json').read_text(encoding='utf-8'))
    db = tmp_path / 'round.db'
    initialize(db, p / 'terminal_disclosure_config.json')
    assert ingest(p / 'terminal_disclosure_readings.csv', db)['imported'] == 8
    raw = report(db, tmp_path / 'report.json').read_text(encoding='utf-8')
    assert 'H2-A3' in raw and 'H1-A4' in raw
    assert request['previous_release']['count'] == 1
    assert request['new_request']['slots'] == ['A1', 'A2', 'A3']

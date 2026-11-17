from pathlib import Path
import json

from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect


def test_billing_fixture_has_three_settleable_slots(tmp_path):
    p = Path(__file__).parents[1] / 'fixtures'
    contract = json.loads((p / 'billing_contract.json').read_text(encoding='utf-8'))
    assert contract['issued_invoice']['tenant_total_cents'] == {'A': 6500, 'B': 3500}
    db = tmp_path / 'round.db'
    initialize(db, p / 'billing_config.json')
    assert ingest(p / 'billing_readings.csv', db)['imported'] == 6
    evaluate(db)
    con = connect(db)
    usage = {r['slot']: r['usage'] for r in con.execute('SELECT slot,usage,status FROM decision WHERE status=?', ('可结算',))}
    assert usage == {'MAIN': 100.0, 'TEN-A': 60.0, 'TEN-B': 30.0}
    con.close()

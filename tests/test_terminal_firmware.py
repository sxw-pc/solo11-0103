from pathlib import Path

import pytest

from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.terminal_firmware import invoke, register, state


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def prepared(tmp_path):
    db = tmp_path / 'meter.db'
    initialize(db, FIX / 'terminal_config.json')
    assert ingest(FIX / 'terminal_readings.csv', db)['imported'] == 6
    assert set(evaluate(db).values()) == {'可结算'}
    assert register(db, 'upgrade-1', FIX / 'terminal_firmware_case.json')['devices'] == 3
    return db


def test_firmware_endpoint_failure_and_recovery_primitives(tmp_path):
    db = prepared(tmp_path)
    assert invoke(db, 'upgrade-1', 'H1', 'install', '2.0', 'op-1', 0)['status'] == 'applied'
    assert invoke(db, 'upgrade-1', 'H2', 'install', '2.0', 'op-2', 0)['status'] == 'ack_lost'
    assert state(db, 'H2')['version'] == '2.0'
    assert invoke(db, 'upgrade-1', 'H2', 'install', '2.0', 'op-2', 0)['status'] == 'replayed'
    assert invoke(db, 'upgrade-1', 'H3', 'install', '2.0', 'op-3', 0)['status'] == 'rejected'
    assert invoke(db, 'upgrade-1', 'H1', 'rollback', '1.0', 'op-4', 1)['status'] == 'applied'
    assert [state(db, x)['version'] for x in ('H1', 'H2', 'H3')] == ['1.0', '2.0', '1.0']


def test_firmware_requires_real_reading_source(tmp_path):
    db = tmp_path / 'meter.db'
    initialize(db, FIX / 'terminal_config.json')
    with pytest.raises(ValueError, match='firmware_reading_source_missing'):
        register(db, 'upgrade-1', FIX / 'terminal_firmware_case.json')

from pathlib import Path

import pytest

from meterround.__main__ import initialize
from meterround.demand_response import dispatch, register, state
from meterround.ingest import ingest
from meterround.rules import evaluate


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def prepared(tmp_path, name):
    db = tmp_path / name
    initialize(db, FIX / 'dr_config.json')
    assert ingest(FIX / 'dr_readings.csv', db)['imported'] == 4
    assert set(evaluate(db).values()) == {'可结算'}
    assert register(db, 'dr-1', FIX / 'dr_case.json')['zones'] == 2
    return db


def test_two_real_control_outcomes(tmp_path):
    cheap = prepared(tmp_path, 'cheap.db')
    first = dispatch(cheap, 'dr-1', 0, ['A'], 'cheap-0')
    second = dispatch(cheap, 'dr-1', 1, [], 'cheap-1')
    assert first['grid_kw'] == 2 and first['shed_kw'] == 1
    assert first['energy_cents'] + second['energy_cents'] == 200
    assert first['zones']['A']['exposure'] + second['zones']['A']['exposure'] == 2
    assert first['zones']['B']['exposure'] + second['zones']['B']['exposure'] == 4
    assert [x['temperature'] for x in state(cheap, 'dr-1')] == [24, 24]
    assert dispatch(cheap, 'dr-1', 0, ['A'], 'cheap-0')['status'] == 'replayed'

    cooler = prepared(tmp_path, 'cooler.db')
    first = dispatch(cooler, 'dr-1', 0, ['A'], 'cool-0')
    second = dispatch(cooler, 'dr-1', 1, ['B'], 'cool-1')
    assert first['energy_cents'] + second['energy_cents'] == 1200
    assert first['zones']['A']['exposure'] + second['zones']['A']['exposure'] == 2
    assert first['zones']['B']['exposure'] + second['zones']['B']['exposure'] == 2
    assert [x['temperature'] for x in state(cooler, 'dr-1')] == [24, 22]


def test_guardrails(tmp_path):
    db = prepared(tmp_path, 'guard.db')
    with pytest.raises(ValueError, match='grid_cap_exceeded'):
        dispatch(db, 'dr-1', 0, ['A', 'B'], 'too-much')
    with pytest.raises(ValueError, match='period_sequence_conflict'):
        dispatch(db, 'dr-1', 1, [], 'out-of-order')
    assert state(db, 'dr-1')[0]['period'] == 0


def test_source_is_required(tmp_path):
    db = tmp_path / 'empty.db'
    initialize(db, FIX / 'dr_config.json')
    with pytest.raises(ValueError, match='demand_response_meter_source_missing'):
        register(db, 'dr-1', FIX / 'dr_case.json')

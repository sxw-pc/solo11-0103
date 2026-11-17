from pathlib import Path

import pytest

from meterround.__main__ import initialize
from meterround.battery_waste import load, process, register, ship, state
from meterround.ingest import ingest
from meterround.rules import evaluate


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def prepared(tmp_path, name):
    db = tmp_path / name
    initialize(db, FIX / 'battery_config.json')
    assert ingest(FIX / 'battery_readings.csv', db)['imported'] == 4
    assert set(evaluate(db).values()) == {'可结算'}
    register(db, 'waste-1', FIX / 'battery_case.json')
    return db


def test_separate_recovery_changes_physical_material(tmp_path):
    db = prepared(tmp_path, 'recover.db')
    load(db, 'waste-1', 'P1', 'C5', 'l1')
    with pytest.raises(ValueError, match='container_capacity_exceeded'):
        load(db, 'waste-1', 'P2', 'C5', 'too-heavy')
    load(db, 'waste-1', 'P2', 'C9', 'l2')
    assert ship(db, 'waste-1', 'C5', 's1')['mass_kg'] == 4
    assert ship(db, 'waste-1', 'C9', 's2')['mass_kg'] == 5
    a = process(db, 'waste-1', 'C5', 'recover', 'p1')
    b = process(db, 'waste-1', 'C9', 'recover', 'p2')
    assert a['recovered_kg'] + b['recovered_kg'] == 7
    assert a['neutralized_kg'] + b['neutralized_kg'] == 2
    assert {p['disposition'] for p in state(db, 'waste-1')['packs']} == {'recover'}


def test_consolidated_thermal_changes_physical_material(tmp_path):
    db = prepared(tmp_path, 'thermal.db')
    load(db, 'waste-1', 'P1', 'C9', 'l1')
    load(db, 'waste-1', 'P2', 'C9', 'l2')
    assert ship(db, 'waste-1', 'C9', 's1')['mass_kg'] == 9
    with pytest.raises(ValueError, match='mixed_chemistry_cannot_recover'):
        process(db, 'waste-1', 'C9', 'recover', 'p1')
    result = process(db, 'waste-1', 'C9', 'thermal', 'p2')
    assert result['recovered_kg'] == 0 and result['neutralized_kg'] == 9
    assert process(db, 'waste-1', 'C9', 'thermal', 'p2')['status'] == 'replayed'


def test_requires_device_source(tmp_path):
    db = tmp_path / 'missing.db'
    initialize(db, FIX / 'battery_config.json')
    with pytest.raises(ValueError, match='battery_device_source_missing'):
        register(db, 'waste-1', FIX / 'battery_case.json')

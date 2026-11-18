import hashlib
import json
from pathlib import Path

from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
CASE = json.loads((FIX / 'solar_attribute_contract.json').read_text(encoding='utf-8'))


def prepare(tmp_path, config=None, readings=None):
    db = tmp_path / 'attrs.db'
    initialize(db, config or FIX / 'solar_config.json')
    ingest(readings or FIX / 'solar_readings.csv', db)
    evaluate(db)
    return db


def contract(overrides=None):
    spec = dict(CASE)
    spec['contract_file'] = str((FIX / CASE['contract_file']).resolve())
    spec['site'] = 'SOLAR-STATION'
    spec.update(overrides or {})
    return spec


def period2_contract(overrides=None):
    spec = contract({'batch_id': 'solar-period-2', 'generation_slot': 'PV-GEN-2',
                     'self_use_slot': 'PV-SELF-2', 'export_slot': 'PV-EXPORT-2'})
    spec.update(overrides or {})
    return spec


def write_config(tmp_path, spec, name='contract.json'):
    path = tmp_path / name
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding='utf-8')
    return path


def rewrite_readings(tmp_path, replacements):
    text = (FIX / 'solar_readings.csv').read_text(encoding='utf-8')
    for old, new in replacements:
        text = text.replace(old, new)
    path = tmp_path / 'readings.csv'
    path.write_text(text, encoding='utf-8')
    return path


def two_period_config(tmp_path):
    spec = json.loads((FIX / 'solar_config.json').read_text(encoding='utf-8'))
    spec['batch'].append({'id': 'solar-period-2', 'start': '2026-02-01T00:00:00+00:00',
                          'end': '2026-03-01T00:00:00+00:00', 'window_hours': 24,
                          'spike_limit': 10000})
    spec['slot'].extend([
        {'id': 'PV-GEN-2', 'batch_id': 'solar-period-2'},
        {'id': 'PV-SELF-2', 'batch_id': 'solar-period-2'},
        {'id': 'PV-EXPORT-2', 'batch_id': 'solar-period-2'},
    ])
    for slot, meter in [('PV-GEN-2', 'M-PV-GEN-2'), ('PV-SELF-2', 'M-PV-SELF-2'),
                        ('PV-EXPORT-2', 'M-PV-EXPORT-2')]:
        spec['assignment'].append({'slot': slot, 'meter': meter,
                                   'start': '2025-01-01T00:00:00+00:00',
                                   'end': '2027-01-01T00:00:00+00:00', 'capacity': 10000})
        spec['multiplier'].append({'slot': slot, 'start': '2025-01-01T00:00:00+00:00',
                                   'end': '2027-01-01T00:00:00+00:00', 'value': 1})
    path = tmp_path / 'solar_config2.json'
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding='utf-8')
    return path


def period2_readings(tmp_path, end_values=(130, 125, 105)):
    csv = ('device,seq,slot,meter,at,value\n'
           'H-PV-GEN-2,1,PV-GEN-2,M-PV-GEN-2,2026-02-01T00:00:00+00:00,100\n'
           'H-PV-GEN-2,2,PV-GEN-2,M-PV-GEN-2,2026-03-01T00:00:00+00:00,%s\n'
           'H-PV-SELF-2,1,PV-SELF-2,M-PV-SELF-2,2026-02-01T00:00:00+00:00,100\n'
           'H-PV-SELF-2,2,PV-SELF-2,M-PV-SELF-2,2026-03-01T00:00:00+00:00,%s\n'
           'H-PV-EXPORT-2,1,PV-EXPORT-2,M-PV-EXPORT-2,2026-02-01T00:00:00+00:00,100\n'
           'H-PV-EXPORT-2,2,PV-EXPORT-2,M-PV-EXPORT-2,2026-03-01T00:00:00+00:00,%s\n'
           % end_values)
    path = tmp_path / 'readings2.csv'
    path.write_text(csv, encoding='utf-8')
    return path


def snapshot(db):
    con = connect(db)
    result = {}
    for table in ('source', 'reading', 'evidence', 'decision'):
        result[table] = [dict(r) for r in con.execute('SELECT * FROM %s ORDER BY 1' % table)]
    con.close()
    return result


def test_contract_snapshot_matches_frozen_fixture():
    assert hashlib.sha256((FIX / CASE['contract_file']).read_bytes()).hexdigest() == \
        CASE['contract_sha256']

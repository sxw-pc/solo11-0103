import json
from pathlib import Path
from meterround.__main__ import demo,initialize
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect

FIX=Path(__file__).resolve().parent.parent/'fixtures'
def setup(tmp_path):
    db=tmp_path/'test.db';initialize(db,FIX/'config.json');return db
def test_demo(tmp_path):
    p=demo(tmp_path);j=json.loads(p.read_text(encoding='utf-8'))
    assert j['decision'][0]['status']=='可结算' and j['decision'][0]['usage']==100
    assert p.with_suffix('.md').exists()
def test_duplicate_file(tmp_path):
    db=setup(tmp_path);assert ingest(FIX/'readings.csv',db)['imported']==2
    assert ingest(FIX/'readings.csv',db)['duplicate_file']
def test_missing_read(tmp_path):
    db=setup(tmp_path);assert evaluate(db)['A-01']=='待核'
def test_multiplier_gap(tmp_path):
    db=setup(tmp_path);ingest(FIX/'readings.csv',db)
    con=connect(db);con.execute('DELETE FROM multiplier');con.commit();con.close()
    assert evaluate(db)['A-01']=='待核'
def test_rollover_ambiguous(tmp_path):
    db=setup(tmp_path);src=tmp_path/'bad.csv';src.write_text((FIX/'readings.csv').read_text().replace(',150',',50'),encoding='utf-8');ingest(src,db)
    assert evaluate(db)['A-01']=='待核'
def test_identity_conflict(tmp_path):
    db=setup(tmp_path);ingest(FIX/'readings.csv',db);src=tmp_path/'conflict.csv';src.write_text((FIX/'readings.csv').read_text().replace(',150',',151'),encoding='utf-8');ingest(src,db)
    con=connect(db);assert con.execute("SELECT count(*) FROM issue WHERE code='identity_conflict'").fetchone()[0]==1
def test_outside_window(tmp_path):
    db=setup(tmp_path);src=tmp_path/'late.csv';src.write_text((FIX/'readings.csv').read_text().replace('2026-02-01','2026-03-01'),encoding='utf-8');ingest(src,db)
    assert evaluate(db)['A-01']=='待核'
def test_spike(tmp_path):
    db=setup(tmp_path);ingest(FIX/'readings.csv',db);con=connect(db);con.execute('UPDATE batch SET spike_limit=20');con.commit();con.close()
    assert evaluate(db)['A-01']=='待核'
def test_replacement(tmp_path):
    db=setup(tmp_path);con=connect(db)
    con.execute("UPDATE assignment SET end='2026-01-15T00:00:00+00:00'")
    con.execute("INSERT INTO assignment VALUES ('A-01','M2','2026-01-15T00:00:00+00:00','2027-01-01T00:00:00+00:00',10000)")
    con.execute("INSERT INTO replacement VALUES ('A-01','M1','M2',120,5,'2026-01-15T00:00:00+00:00')")
    con.commit();con.close()
    src=tmp_path/'swap.csv';src.write_text((FIX/'readings.csv').read_text().replace('H1,2,A-01,M1,2026-02-01T00:00:00+00:00,150','H1,2,A-01,M2,2026-02-01T00:00:00+00:00,35'),encoding='utf-8')
    ingest(src,db);assert evaluate(db)['A-01']=='可结算'
    con=connect(db);assert con.execute('SELECT usage FROM decision').fetchone()[0]==100
def test_rollover(tmp_path):
    db=setup(tmp_path);con=connect(db);con.execute('UPDATE assignment SET capacity=100');con.commit();con.close()
    src=tmp_path/'roll.csv';src.write_text((FIX/'readings.csv').read_text().replace(',100\n',',95\n').replace(',150\n',',5\n'),encoding='utf-8')
    ingest(src,db);assert evaluate(db)['A-01']=='可结算'
    con=connect(db);assert con.execute('SELECT usage FROM decision').fetchone()[0]==20
def test_device_gap_blocks_settlement(tmp_path):
    db=setup(tmp_path);src=tmp_path/'gap.csv';src.write_text((FIX/'readings.csv').read_text().replace('H1,2,','H1,3,'),encoding='utf-8');ingest(src,db)
    assert evaluate(db)['A-01']=='待核'
def test_midperiod_readings_not_settled(tmp_path):
    db=setup(tmp_path);src=tmp_path/'mid.csv';src.write_text((FIX/'readings.csv').read_text().replace('2026-01-01','2026-01-10').replace('2026-02-01','2026-01-20'),encoding='utf-8');ingest(src,db)
    assert evaluate(db)['A-01']=='待核'

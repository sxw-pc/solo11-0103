import json
from pathlib import Path
from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect
FIX=Path(__file__).resolve().parent.parent/'fixtures'
def setup(tmp):
    db=tmp/'x.db';initialize(db,FIX/'config.json');return db
def test_assignment_overlap_blocks_settlement(tmp_path):
    db=setup(tmp_path);ingest(FIX/'readings.csv',db);c=connect(db)
    c.execute("INSERT INTO assignment VALUES ('A-01','M1','2026-01-01T00:00:00+00:00','2027-01-01T00:00:00+00:00',10000)");c.commit();c.close()
    assert evaluate(db)['A-01']=='待核'
def test_replacement_without_manifest_blocks_settlement(tmp_path):
    db=setup(tmp_path);src=tmp_path/'x.csv';src.write_text((FIX/'readings.csv').read_text().replace('H1,2,A-01,M1','H1,2,A-01,M2'),encoding='utf-8');ingest(src,db)
    assert evaluate(db)['A-01']=='待核'
def test_invalid_row_is_isolated(tmp_path):
    db=setup(tmp_path);src=tmp_path/'x.csv';src.write_text((FIX/'readings.csv').read_text()+'H1,3,A-01,M1,invalid,5\n',encoding='utf-8');result=ingest(src,db)
    assert result['imported']==2
    c=connect(db);assert c.execute("SELECT count(*) FROM issue WHERE code='parse_error'").fetchone()[0]==1
def test_zero_usage_requires_review(tmp_path):
    db=setup(tmp_path);src=tmp_path/'x.csv';src.write_text((FIX/'readings.csv').read_text().replace(',150\n',',100\n'),encoding='utf-8');ingest(src,db)
    assert evaluate(db)['A-01']=='待核'
def test_report_references_evidence_ids(tmp_path):
    from meterround.__main__ import demo
    data=json.loads(demo(tmp_path).read_text(encoding='utf-8'));detail=json.loads(data['decision'][0]['detail'])
    assert detail['segments'][0]['evidence']==[1,2]

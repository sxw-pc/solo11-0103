import json
from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect

T0='2026-01-01T00:00:00+00:00';T1='2026-01-02T00:00:00+00:00';T2='2026-01-03T00:00:00+00:00'
# 同一表计区间：末读 95、起读 5、容量 100、期间两段倍率 1 与 2
PROOF_ROWS=[('H1',1,'A-01','M1',T0,95),('H1',2,'A-01','M1',T2,5)]
TWO_MULT=[(T0,T1,1),(T1,T2,2)]
REPL_ROWS=[('H1',1,'A-01','M1',T0,100),('H1',2,'A-01','M2',T2,35)]
REPL_ASSIGNMENTS=[{'slot':'A-01','meter':'M1','start':'2025-01-01T00:00:00+00:00','end':T1,'capacity':100},
                  {'slot':'A-01','meter':'M2','start':T1,'end':'2027-01-01T00:00:00+00:00','capacity':100}]

def make_config(tmp_path,rule='capacity',spike=100.0,capacity=100.0,multipliers=TWO_MULT,
                start=T0,end=T2,window=24,replacements=(),assignments='default'):
    conf={'batch':[{'id':'period-1','start':start,'end':end,'window_hours':window,'spike_limit':spike,'rollover_rule':rule}],
          'slot':[{'id':'A-01','batch_id':'period-1'}],
          'assignment':[{'slot':'A-01','meter':'M1','start':'2025-01-01T00:00:00+00:00','end':'2027-01-01T00:00:00+00:00','capacity':capacity}] if assignments=='default' else list(assignments),
          'multiplier':[{'slot':'A-01','start':a,'end':b,'value':v} for a,b,v in multipliers],
          'replacement':[{'slot':r[0],'old_meter':r[1],'new_meter':r[2],'old_final':r[3],'new_initial':r[4],'at':r[5]} for r in replacements]}
    p=tmp_path/'config.json';p.write_text(json.dumps(conf),encoding='utf-8');return p

def make_csv(tmp_path,rows):
    p=tmp_path/'readings.csv'
    p.write_text('device,seq,slot,meter,at,value\n'+''.join(','.join(map(str,r))+'\n' for r in rows),encoding='utf-8')
    return p

def build(tmp_path,rows=None,**kw):
    db=tmp_path/'test.db';initialize(db,make_config(tmp_path,**kw))
    if rows is not None:ingest(make_csv(tmp_path,rows),db)
    return db

def decision(db):
    con=connect(db);row=con.execute('SELECT status,usage,detail FROM decision').fetchone();con.close()
    return row['status'],row['usage'],json.loads(row['detail'])

def issues(db,code):
    con=connect(db);n=con.execute('SELECT count(*) FROM issue WHERE code=?',(code,)).fetchone()[0];con.close()
    return n

# 唯一候选：spike_limit=100 恰使 1 次回卷(15)可行、2 次(165)不可行，容量约束规则锁定 k=1 结算
def test_capacity_rule_unique_candidate_settles(tmp_path):
    db=build(tmp_path,rows=PROOF_ROWS,rule='capacity',spike=100)
    assert evaluate(db)['A-01']=='可结算'
    status,usage,detail=decision(db)
    assert usage==15
    iv,=detail['intervals']
    assert iv['settled_k']==1
    assert {c['k']:c['feasible'] for c in iv['candidates']}=={0:False,1:True,2:False}
    k2=next(c for c in iv['candidates'] if c['k']==2)
    assert 'spike_limit' in k2['reasons']
    assert [s['multiplier'] for s in detail['segments']]==[1,2]
    assert [s['usage'] for s in detail['segments']]==[5,10]
    # 每个候选的每个分段都保留读数证据、倍率来源与回卷差值，不只写进总量
    for c in iv['candidates']:
        for s in c['segments']:
            assert s['evidence']==[1,2]
            assert s['multiplier_source']=={'start':s['from'],'end':s['to']} or s['multiplier_source']
            assert s['raw_delta']==c['raw_usage']

# 同一区间同一数据，单回卷规则因回卷跨越多个倍率周期而待核
def test_single_rule_defers_when_rollover_spans_multiplier_periods(tmp_path):
    db=build(tmp_path,rows=PROOF_ROWS,rule='single',spike=100)
    assert evaluate(db)['A-01']=='待核'
    status,usage,detail=decision(db)
    assert usage is None
    iv,=detail['intervals']
    assert iv['settled_k'] is None
    k1=next(c for c in iv['candidates'] if c['k']==1)
    assert 'crosses_multiplier_period' in k1['reasons']
    assert issues(db,'rollover_no_candidate')==1

# 放宽 spike_limit 使 1 次与 2 次都可行，容量约束规则因多候选转待核
def test_capacity_rule_defers_when_spike_allows_two_counts(tmp_path):
    db=build(tmp_path,rows=PROOF_ROWS,rule='capacity',spike=200)
    assert evaluate(db)['A-01']=='待核'
    status,usage,detail=decision(db)
    iv,=detail['intervals']
    feasible=[c for c in iv['candidates'] if c['feasible']]
    assert [c['k'] for c in feasible]==[1,2]
    assert {c['k']:c['usage'] for c in feasible}=={1:15,2:165}
    assert issues(db,'rollover_ambiguous')==1

# 倍率边界：倍率切换恰好落在读数时刻不算跨界，单回卷规则可结算
def test_multiplier_boundary_exactly_at_reading_is_not_a_crossing(tmp_path):
    rows=[('H1',1,'A-01','M1',T0,95),('H1',2,'A-01','M1',T1,5)]
    db=build(tmp_path,rows=rows,rule='single',spike=1000,start=T0,end=T1,
             multipliers=[('2025-12-31T00:00:00+00:00',T0,1),(T0,T2,2)])
    assert evaluate(db)['A-01']=='可结算'
    status,usage,detail=decision(db)
    assert usage==20
    iv,=detail['intervals']
    assert iv['settled_k']==1 and len(detail['segments'])==1

# 容量边界：末读恰好等于量程，按一次回卷结算
def test_reading_exactly_at_capacity_rolls_over(tmp_path):
    rows=[('H1',1,'A-01','M1',T0,100),('H1',2,'A-01','M1',T1,5)]
    db=build(tmp_path,rows=rows,rule='single',spike=1000,start=T0,end=T1,
             multipliers=[('2025-01-01T00:00:00+00:00','2027-01-01T00:00:00+00:00',2)])
    assert evaluate(db)['A-01']=='可结算'
    status,usage,detail=decision(db)
    assert usage==10
    iv,=detail['intervals']
    assert iv['candidates'][1]['raw_usage']==5

# 量程未知：没有覆盖区间的表计绑定，待核
def test_missing_assignment_means_unknown_capacity(tmp_path):
    db=build(tmp_path,rows=PROOF_ROWS,rule='capacity',spike=1000,assignments=())
    assert evaluate(db)['A-01']=='待核'
    assert issues(db,'assignment_conflict')>0

# 量程未知：绑定的容量非正数，待核
def test_nonpositive_capacity_is_unknown(tmp_path):
    db=build(tmp_path,rows=PROOF_ROWS,rule='capacity',spike=1000,capacity=0)
    assert evaluate(db)['A-01']=='待核'
    assert issues(db,'capacity_unknown')==1

# 换表证据闭合：旧表末读与新表初读进入算式，分段保留读数与倍率来源
def test_replacement_closed_settles_and_segments_keep_sources(tmp_path):
    db=build(tmp_path,rows=REPL_ROWS,rule='capacity',spike=100,
             replacements=[('A-01','M1','M2',120,5,T1)],assignments=REPL_ASSIGNMENTS)
    assert evaluate(db)['A-01']=='可结算'
    status,usage,detail=decision(db)
    assert usage==75
    iv,=detail['intervals']
    cand,=iv['candidates']
    assert cand['source']['replacement']['old_final']==120
    assert cand['source']['replacement']['new_initial']==5
    assert iv['settled_k']==0
    for s in detail['segments']:
        assert s['evidence']==[1,2] and s['multiplier_source']

# 换表证据不闭合：旧表末读低于换表前最后一次读数，待核
def test_replacement_unclosed_old_final_defers(tmp_path):
    db=build(tmp_path,rows=REPL_ROWS,rule='capacity',spike=1000,
             replacements=[('A-01','M1','M2',80,5,T1)],assignments=REPL_ASSIGNMENTS)
    assert evaluate(db)['A-01']=='待核'
    assert issues(db,'replacement_gap')==1

# 换表证据不闭合：新表读数低于新表初读，待核
def test_replacement_unclosed_new_initial_defers(tmp_path):
    db=build(tmp_path,rows=REPL_ROWS,rule='capacity',spike=1000,
             replacements=[('A-01','M1','M2',120,40,T1)],assignments=REPL_ASSIGNMENTS)
    assert evaluate(db)['A-01']=='待核'
    assert issues(db,'replacement_gap')==1

# 换表证据不闭合：其他表计对的换表记录不能闭合本区间，待核
def test_replacement_wrong_meter_pair_does_not_close(tmp_path):
    db=build(tmp_path,rows=REPL_ROWS,rule='capacity',spike=1000,
             replacements=[('A-01','M1','M3',120,5,T1)],assignments=REPL_ASSIGNMENTS)
    assert evaluate(db)['A-01']=='待核'
    assert issues(db,'replacement_gap')==1

# 区间隔离：前一段正常结算，后一段回卷跨界待核，候选互不外溢
def test_intervals_are_solved_and_reported_independently(tmp_path):
    rows=[('H1',1,'A-01','M1',T0,10),('H1',2,'A-01','M1',T1,20),('H1',3,'A-01','M1',T2,15)]
    mid='2026-01-02T12:00:00+00:00'
    db=build(tmp_path,rows=rows,rule='single',spike=100,start=T0,end=T2,
             multipliers=[('2025-12-31T00:00:00+00:00',mid,1),(mid,'2026-01-04T00:00:00+00:00',1)])
    assert evaluate(db)['A-01']=='待核'
    status,usage,detail=decision(db)
    assert usage==10
    iv1,iv2=detail['intervals']
    assert iv1['settled_k']==0 and iv2['settled_k'] is None
    assert iv1['evidence']==[1,2] and iv2['evidence']==[2,3]
    k1=next(c for c in iv2['candidates'] if c['k']==1)
    assert 'crosses_multiplier_period' in k1['reasons']
    assert len(detail['segments'])==1 and detail['segments'][0]['evidence']==[1,2]
    assert issues(db,'rollover_no_candidate')==1

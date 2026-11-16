import json
from collections import defaultdict
from datetime import timedelta
from .ingest import at
from .store import connect

VERSION='2'
RULES=('single','capacity')
MAX_CANDIDATES=64

def span(row,t): return at(row['start'])<=t<at(row['end'])

def _segments(multipliers,start,end):
    cuts={start,end}
    cuts.update(at(m['start']) for m in multipliers if start<at(m['start'])<end)
    cuts.update(at(m['end']) for m in multipliers if start<at(m['end'])<end)
    cuts=sorted(cuts)
    segs=[];complete=True
    for a,b in zip(cuts,cuts[1:]):
        m=[x for x in multipliers if span(x,a) and span(x,b-timedelta(microseconds=1))]
        if len(m)!=1:complete=False
        else:segs.append((a,b,m[0]))
    return segs,complete

def _candidates(d0,capacity,segs,start,end,spike,rule,evidence,source,allow_rollover=True):
    length=(end-start).total_seconds();out=[];feasible=0;k=0
    while k<MAX_CANDIDATES:
        u=d0+k*capacity;reasons=[]
        if u<0:reasons.append('negative_usage')
        parts=[]
        if u>=0:
            for a,b,m in segs:
                part=u*((b-a).total_seconds()/length)*m['value']
                parts.append({'from':a.isoformat(),'to':b.isoformat(),'raw_delta':u,'multiplier':m['value'],'multiplier_source':{'start':m['start'],'end':m['end']},'usage':part,'evidence':list(evidence)})
        usage=sum(p['usage'] for p in parts) if u>=0 else None
        if u>=0 and usage>spike:reasons.append('spike_limit')
        if rule=='single' and k==1 and len(segs)>1:reasons.append('crosses_multiplier_period')
        out.append({'k':k,'raw_usage':u,'usage':usage,'feasible':not reasons,'reasons':reasons,'source':source,'segments':parts})
        if not reasons:feasible+=1
        if feasible>=2:break
        if u>=0 and usage>spike:break
        if not allow_rollover or (rule=='single' and k>=1):break
        k+=1
    return out

def evaluate(db_path):
    db=connect(db_path);out={}
    with db:
        run_id=db.execute("INSERT INTO run(status,started_at) VALUES ('待接收',CURRENT_TIMESTAMP)").lastrowid
        db.execute("UPDATE run SET status='已校验' WHERE id=?",(run_id,))
        db.execute('DELETE FROM decision')
        db.execute("DELETE FROM issue WHERE code IN ('outside_window','assignment_conflict','missing_read','missing_boundary','reverse','rollover_ambiguous','rollover_no_candidate','replacement_gap','multiplier_gap','capacity_unknown','zero_usage','spike','sequence_gap')")
        readings=[dict(r) for r in db.execute('SELECT * FROM reading')]
        by_device=defaultdict(list);gap_slots=set()
        for r in readings:by_device[r['device']].append(r)
        for d,events in by_device.items():
            seq={r['seq'] for r in events}
            if max(seq)-min(seq)+1!=len(seq):
                gap_slots.update(r['slot'] for r in events)
                db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',('sequence_gap',d,'device sequence gap'))
        for slot in db.execute('SELECT * FROM slot'):
            subject=slot['id'];batch=db.execute('SELECT * FROM batch WHERE id=?',(slot['batch_id'],)).fetchone()
            rule=batch['rollover_rule'] if 'rollover_rule' in batch.keys() else 'single'
            if rule not in RULES:rule='single'
            rr=sorted((r for r in readings if r['slot']==subject),key=lambda r:at(r['at']))
            assignments=[dict(r) for r in db.execute('SELECT * FROM assignment WHERE slot=?',(subject,))]
            multipliers=[dict(r) for r in db.execute('SELECT * FROM multiplier WHERE slot=?',(subject,))]
            replacements=[dict(r) for r in db.execute('SELECT * FROM replacement WHERE slot=?',(subject,))]
            problems=[];segments=[];intervals=[];total=0.0
            if subject in gap_slots:problems.append(('sequence_gap',None))
            for r in rr:
                t=at(r['at']);matches=[a for a in assignments if a['meter']==r['meter'] and span(a,t)]
                if len(matches)!=1:problems.append(('assignment_conflict',r['id']))
                start=at(batch['start']);end=at(batch['end']);margin=timedelta(hours=batch['window_hours'])
                if not start-margin<=t<=end+margin:problems.append(('outside_window',r['id']))
            if len(rr)<2:problems.append(('missing_read',None))
            else:
                start=at(batch['start']);end=at(batch['end']);margin=timedelta(hours=batch['window_hours'])
                if abs(at(rr[0]['at'])-start)>margin or abs(at(rr[-1]['at'])-end)>margin:
                    problems.append(('missing_boundary',None))
            for prev,curr in zip(rr,rr[1:]):
                start,end=at(prev['at']),at(curr['at'])
                if end<=start:problems.append(('reverse',curr['id']));continue
                evidence=[prev['id'],curr['id']]
                meter=prev['meter'] if prev['meter']==curr['meter'] else prev['meter']+'->'+curr['meter']
                iv={'from':start.isoformat(),'to':end.isoformat(),'meter':meter,'rule':rule,'evidence':evidence,'candidates':[],'settled_k':None}
                intervals.append(iv)
                if prev['meter']==curr['meter']:
                    matches=[a for a in assignments if a['meter']==curr['meter'] and span(a,start) and span(a,end-timedelta(microseconds=1))]
                    if len(matches)!=1:iv['blocked']='assignment_conflict';problems.append(('assignment_conflict',curr['id']));continue
                    cap=matches[0]['capacity']
                    if cap is None or cap<=0:iv['blocked']='capacity_unknown';problems.append(('capacity_unknown',curr['id']));continue
                    segs,complete=_segments(multipliers,start,end)
                    if not complete:iv['blocked']='multiplier_gap';problems.append(('multiplier_gap',curr['id']));continue
                    source={'capacity':cap,'assignment':{'meter':matches[0]['meter'],'start':matches[0]['start'],'end':matches[0]['end']}}
                    cands=_candidates(curr['value']-prev['value'],cap,segs,start,end,batch['spike_limit'],rule,evidence,source)
                else:
                    change=[r for r in replacements if r['old_meter']==prev['meter'] and r['new_meter']==curr['meter'] and start<=at(r['at'])<=end]
                    if len(change)!=1:iv['blocked']='replacement_gap';problems.append(('replacement_gap',curr['id']));continue
                    c=change[0]
                    if c['old_final']<prev['value'] or curr['value']<c['new_initial']:
                        iv['blocked']='replacement_gap';problems.append(('replacement_gap',curr['id']));continue
                    segs,complete=_segments(multipliers,start,end)
                    if not complete:iv['blocked']='multiplier_gap';problems.append(('multiplier_gap',curr['id']));continue
                    source={'replacement':{'old_meter':c['old_meter'],'new_meter':c['new_meter'],'old_final':c['old_final'],'new_initial':c['new_initial'],'at':c['at']}}
                    d0=(c['old_final']-prev['value'])+(curr['value']-c['new_initial'])
                    cands=_candidates(d0,0,segs,start,end,batch['spike_limit'],rule,evidence,source,allow_rollover=False)
                iv['candidates']=cands
                feasible=[c for c in cands if c['feasible']]
                if len(feasible)==1:
                    chosen=feasible[0];iv['settled_k']=chosen['k'];total+=chosen['usage'];segments.extend(chosen['segments'])
                elif len(feasible)==0:problems.append(('rollover_no_candidate',curr['id']))
                else:problems.append(('rollover_ambiguous',curr['id']))
            if rr and total==0:problems.append(('zero_usage',rr[-1]['id']))
            if total>batch['spike_limit']:problems.append(('spike',rr[-1]['id'] if rr else None))
            for code,evidence_id in sorted(set(problems)):
                db.execute('INSERT INTO issue(code,subject,detail,evidence_id) VALUES (?,?,?,?)',(code,subject,code,evidence_id))
            status='可结算' if not problems else '待核'
            detail=json.dumps({'rule':rule,'segments':segments,'intervals':intervals,'issues':[x[0] for x in problems]},ensure_ascii=False)
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(subject,status,total if segments else None,detail,VERSION));out[subject]=status
        db.execute("UPDATE run SET status=CASE WHEN EXISTS(SELECT 1 FROM decision WHERE status<>'可结算') THEN '待核' ELSE '已判定' END,ended_at=CURRENT_TIMESTAMP WHERE id=?",(run_id,))
    db.close();return out

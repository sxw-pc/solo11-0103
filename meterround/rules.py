import json
from collections import defaultdict
from datetime import timedelta
from .ingest import at
from .store import connect

VERSION='1'
def span(row,t): return at(row['start'])<=t<at(row['end'])
def evaluate(db_path):
    db=connect(db_path);out={}
    with db:
        run_id=db.execute("INSERT INTO run(status,started_at) VALUES ('待接收',CURRENT_TIMESTAMP)").lastrowid
        db.execute("UPDATE run SET status='已校验' WHERE id=?",(run_id,))
        db.execute('DELETE FROM decision')
        db.execute("DELETE FROM issue WHERE code IN ('outside_window','assignment_conflict','missing_read','reverse','rollover_ambiguous','replacement_gap','multiplier_gap','zero_usage','spike','sequence_gap')")
        readings=[dict(r) for r in db.execute('SELECT * FROM reading')]
        by_device=defaultdict(list)
        gap_slots=set()
        for r in readings:by_device[r['device']].append(r)
        for d,events in by_device.items():
            seq={r['seq'] for r in events}
            if max(seq)-min(seq)+1!=len(seq):
                gap_slots.update(r['slot'] for r in events)
                db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',('sequence_gap',d,'device sequence gap'))
        for slot in db.execute('SELECT * FROM slot'):
            subject=slot['id'];batch=db.execute('SELECT * FROM batch WHERE id=?',(slot['batch_id'],)).fetchone(); rr=sorted((r for r in readings if r['slot']==subject),key=lambda r:at(r['at']))
            assignments=[dict(r) for r in db.execute('SELECT * FROM assignment WHERE slot=?',(subject,))]
            multipliers=[dict(r) for r in db.execute('SELECT * FROM multiplier WHERE slot=?',(subject,))]
            replacements=[dict(r) for r in db.execute('SELECT * FROM replacement WHERE slot=?',(subject,))]
            problems=[];segments=[]; total=0.0
            if subject in gap_slots:problems.append(('sequence_gap',None))
            for r in rr:
                t=at(r['at']); matches=[a for a in assignments if a['meter']==r['meter'] and span(a,t)]
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
                if prev['meter']==curr['meter']:
                    matches=[a for a in assignments if a['meter']==curr['meter'] and span(a,start) and span(a,end-timedelta(microseconds=1))]
                    if len(matches)!=1:problems.append(('assignment_conflict',curr['id']));continue
                    delta=curr['value']-prev['value'];cap=matches[0]['capacity']
                    if delta<0:
                        if prev['value']>=cap*0.9 and curr['value']<=cap*0.1:delta=cap-prev['value']+curr['value']
                        else:problems.append(('rollover_ambiguous',curr['id']));continue
                else:
                    change=[r for r in replacements if r['old_meter']==prev['meter'] and r['new_meter']==curr['meter'] and start<=at(r['at'])<=end]
                    if len(change)!=1:problems.append(('replacement_gap',curr['id']));continue
                    c=change[0];delta=(c['old_final']-prev['value'])+(curr['value']-c['new_initial'])
                    if delta<0:problems.append(('replacement_gap',curr['id']));continue
                cuts={start,end}
                cuts.update(at(m['start']) for m in multipliers if start<at(m['start'])<end)
                cuts.update(at(m['end']) for m in multipliers if start<at(m['end'])<end)
                cuts=sorted(cuts)
                for a,b in zip(cuts,cuts[1:]):
                    m=[x for x in multipliers if span(x,a) and span(x,b-timedelta(microseconds=1))]
                    if len(m)!=1:problems.append(('multiplier_gap',curr['id']));continue
                    part=delta*(b-a)/(end-start)*m[0]['value'];total+=part
                    segments.append({'from':a.isoformat(),'to':b.isoformat(),'raw_delta':delta,'multiplier':m[0]['value'],'usage':part,'evidence':[prev['id'],curr['id']]})
            if rr and total==0:problems.append(('zero_usage',rr[-1]['id']))
            if total>batch['spike_limit']:problems.append(('spike',rr[-1]['id'] if rr else None))
            for code,evidence in sorted(set(problems)):
                db.execute('INSERT INTO issue(code,subject,detail,evidence_id) VALUES (?,?,?,?)',(code,subject,code,evidence))
            status='可结算' if not problems else '待核'
            detail=json.dumps({'segments':segments,'issues':[x[0] for x in problems]},ensure_ascii=False)
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(subject,status,total if segments else None,detail,VERSION));out[subject]=status
        db.execute("UPDATE run SET status=CASE WHEN EXISTS(SELECT 1 FROM decision WHERE status<>'可结算') THEN '待核' ELSE '已判定' END,ended_at=CURRENT_TIMESTAMP WHERE id=?",(run_id,))
    db.close();return out

import argparse
import json
import tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from . import demand_response as dr

def initialize(db_path,config):
    conf=json.loads(Path(config).read_text(encoding='utf-8'))
    db=connect(db_path)
    with db:
        for table,columns in [('batch',['id','start','end','window_hours','spike_limit']),('slot',['id','batch_id']),('assignment',['slot','meter','start','end','capacity']),('multiplier',['slot','start','end','value']),('replacement',['slot','old_meter','new_meter','old_final','new_initial','at'])]:
            for item in conf.get(table,[]):
                marks=','.join('?' for _ in columns)
                db.execute(f'INSERT OR IGNORE INTO {table}({",".join(columns)}) VALUES ({marks})',[item[k] for k in columns])
    db.close()

def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='meterround-'));root.mkdir(parents=True,exist_ok=True)
    fixtures=Path(__file__).resolve().parent.parent/'fixtures'; db=root/'meterround.db'
    initialize(db,fixtures/'config.json');ingest(fixtures/'readings.csv',db);evaluate(db)
    return report(db,root/'report.json')

def main():
    p=argparse.ArgumentParser(description='离线抄表结算证据台');s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    i=s.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    a=s.add_parser('ingest');a.add_argument('file');a.add_argument('--db',required=True)
    e=s.add_parser('evaluate');e.add_argument('--db',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    drr=s.add_parser('dr-register');drr.add_argument('--db',required=True);drr.add_argument('--case',required=True);drr.add_argument('--case-id',required=True)
    drp=s.add_parser('dr-plan');drp.add_argument('--db',required=True);drp.add_argument('--case-id',required=True);drp.add_argument('--contract',required=True,choices=list(dr.CONTRACTS));drp.add_argument('--plan-id');drp.add_argument('--out')
    dre=s.add_parser('dr-execute');dre.add_argument('--db',required=True);dre.add_argument('--plan-id',required=True);dre.add_argument('--period',required=True,type=int);dre.add_argument('--action-id')
    drf=s.add_parser('dr-feedback');drf.add_argument('--db',required=True);drf.add_argument('--case-id',required=True);drf.add_argument('--period',required=True,type=int);drf.add_argument('--zone',required=True);drf.add_argument('--temperature',required=True,type=float);drf.add_argument('--source-sha256',required=True);drf.add_argument('--at',required=True)
    drs=s.add_parser('dr-state');drs.add_argument('--db',required=True);drs.add_argument('--case-id',required=True)
    args=p.parse_args()
    if args.command=='demo':print(demo(args.workdir))
    elif args.command=='init':initialize(args.db,args.config)
    elif args.command=='ingest':print(ingest(args.file,args.db))
    elif args.command=='evaluate':print(evaluate(args.db))
    elif args.command=='report':print(report(args.db,args.out))
    elif args.command=='inspect':
        db=connect(args.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
    elif args.command=='dr-register':
        print(json.dumps(dr.register(args.db,args.case_id,args.case),ensure_ascii=False))
    elif args.command=='dr-plan':
        try:
            out=dr.plan(args.db,args.case_id,args.contract,plan_id=args.plan_id)
        except dr.InfeasiblePlan:
            out={'status':dr.PLAN_HOLD,'reason':'dr_no_feasible_plan',
                 'plan_id':args.plan_id}
        if args.out and out.get('plan_id'):
            out['evidence']=str(dr.write_evidence(args.db,out['plan_id'],args.out))
        print(json.dumps(out,ensure_ascii=False))
    elif args.command=='dr-execute':
        print(json.dumps(dr.execute(args.db,args.plan_id,args.period,args.action_id),ensure_ascii=False))
    elif args.command=='dr-feedback':
        print(json.dumps(dr.feedback(args.db,args.case_id,args.period,args.zone,args.temperature,args.source_sha256,args.at),ensure_ascii=False))
    elif args.command=='dr-state':
        print(json.dumps(dr.state(args.db,args.case_id),ensure_ascii=False))
if __name__=='__main__':main()

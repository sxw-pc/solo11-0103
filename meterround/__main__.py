import argparse
import json
import tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report

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

def battery_register(db,case,fixture):
    from .battery_waste import register
    return register(db,case,fixture)

def battery_plan(db,case,contract):
    from .battery_waste import plan_campaign
    return plan_campaign(db,case,contract)

def battery_run(db,case,contract,campaign=None,stop_after=None,out=None,plan_id=None):
    from .battery_waste import CampaignInterrupted,execute_campaign,campaign_state
    campaign_id=campaign or f'campaign:{case}:{contract}'
    try:
        execute_campaign(db,case,contract,campaign_id=campaign_id,
                         stop_after=stop_after,plan_id=plan_id)
        kind='result'
    except CampaignInterrupted:
        kind='interrupted'
    state=campaign_state(db,case,campaign_id)
    if out:
        from .battery_report import build_report
        report=build_report(db,case,contract=contract,campaign_id=campaign_id,out=out)
        summary={'report_path':str(out),'campaign_id':campaign_id,
                 'status':state['campaign']['status'],'contract':contract,
                 'totals':report['totals']}
        return 'report',summary
    result=state['result'] or {}
    result=dict(result)
    result['campaign']={'campaign_id':campaign_id,
                        'status':state['campaign']['status'],
                        'reason':state['campaign']['reason'],
                        'plan_id':state['campaign']['plan_id'],
                        'plans':state['plans']}
    result['steps']=state['steps']
    return kind,result

def main():
    p=argparse.ArgumentParser(description='离线抄表结算证据台')
    s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    i=s.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    a=s.add_parser('ingest');a.add_argument('file');a.add_argument('--db',required=True)
    e=s.add_parser('evaluate');e.add_argument('--db',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    from .battery_waste import CONTRACTS
    b=s.add_parser('battery-register');b.add_argument('--db',required=True);b.add_argument('--case',required=True);b.add_argument('--fixture',required=True)
    pl=s.add_parser('battery-plan');pl.add_argument('--db',required=True);pl.add_argument('--case',required=True);pl.add_argument('--contract',choices=CONTRACTS,required=True)
    br=s.add_parser('battery-run');br.add_argument('--db',required=True);br.add_argument('--case',required=True);br.add_argument('--contract',choices=CONTRACTS,required=True)
    br.add_argument('--campaign');br.add_argument('--stop-after');br.add_argument('--out')
    br.add_argument('--plan-id')
    bt=s.add_parser('battery-state');bt.add_argument('--db',required=True);bt.add_argument('--case',required=True);bt.add_argument('--campaign',required=True)
    args=p.parse_args()
    if args.command=='demo':print(demo(args.workdir))
    elif args.command=='init':initialize(args.db,args.config)
    elif args.command=='ingest':print(ingest(args.file,args.db))
    elif args.command=='evaluate':print(evaluate(args.db))
    elif args.command=='report':print(report(args.db,args.out))
    elif args.command=='inspect':
        db=connect(args.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
    elif args.command=='battery-register':
        print(json.dumps(battery_register(args.db,args.case,args.fixture),ensure_ascii=False,sort_keys=True))
    elif args.command=='battery-plan':
        print(json.dumps(battery_plan(args.db,args.case,args.contract),ensure_ascii=False,sort_keys=True))
    elif args.command=='battery-run':
        kind,result=battery_run(args.db,args.case,args.contract,args.campaign,
                                args.stop_after,args.out,args.plan_id)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    elif args.command=='battery-state':
        from .battery_waste import campaign_state
        print(json.dumps(campaign_state(args.db,args.case,args.campaign),ensure_ascii=False,sort_keys=True))
if __name__=='__main__':main()

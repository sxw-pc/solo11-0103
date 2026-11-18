import argparse
import json
import tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from . import custody

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
    def custody_parser(name,**extra):
        c=s.add_parser(name);c.add_argument('--db',required=True);c.add_argument('--handoff',required=True)
        c.add_argument('--fixtures-dir',required=True);c.add_argument('--request-id',required=True)
        c.add_argument('--actor');c.add_argument('--at')
        for flag,kw in extra.items():c.add_argument(flag,**kw)
        return c
    custody_parser('retire')
    custody_parser('seal')
    custody_parser('handoff-issue',**{'--contract':{'required':True}})
    sign=custody_parser('handoff-sign')
    sign.add_argument('--order-id',required=True)
    sign.add_argument('--direction',required=True,choices=list(custody.DIRECTIONS))
    sign.add_argument('--seal-id');sign.add_argument('--seal-sha256')
    v=s.add_parser('custody-view');v.add_argument('--db',required=True);v.add_argument('--old-meter',required=True)
    args=p.parse_args()
    if args.command=='demo':print(demo(args.workdir))
    elif args.command=='init':initialize(args.db,args.config)
    elif args.command=='ingest':print(ingest(args.file,args.db))
    elif args.command=='evaluate':print(evaluate(args.db))
    elif args.command=='report':print(report(args.db,args.out))
    elif args.command=='inspect':
        db=connect(args.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
    elif args.command in ('retire','seal','handoff-issue','handoff-sign'):
        common=dict(db_path=args.db,handoff_config=args.handoff,fixtures_dir=args.fixtures_dir,
                    request_id=args.request_id,actor=args.actor or custody.WAREHOUSE,occurred_at=args.at)
        try:
            if args.command=='retire':result=custody.retire_meter(**common)
            elif args.command=='seal':result=custody.seal_meter(**common)
            elif args.command=='handoff-issue':result=custody.issue_handoff(db_path=args.db,handoff_config=args.handoff,fixtures_dir=args.fixtures_dir,contract=args.contract,request_id=args.request_id,actor=args.actor or custody.WAREHOUSE,occurred_at=args.at)
            else:
                result=custody.submit_credential(db_path=args.db,handoff_config=args.handoff,fixtures_dir=args.fixtures_dir,order_id=args.order_id,direction=args.direction,request_id=args.request_id,actor=args.actor,occurred_at=args.at,seal_id=args.seal_id,seal_sha256=args.seal_sha256)
        except custody.CustodyError as exc:
            print(json.dumps({'ok':False,'reason':exc.reason,'detail':exc.detail},ensure_ascii=False));raise SystemExit(1)
        print(json.dumps(result,ensure_ascii=False))
    elif args.command=='custody-view':
        print(json.dumps(custody.custody_view(args.db,args.old_meter),ensure_ascii=False,indent=2))
if __name__=='__main__':main()

import argparse
import json
import tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from .terminal_firmware import (
    register as firmware_register,
    rollout as firmware_rollout,
    state as firmware_state,
    firmware_report,
)

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
    fr=s.add_parser('firmware-register');fr.add_argument('--db',required=True);fr.add_argument('--case',required=True);fr.add_argument('--fixture',required=True)
    fu=s.add_parser('firmware-rollout');fu.add_argument('--db',required=True);fu.add_argument('--case',required=True);fu.add_argument('--contract',choices=['fleet_atomic','cell_isolated'],required=True)
    fm=s.add_parser('firmware-resume');fm.add_argument('--db',required=True);fm.add_argument('--round',required=True)
    fs=s.add_parser('firmware-state');fs.add_argument('--db',required=True);fs.add_argument('--device',required=True)
    fp=s.add_parser('firmware-report');fp.add_argument('--db',required=True);fp.add_argument('--round',required=True);fp.add_argument('--out',required=True)
    args=p.parse_args()
    if args.command=='demo':print(demo(args.workdir))
    elif args.command=='init':initialize(args.db,args.config)
    elif args.command=='ingest':print(ingest(args.file,args.db))
    elif args.command=='evaluate':print(evaluate(args.db))
    elif args.command=='report':print(report(args.db,args.out))
    elif args.command=='inspect':
        db=connect(args.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
    elif args.command=='firmware-register':print(json.dumps(firmware_register(args.db,args.case,args.fixture),ensure_ascii=False))
    elif args.command=='firmware-rollout':print(json.dumps(firmware_rollout(args.db,case_id=args.case,contract=args.contract),ensure_ascii=False))
    elif args.command=='firmware-resume':print(json.dumps(firmware_rollout(args.db,round_id=args.round),ensure_ascii=False))
    elif args.command=='firmware-state':print(json.dumps(firmware_state(args.db,args.device),ensure_ascii=False))
    elif args.command=='firmware-report':print(firmware_report(args.db,args.round,args.out))
if __name__=='__main__':main()

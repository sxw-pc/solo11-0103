import argparse
import json
import tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from .attributes import (TITLE_MODES, issue, reissue, revoke, transfer,
                         complete_recall, ledger, summary)

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

def _run(fn,*args):
    from .attributes import LedgerError
    try:print(json.dumps(fn(*args),ensure_ascii=False,indent=2))
    except LedgerError as exc:
        print(json.dumps({'error':exc.code,'detail':exc.detail},ensure_ascii=False,indent=2))
        raise SystemExit(1)

def main():
    p=argparse.ArgumentParser(description='离线抄表结算证据台');s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    i=s.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    a=s.add_parser('ingest');a.add_argument('file');a.add_argument('--db',required=True)
    e=s.add_parser('evaluate');e.add_argument('--db',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    at=s.add_parser('attribute-issue',help='按冻结的合同/证据快照发行绿色属性凭证')
    at.add_argument('--db',required=True);at.add_argument('--config',required=True)
    at.add_argument('--title',required=True,choices=TITLE_MODES)
    ar=s.add_parser('attribute-reissue',help='证据修订后撤销旧发行并在同一事务重发')
    ar.add_argument('--db',required=True);ar.add_argument('--config',required=True)
    ar.add_argument('--title',required=True,choices=TITLE_MODES)
    ar.add_argument('--reason',default='evidence_revised')
    av=s.add_parser('attribute-revoke',help='撤销有效发行，已转让凭证转召回任务')
    av.add_argument('--db',required=True);av.add_argument('--config',required=True)
    av.add_argument('--reason',default='revoked')
    af=s.add_parser('attribute-transfer',help='转让一张有效凭证')
    af.add_argument('--db',required=True);af.add_argument('--certificate',required=True)
    af.add_argument('--to',required=True)
    ac=s.add_parser('attribute-recall-done',help='登记召回完成')
    ac.add_argument('--db',required=True);ac.add_argument('--certificate',required=True)
    al=s.add_parser('attribute-ledger',help='输出属性发行/凭证/召回/结转台账')
    al.add_argument('--db',required=True);al.add_argument('--batch')
    al.add_argument('--summary',action='store_true')
    args=p.parse_args()
    if args.command=='demo':print(demo(args.workdir))
    elif args.command=='init':initialize(args.db,args.config)
    elif args.command=='ingest':print(ingest(args.file,args.db))
    elif args.command=='evaluate':print(evaluate(args.db))
    elif args.command=='report':print(report(args.db,args.out))
    elif args.command=='inspect':
        db=connect(args.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
    elif args.command=='attribute-issue':
        _run(issue,args.config,args.title,args.db)
    elif args.command=='attribute-reissue':
        _run(reissue,args.config,args.title,args.db,args.reason)
    elif args.command=='attribute-revoke':
        _run(revoke,args.config,args.db,args.reason)
    elif args.command=='attribute-transfer':
        _run(transfer,args.certificate,args.to,args.db)
    elif args.command=='attribute-recall-done':
        _run(complete_recall,args.certificate,args.db)
    elif args.command=='attribute-ledger':
        _run(summary if args.summary else ledger,args.db,args.batch)
if __name__=='__main__':main()

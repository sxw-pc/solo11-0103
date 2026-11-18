import argparse
import json
import tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report,publish_disclosures,recover_disclosures,export_external_bundle

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
    pub=s.add_parser('publish-disclosures',help='对外发布 H2 覆盖汇总（内部报告不对外）')
    pub.add_argument('--db',required=True);pub.add_argument('--requests',required=True);pub.add_argument('--out',required=True)
    pub.add_argument('--policy',choices=['deny_unsafe','minimum_safe_coarsening'],default='minimum_safe_coarsening')
    pub.add_argument('--dry-run',action='store_true')
    rec=s.add_parser('recover-disclosures',help='重放中断的发布并核验交付字节')
    rec.add_argument('--db',required=True);rec.add_argument('--out')
    exp=s.add_parser('export-external',help='按台账重放单个已发布文件')
    exp.add_argument('--db',required=True);exp.add_argument('--publication-id',required=True);exp.add_argument('--out',required=True)
    exp.add_argument('--audience')
    args=p.parse_args()
    if args.command=='demo':print(demo(args.workdir))
    elif args.command=='init':initialize(args.db,args.config)
    elif args.command=='ingest':print(ingest(args.file,args.db))
    elif args.command=='evaluate':print(evaluate(args.db))
    elif args.command=='report':print(report(args.db,args.out))
    elif args.command=='inspect':
        db=connect(args.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
    elif args.command=='publish-disclosures':print(json.dumps(publish_disclosures(args.db,args.requests,args.out,args.policy,args.dry_run),ensure_ascii=False))
    elif args.command=='recover-disclosures':print(json.dumps(recover_disclosures(args.db,args.out),ensure_ascii=False))
    elif args.command=='export-external':print(json.dumps(export_external_bundle(args.db,args.out,args.publication_id,args.audience),ensure_ascii=False))
if __name__=='__main__':main()

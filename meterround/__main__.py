import argparse
import json
import tempfile
from pathlib import Path
from .store import connect, save_contract, statement
from .ingest import ingest
from .rules import evaluate
from .report import report
from . import billing

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

def bill(db_path,contracts,out=None):
    db=connect(db_path)
    if isinstance(contracts,(str,Path)):contracts=[contracts]
    contract_ids=[]
    for path in contracts:
        path=Path(path);raw=path.read_bytes()
        document=json.loads(raw.decode('utf-8-sig'))
        contract_ids.append(save_contract(db,document,raw))
    results={}
    for contract_id in contract_ids:
        results[contract_id]=billing.bill_all(db,contract_id)
    data={'contracts':[{'contract_id':cid,'bills':{m:billing.summarize(r) for m,r in methods.items()},
                        'statement':statement(db,cid)} for cid,methods in results.items()]}
    if out:
        target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(data,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')
    db.close()
    return data

def main():
    p=argparse.ArgumentParser(description='离线抄表结算证据台');s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    i=s.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    a=s.add_parser('ingest');a.add_argument('file');a.add_argument('--db',required=True)
    e=s.add_parser('evaluate');e.add_argument('--db',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    b=s.add_parser('bill');b.add_argument('--db',required=True);b.add_argument('--contract',required=True,action='append');b.add_argument('--out')
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    args=p.parse_args()
    if args.command=='demo':print(demo(args.workdir))
    elif args.command=='init':initialize(args.db,args.config)
    elif args.command=='ingest':print(ingest(args.file,args.db))
    elif args.command=='evaluate':print(evaluate(args.db))
    elif args.command=='report':print(report(args.db,args.out))
    elif args.command=='bill':print(json.dumps(bill(args.db,args.contract,args.out),ensure_ascii=False,indent=2))
    elif args.command=='inspect':
        db=connect(args.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
if __name__=='__main__':main()

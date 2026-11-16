import json
from pathlib import Path
from .store import connect
def report(db_path,out):
    db=connect(db_path)
    data={table:[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in ('batch','slot','assignment','multiplier','replacement','reading','evidence','issue','decision','run')}
    data['source']=[dict(r) for r in db.execute('SELECT id,name,sha256,length(raw) AS byte_length FROM source ORDER BY id')]
    path=Path(out);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 抄表结算依据','',*['- '+r['slot']+'：'+r['status']+'；用量 '+str(r['usage']) for r in data['decision']],'','问题：']
    lines += ['- '+r['code']+' '+r['subject'] for r in data['issue']]
    path.with_suffix('.md').write_text('\n'.join(lines),encoding='utf-8')
    db.close();return path

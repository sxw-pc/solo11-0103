import csv
import hashlib
import io
from datetime import datetime
from pathlib import Path
from .store import connect

def at(value):
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None: raise ValueError('timezone_required')
    return dt

def ingest(csv_path,db_path):
    raw=Path(csv_path).read_bytes();digest=hashlib.sha256(raw).hexdigest()
    db=connect(db_path)
    if db.execute('SELECT 1 FROM source WHERE sha256=?',(digest,)).fetchone():
        db.close();return {'imported':0,'duplicate_file':True}
    rows=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    count=0
    with db:
        sid=db.execute('INSERT INTO source(name,sha256,raw) VALUES (?,?,?)',(str(csv_path),digest,raw)).lastrowid
        for line,row in enumerate(rows,2):
            try:
                device=row['device'].strip();seq=int(row['seq']);slot=row['slot'].strip();meter=row['meter'].strip();date=row['at'].strip();value=float(row['value'])
                at(date)
                if not device or not slot or not meter or value<0:raise ValueError('invalid_reading')
                rawline=','.join(row.get(k,'') for k in ('device','seq','slot','meter','at','value'))
                h=hashlib.sha256(rawline.encode()).hexdigest()
                old=db.execute('SELECT sha256 FROM reading WHERE device=? AND seq=?',(device,seq)).fetchone()
                if old:
                    if old['sha256']!=h:db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',('identity_conflict',slot,f'{device}:{seq}'))
                    continue
                if not db.execute('SELECT 1 FROM slot WHERE id=?',(slot,)).fetchone():
                    db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',('unknown_slot',slot,f'line {line}'))
                    continue
                eid=db.execute('INSERT INTO reading(source_id,line_no,device,seq,slot,meter,at,value,raw,sha256) VALUES (?,?,?,?,?,?,?,?,?,?)',(sid,line,device,seq,slot,meter,date,value,rawline,h)).lastrowid;db.execute('INSERT INTO evidence(reading_id,source_sha256,payload_sha256,role) VALUES (?,?,?,?)',(eid,digest,h,'reading'));count+=1
            except (KeyError,ValueError,TypeError) as e:
                db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',('parse_error',str(line),str(e)))
    db.close();return {'imported':count,'duplicate_file':False}

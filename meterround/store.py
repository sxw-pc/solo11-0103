import sqlite3
from pathlib import Path

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS batch(id TEXT PRIMARY KEY,start TEXT NOT NULL,end TEXT NOT NULL,window_hours INTEGER NOT NULL,spike_limit REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,name TEXT NOT NULL,sha256 TEXT NOT NULL UNIQUE,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS slot(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES batch(id));
CREATE TABLE IF NOT EXISTS assignment(slot TEXT NOT NULL REFERENCES slot(id),meter TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,capacity REAL NOT NULL,PRIMARY KEY(slot,meter,start));
CREATE TABLE IF NOT EXISTS multiplier(slot TEXT NOT NULL REFERENCES slot(id),start TEXT NOT NULL,end TEXT NOT NULL,value REAL NOT NULL,PRIMARY KEY(slot,start));
CREATE TABLE IF NOT EXISTS replacement(slot TEXT NOT NULL REFERENCES slot(id),old_meter TEXT NOT NULL,new_meter TEXT NOT NULL,old_final REAL NOT NULL,new_initial REAL NOT NULL,at TEXT NOT NULL,PRIMARY KEY(slot,at));
CREATE TABLE IF NOT EXISTS reading(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES source(id),line_no INTEGER NOT NULL,device TEXT NOT NULL,seq INTEGER NOT NULL,slot TEXT NOT NULL,meter TEXT NOT NULL,at TEXT NOT NULL,value REAL NOT NULL,raw TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(device,seq));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,code TEXT NOT NULL,subject TEXT NOT NULL,detail TEXT NOT NULL,evidence_id INTEGER REFERENCES reading(id));
CREATE TABLE IF NOT EXISTS decision(slot TEXT PRIMARY KEY REFERENCES slot(id),status TEXT NOT NULL,usage REAL,detail TEXT NOT NULL,rule_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, reading_id INTEGER NOT NULL REFERENCES reading(id), source_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL, role TEXT NOT NULL, UNIQUE(reading_id,role));
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')), started_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE IF NOT EXISTS retired_meter(id INTEGER PRIMARY KEY,slot TEXT NOT NULL,old_meter TEXT NOT NULL UNIQUE,new_meter TEXT NOT NULL,replacement_at TEXT NOT NULL,seal_id TEXT NOT NULL UNIQUE,seal_sha256 TEXT NOT NULL,seal_file TEXT NOT NULL,created_at TEXT NOT NULL,created_by TEXT NOT NULL,source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS custody_event(id INTEGER PRIMARY KEY,retired_id INTEGER NOT NULL REFERENCES retired_meter(id),seq INTEGER NOT NULL,action TEXT NOT NULL,state_from TEXT,state_to TEXT NOT NULL,actor TEXT NOT NULL,occurred_at TEXT NOT NULL,source TEXT NOT NULL,request_id TEXT NOT NULL UNIQUE,detail TEXT NOT NULL,UNIQUE(retired_id,seq));
CREATE UNIQUE INDEX IF NOT EXISTS idx_custody_signoff_once ON custody_event(retired_id) WHERE action='signoff';
CREATE TABLE IF NOT EXISTS handoff_order(id TEXT PRIMARY KEY,retired_id INTEGER NOT NULL REFERENCES retired_meter(id),contract TEXT NOT NULL,consignee TEXT NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL,seal_id TEXT NOT NULL,seal_sha256 TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('待交接','已签收','已作废')),issued_at TEXT NOT NULL,issued_by TEXT NOT NULL,source TEXT NOT NULL,UNIQUE(retired_id,contract));
CREATE TABLE IF NOT EXISTS handoff_credential(id INTEGER PRIMARY KEY,order_id TEXT NOT NULL REFERENCES handoff_order(id),retired_id INTEGER NOT NULL REFERENCES retired_meter(id),direction TEXT NOT NULL CHECK(direction IN ('dispatch','receipt')),actor TEXT NOT NULL,seal_id TEXT NOT NULL,seal_sha256 TEXT NOT NULL,arrived_at TEXT NOT NULL,request_id TEXT NOT NULL UNIQUE,UNIQUE(order_id,direction));
CREATE TABLE IF NOT EXISTS custody_conflict(id INTEGER PRIMARY KEY,retired_id INTEGER NOT NULL REFERENCES retired_meter(id),order_id TEXT,request_id TEXT NOT NULL,actor TEXT NOT NULL,contract TEXT,reason TEXT NOT NULL,occurred_at TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS custody_request(request_id TEXT PRIMARY KEY,retired_id INTEGER,operation TEXT NOT NULL,ok INTEGER NOT NULL,result TEXT NOT NULL,recorded_at TEXT NOT NULL);
"""
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    return db

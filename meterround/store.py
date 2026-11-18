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
CREATE TABLE IF NOT EXISTS terminal_firmware_device(id TEXT PRIMARY KEY,cell TEXT NOT NULL,version TEXT NOT NULL,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS terminal_firmware_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS terminal_firmware_operation(id TEXT PRIMARY KEY,device TEXT NOT NULL,command TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS terminal_firmware_round(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES terminal_firmware_case(id),contract TEXT NOT NULL CHECK(contract IN ('fleet_atomic','cell_isolated')),status TEXT NOT NULL,created_at TEXT NOT NULL,ended_at TEXT,detail TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS terminal_firmware_step(id INTEGER PRIMARY KEY,round_id TEXT NOT NULL REFERENCES terminal_firmware_round(id),device TEXT NOT NULL,seq INTEGER NOT NULL,phase TEXT NOT NULL CHECK(phase IN ('upgrade','rollback')),cell TEXT NOT NULL,command TEXT NOT NULL CHECK(command IN ('install','rollback')),version TEXT NOT NULL,operation_id TEXT NOT NULL UNIQUE,expected_revision INTEGER NOT NULL,status TEXT NOT NULL,response TEXT,verified TEXT,UNIQUE(round_id,device,phase));
CREATE TABLE IF NOT EXISTS terminal_firmware_journal(id INTEGER PRIMARY KEY,round_id TEXT NOT NULL,device TEXT,event TEXT NOT NULL,detail TEXT NOT NULL,at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    return db

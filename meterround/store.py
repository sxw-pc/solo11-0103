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

-- 外部披露台账。每一受众独立保存已发布的表位集合、H2 覆盖数和文件摘要。
-- content 是唯一的交付字节来源；pending 行在文件原子替换前已经提交，
-- 因此中断后可以据此重放，不会出现“文件已交付但没有台账”的窗口。
CREATE TABLE IF NOT EXISTS disclosure_publication(
    id INTEGER PRIMARY KEY,
    audience TEXT NOT NULL,
    request_id TEXT NOT NULL,
    publication_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    version TEXT NOT NULL,
    slots TEXT NOT NULL,
    h2_count INTEGER NOT NULL CHECK(h2_count>=0),
    file_path TEXT NOT NULL DEFAULT '',
    file_sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('legacy','pending','published')),
    created_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE(audience, request_id),
    -- 发布 ID 只在受众内唯一：不同受众的历史发布可以用同一个 ID，
    -- 两边的集合/数值/摘要必须各自独立落账。
    UNIQUE(audience, publication_id)
);
CREATE INDEX IF NOT EXISTS idx_disclosure_audience ON disclosure_publication(audience,status);

-- 拒绝只进入内部审计台账，绝不参与外部受众可见的联立等式。
CREATE TABLE IF NOT EXISTS disclosure_refusal(
    id INTEGER PRIMARY KEY,
    audience TEXT NOT NULL,
    request_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    requested_slots TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(audience, request_id, policy)
);
"""

def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30.0);db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    db.executescript(SCHEMA)
    return db

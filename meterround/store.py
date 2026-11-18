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

-- 绿色属性合同字节快照。raw 只写一次，作为权属基础，不随报告改写。
CREATE TABLE IF NOT EXISTS attribute_contract(
 id INTEGER PRIMARY KEY,
 sha256 TEXT NOT NULL,
 raw BLOB NOT NULL,
 contract_file TEXT NOT NULL,
 config_sha256 TEXT NOT NULL,
 title_mode TEXT NOT NULL,
 holder TEXT NOT NULL,
 unit_per_certificate REAL NOT NULL,
 UNIQUE(sha256,title_mode,holder,unit_per_certificate)
);
-- 一次属性发行所冻结的结算证据摘要。同一 fingerprint 只落一次。
CREATE TABLE IF NOT EXISTS attribute_basis(
 id INTEGER PRIMARY KEY,
 fingerprint TEXT NOT NULL UNIQUE,
 contract_id INTEGER NOT NULL REFERENCES attribute_contract(id),
 batch_id TEXT NOT NULL REFERENCES batch(id),
 site TEXT NOT NULL,
 generation_slot TEXT NOT NULL,
 self_use_slot TEXT NOT NULL,
 export_slot TEXT NOT NULL,
 period_start TEXT NOT NULL,
 period_end TEXT NOT NULL,
 generation_usage REAL,
 self_use_usage REAL,
 export_usage REAL,
 generation_status TEXT NOT NULL,
 self_use_status TEXT NOT NULL,
 export_status TEXT NOT NULL,
 rule_versions TEXT NOT NULL,
 decision_digest TEXT NOT NULL,
 reading_digest TEXT NOT NULL,
 evidence_digest TEXT NOT NULL,
 source_digest TEXT NOT NULL,
 evidence_fingerprint TEXT NOT NULL,
 unit_per_certificate REAL NOT NULL,
 eligible_units REAL NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attribute_basis_batch ON attribute_basis(batch_id,site);
-- 一次权属发行：一个批次只允许一条有效记录，权属模式与持有人不可并存。
CREATE TABLE IF NOT EXISTS attribute_issuance(
 id INTEGER PRIMARY KEY,
 basis_id INTEGER NOT NULL REFERENCES attribute_basis(id),
 batch_id TEXT NOT NULL REFERENCES batch(id),
 site TEXT NOT NULL,
 contract_id INTEGER NOT NULL REFERENCES attribute_contract(id),
 title_mode TEXT NOT NULL,
 holder TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('有效','待生效','已撤销','待召回')),
 certificate_count INTEGER NOT NULL,
 eligible_units REAL NOT NULL,
 issued_units REAL NOT NULL,
 period_remainder_units REAL NOT NULL,
 opening_carry_units REAL NOT NULL,
 closing_carry_units REAL NOT NULL,
 carried_from_id INTEGER REFERENCES attribute_issuance(id),
 supersedes_id INTEGER REFERENCES attribute_issuance(id),
 reason TEXT,
 created_at TEXT NOT NULL,
 revoked_at TEXT
);
-- 同一批次只允许一个有效（或待生效）权属发行；并发第二方由部分唯一索引拒绝。
CREATE UNIQUE INDEX IF NOT EXISTS ux_attribute_issuance_active ON attribute_issuance(batch_id) WHERE status IN ('有效','待生效');
CREATE TABLE IF NOT EXISTS attribute_certificate(
 id INTEGER PRIMARY KEY,
 certificate_id TEXT NOT NULL UNIQUE,
 issuance_id INTEGER NOT NULL REFERENCES attribute_issuance(id),
 basis_id INTEGER NOT NULL REFERENCES attribute_basis(id),
 basis_fingerprint TEXT NOT NULL,
 batch_id TEXT NOT NULL,
 site TEXT NOT NULL,
 period_start TEXT NOT NULL,
 period_end TEXT NOT NULL,
 contract_sha256 TEXT NOT NULL,
 title_mode TEXT NOT NULL,
 holder TEXT NOT NULL,
 origin_holder TEXT NOT NULL,
 energy_share REAL NOT NULL,
 serial INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('有效','待生效','已撤销','待召回','已转让','已召回')),
 transferred_to TEXT,
 transferred_at TEXT,
 created_at TEXT NOT NULL,
 UNIQUE(issuance_id,serial)
);
CREATE INDEX IF NOT EXISTS ix_attribute_certificate_batch ON attribute_certificate(batch_id,site,status);
CREATE TABLE IF NOT EXISTS attribute_recall(
 id INTEGER PRIMARY KEY,
 certificate_id INTEGER NOT NULL UNIQUE REFERENCES attribute_certificate(id),
 issuance_id INTEGER NOT NULL REFERENCES attribute_issuance(id),
 batch_id TEXT NOT NULL,
 site TEXT NOT NULL,
 holder TEXT NOT NULL,
 reason TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('待召回','已完成')),
 created_at TEXT NOT NULL,
 completed_at TEXT
);
-- 未满一张凭证的余量按合同/场站/权属链结转，只作可追溯台账，不跨期间凑证。
CREATE TABLE IF NOT EXISTS attribute_carry(
 id INTEGER PRIMARY KEY,
 issuance_id INTEGER NOT NULL UNIQUE REFERENCES attribute_issuance(id),
 basis_id INTEGER NOT NULL REFERENCES attribute_basis(id),
 batch_id TEXT NOT NULL,
 site TEXT NOT NULL,
 contract_sha256 TEXT NOT NULL,
 title_mode TEXT NOT NULL,
 holder TEXT NOT NULL,
 unit_per_certificate REAL NOT NULL,
 eligible_units REAL NOT NULL,
 opening_units REAL NOT NULL,
 period_remainder_units REAL NOT NULL,
 closing_units REAL NOT NULL,
 carried_from_id INTEGER REFERENCES attribute_carry(id),
 closed_by_issuance_id INTEGER REFERENCES attribute_issuance(id),
 status TEXT NOT NULL CHECK(status IN ('可结转','已结转','已作废')),
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attribute_carry_chain ON attribute_carry(site,contract_sha256,title_mode,holder,status);
"""
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30.0);db.row_factory=sqlite3.Row;db.execute('PRAGMA busy_timeout=30000');db.executescript(SCHEMA)
    return db

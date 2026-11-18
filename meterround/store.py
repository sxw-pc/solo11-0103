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
CREATE TABLE IF NOT EXISTS battery_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS battery_pack(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES battery_case(id),chemistry TEXT NOT NULL,mass_kg REAL NOT NULL,metal_kg REAL NOT NULL,fluid_kg REAL NOT NULL,container TEXT,location TEXT NOT NULL,disposition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS battery_container(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES battery_case(id),capacity_kg REAL NOT NULL,location TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS battery_operation(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES battery_case(id),kind TEXT NOT NULL,result TEXT NOT NULL);
"""

# Additive migrations.  SQLite cannot add several columns in one statement, and
# older databases created before the battery campaign existed must keep their
# evidence rows intact.  Every addition is therefore guarded by PRAGMA
# table_info and applied only once.
MIGRATION_COLUMNS = {
    'battery_case': [
        ('payload_sha256', 'TEXT'),
    ],
    'battery_pack': [
        ('device', 'TEXT'),
        ('slot', 'TEXT'),
        ('source_sha256', 'TEXT'),
        ('reading_id', 'INTEGER'),
        ('isolated_reason', 'TEXT'),
    ],
    'battery_container': [
        ('route', 'TEXT'),
        ('shipped_at', 'TEXT'),
        ('processed_at', 'TEXT'),
        ('isolation_reason', 'TEXT'),
        ('last_operation_id', 'TEXT'),
    ],
    'battery_operation': [
        ('campaign_id', 'TEXT'),
        ('intent', 'TEXT'),
        ('status', "TEXT NOT NULL DEFAULT 'done'"),
        ('error', 'TEXT'),
        ('started_at', 'TEXT'),
        ('finished_at', 'TEXT'),
    ],
}

MIGRATION_TABLES = """
CREATE TABLE IF NOT EXISTS battery_pack_source(
    pack_id TEXT PRIMARY KEY REFERENCES battery_pack(id) ON DELETE CASCADE,
    case_id TEXT NOT NULL,
    device TEXT NOT NULL,
    slot TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    first_reading_id INTEGER REFERENCES reading(id),
    first_reading_sha256 TEXT NOT NULL,
    reading_count INTEGER NOT NULL,
    verified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS battery_pack_evidence(
    pack_id TEXT NOT NULL REFERENCES battery_pack(id) ON DELETE CASCADE,
    reading_id INTEGER NOT NULL REFERENCES reading(id),
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    source_sha256 TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    PRIMARY KEY(pack_id,reading_id)
);
CREATE TABLE IF NOT EXISTS battery_plan(
    plan_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES battery_case(id),
    contract TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS battery_plan_box(
    plan_id TEXT NOT NULL REFERENCES battery_plan(plan_id) ON DELETE CASCADE,
    box_no INTEGER NOT NULL,
    container_id TEXT NOT NULL,
    capacity_kg REAL NOT NULL,
    pack_ids TEXT NOT NULL,
    mass_kg REAL NOT NULL,
    metal_kg REAL NOT NULL,
    fluid_kg REAL NOT NULL,
    chemistry TEXT NOT NULL,
    route TEXT NOT NULL,
    PRIMARY KEY(plan_id,box_no)
);
CREATE TABLE IF NOT EXISTS battery_campaign(
    campaign_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES battery_case(id),
    contract TEXT NOT NULL,
    plan_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reason TEXT,
    result TEXT
);
CREATE TABLE IF NOT EXISTS battery_campaign_plan(
    campaign_id TEXT NOT NULL REFERENCES battery_campaign(campaign_id) ON DELETE CASCADE,
    plan_id TEXT NOT NULL REFERENCES battery_plan(plan_id),
    seq INTEGER NOT NULL,
    PRIMARY KEY(campaign_id,plan_id)
);
CREATE TABLE IF NOT EXISTS battery_step(
    operation_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    plan_id TEXT,
    box_no INTEGER,
    kind TEXT NOT NULL,
    container_id TEXT,
    pack_id TEXT,
    route TEXT,
    intent TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS battery_pack_result(
    pack_id TEXT PRIMARY KEY REFERENCES battery_pack(id) ON DELETE CASCADE,
    case_id TEXT NOT NULL,
    campaign_id TEXT,
    container_id TEXT NOT NULL,
    route TEXT NOT NULL,
    recovered_kg REAL NOT NULL,
    neutralized_kg REAL NOT NULL,
    operation_id TEXT NOT NULL,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS battery_container_result(
    container_id TEXT PRIMARY KEY REFERENCES battery_container(id) ON DELETE CASCADE,
    case_id TEXT NOT NULL,
    campaign_id TEXT,
    route TEXT NOT NULL,
    mass_kg REAL NOT NULL,
    recovered_kg REAL NOT NULL,
    neutralized_kg REAL NOT NULL,
    pack_count INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS battery_isolation(
    id INTEGER PRIMARY KEY,
    case_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    subject TEXT NOT NULL,
    reason TEXT NOT NULL,
    operation_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(case_id,scope,subject,reason)
);
CREATE INDEX IF NOT EXISTS battery_pack_container_idx ON battery_pack(container);
CREATE INDEX IF NOT EXISTS battery_pack_case_idx ON battery_pack(case_id);
CREATE INDEX IF NOT EXISTS battery_step_campaign_idx ON battery_step(campaign_id);
CREATE INDEX IF NOT EXISTS battery_operation_case_idx ON battery_operation(case_id);
CREATE INDEX IF NOT EXISTS battery_isolation_case_idx ON battery_isolation(case_id);
"""


def _migrate(db):
    for table, columns in MIGRATION_COLUMNS.items():
        existing = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        for name, declaration in columns:
            if name not in existing:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    db.executescript(MIGRATION_TABLES)


def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    _migrate(db)
    return db

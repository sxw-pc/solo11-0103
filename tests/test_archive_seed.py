import json
from pathlib import Path
import pytest
from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.archive import register
from meterround.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_archive_sources_are_distinct_but_share_raw_records(tmp_path):
    db_path = tmp_path / 'archive.db'
    initialize(db_path, FIX / 'config.json')
    assert ingest(FIX / 'archive-a.csv', db_path)['imported'] == 20
    assert ingest(FIX / 'archive-b.csv', db_path)['imported'] == 1
    assert register(db_path, 'archive-01', FIX / 'archive-a.csv', FIX / 'archive-b.csv') == {
        'case_id': 'archive-01', 'sources': 2, 'shared_records': 20}
    db = connect(db_path)
    payload = json.loads(db.execute('SELECT payload FROM archive_case').fetchone()[0])
    db.close()
    assert payload['sources'][0]['source']['sha256'] != payload['sources'][1]['source']['sha256']
    assert payload['sources'][0]['source']['byte_length'] < payload['sources'][1]['source']['byte_length']


def test_archive_requires_ingested_sources(tmp_path):
    db_path = tmp_path / 'archive.db'
    initialize(db_path, FIX / 'config.json')
    with pytest.raises(ValueError, match='archive_source_not_ingested'):
        register(db_path, 'archive-01', FIX / 'archive-a.csv', FIX / 'archive-b.csv')

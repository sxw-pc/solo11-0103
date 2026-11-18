from pathlib import Path

import pytest

from meterround import billing
from meterround.__main__ import initialize
from meterround.ingest import ingest
from meterround.rules import evaluate
from meterround.store import connect, statement

FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def prepared(tmp_path, contract=None, config=None):
    db = tmp_path / 'billing.db'
    initialize(db, config or FIX / 'billing_config.json')
    ingest(FIX / 'billing_readings.csv', db)
    evaluate(db)
    con = connect(db)
    document = contract or (FIX / 'billing_contract.json').read_text(encoding='utf-8')
    from meterround.store import save_contract
    import json
    contract_id = save_contract(con, json.loads(document))
    con.close()
    return db, contract_id


def lines_of(method_bill):
    return {line['tenant_id']: line for line in method_bill['lines']}


# ---------------------------------------------------------------------------
# Two contracted liability splits change who owes what, not just the label.
# ---------------------------------------------------------------------------

def test_usage_share_true_up_matches_contract(tmp_path):
    db, contract = prepared(tmp_path)
    result = billing.bill(connect(db), contract, 'usage_share')
    assert result['status'] == '已出账' and result['revision'] == 1
    assert result['main_cents'] == 10000 and result['common_pool_cents'] == 1000
    lines = lines_of(result)
    assert lines['A']['amount_cents'] == 6667
    assert lines['B']['amount_cents'] == 3333
    assert result['total_cents'] == 10000
    # INV-001 issued A6500/B3500: A owes 167 more, B is owed 167 back.
    assert lines['A']['true_up_cents'] == 167 and lines['B']['true_up_cents'] == -167
    assert lines['A']['issued_cents'] == 6500 and lines['B']['issued_cents'] == 3500


def test_capacity_share_true_up_matches_contract(tmp_path):
    db, contract = prepared(tmp_path)
    result = billing.bill(connect(db), contract, 'capacity_share')
    assert result['status'] == '已出账'
    lines = lines_of(result)
    assert lines['A']['amount_cents'] == 6250
    assert lines['B']['amount_cents'] == 3750
    assert sum(line['amount_cents'] for line in result['lines']) == result['main_cents'] == 10000
    # A subscribed 1 : B subscribed 3, so A overpaid 250 and B underpaid 250.
    assert lines['A']['true_up_cents'] == -250
    assert lines['B']['true_up_cents'] == 250
    assert lines['A']['direct_cents'] == 6000 and lines['B']['direct_cents'] == 3000


def test_two_methods_are_separate_ledgers(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    billing.bill(con, contract, 'usage_share')
    billing.bill(con, contract, 'capacity_share')
    entries = con.execute('SELECT DISTINCT allocation_method FROM billing_entry').fetchall()
    assert {row[0] for row in entries} == {'usage_share', 'capacity_share'}
    # Each method's net mirrors its own true-up; they are never summed together.
    nets = {s['allocation_method']: s['net_cents'] for s in statement(con, contract)}
    assert nets['usage_share'] == {'A': 167, 'B': -167}
    assert nets['capacity_share'] == {'A': -250, 'B': 250}
    con.close()


def test_payer_is_a_real_liability_change(tmp_path):
    """The two contracts do not merely relabel the report: the payer differs."""
    db, contract = prepared(tmp_path)
    con = connect(db)
    usage = lines_of(billing.bill(con, contract, 'usage_share'))
    capacity = lines_of(billing.bill(con, contract, 'capacity_share'))
    assert usage['A']['true_up_cents'] > 0 and usage['B']['true_up_cents'] < 0
    assert capacity['A']['true_up_cents'] < 0 and capacity['B']['true_up_cents'] > 0
    assert capacity['B']['amount_cents'] - usage['B']['amount_cents'] == 417
    con.close()


# ---------------------------------------------------------------------------
# Largest-remainder in integer cents.
# ---------------------------------------------------------------------------

def test_largest_remainder_distributes_the_last_cent_once():
    assert billing.largest_remainder([1, 1, 1], 10) == [4, 3, 3]
    assert billing.largest_remainder([1, 2], 100) == [33, 67]
    assert sum(billing.largest_remainder([5, 3, 2], 7)) == 7


def test_remainder_cent_is_deterministic_and_never_duplicated(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    first = billing.bill(con, contract, 'usage_share')
    second = billing.bill(con, contract, 'usage_share')
    assert first['bill_id'] == second['bill_id']
    assert first['revision'] == second['revision'] == 1
    # Exact split: the pool of 1000 across 60:30 (2:1) is 667/333.
    lines = lines_of(first)
    assert lines['A']['pool_cents'] + lines['B']['pool_cents'] == 1000
    assert lines['A']['pool_cents'] == 667 and lines['B']['pool_cents'] == 333
    con.close()


def test_pool_allocation_sums_to_main_even_with_odd_remainders(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    con.execute('UPDATE billing_contract SET unit_price_cents=101')
    con.commit()
    result = billing.bill(con, contract, 'usage_share')
    assert sum(line['pool_cents'] for line in result['lines']) == result['common_pool_cents']
    assert sum(line['amount_cents'] for line in result['lines']) == result['main_cents']
    con.close()


# ---------------------------------------------------------------------------
# Pending isolation: nothing is partly billed, nothing clamped.
# ---------------------------------------------------------------------------

def test_negative_common_pool_is_pending_not_clamped(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    con.execute("UPDATE decision SET usage=80,status='可结算' WHERE slot='MAIN'")
    con.commit()
    result = billing.bill(con, contract, 'usage_share')
    assert result['status'] == '待核'
    assert result['reasons'] == ['negative_common_pool']
    assert result['lines'] == []
    assert result['main_cents'] is None
    assert con.execute('SELECT count(*) FROM billing_entry').fetchone()[0] == 0
    con.close()


def test_any_pending_slot_blocks_the_whole_bill(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    con.execute("UPDATE decision SET status='待核' WHERE slot='TEN-B'")
    con.commit()
    result = billing.bill(con, contract, 'usage_share')
    assert result['status'] == '待核'
    assert result['reasons'] == ['slot_not_settleable:TEN-B']
    assert result['lines'] == []
    assert con.execute("SELECT count(*) FROM bill_line").fetchone()[0] == 0
    con.close()


def test_pending_main_slot_blocks_whole_bill(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    con.execute("UPDATE decision SET status='待核' WHERE slot='MAIN'")
    con.commit()
    result = billing.bill(con, contract, 'capacity_share')
    assert result['status'] == '待核' and 'MAIN' in result['reasons'][0]
    con.close()


@pytest.mark.parametrize('code,sql', [
    ('invalid_price', 'UPDATE billing_contract SET unit_price_cents=0'),
    ('invalid_price', 'UPDATE billing_contract SET unit_price_cents=-5'),
    ('duplicate_tenant', "INSERT INTO billing_tenant(contract_id,tenant_id,slot,subscribed_capacity) VALUES (?, 'A2', 'TEN-A', 2)"),
    ('period_mismatch', "INSERT INTO slot(id,batch_id) VALUES ('OTHER','period-bill')"),
])
def test_malformed_contract_is_pending(tmp_path, code, sql):
    db, contract = prepared(tmp_path)
    con = connect(db)
    if 'OTHER' in sql:
        con.execute("INSERT INTO batch(id,start,end,window_hours,spike_limit) VALUES ('other-batch','2026-03-01T00:00:00+00:00','2026-04-01T00:00:00+00:00',24,10000)")
        con.execute(sql)
        con.execute("UPDATE billing_tenant SET slot='OTHER' WHERE tenant_id='B'")
        con.execute("UPDATE slot SET batch_id='other-batch' WHERE id='OTHER'")
    else:
        con.execute(sql.replace('?', '?' ), (contract,) if '?' in sql else ())
    con.commit()
    result = billing.bill(con, contract, 'usage_share')
    assert result['status'] == '待核'
    assert any(reason.startswith(code) for reason in result['reasons']), result['reasons']
    con.close()


def test_unknown_allocation_method_is_rejected(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    with pytest.raises(billing.BillingError) as error:
        billing.bill(con, contract, 'area_share')
    assert error.value.code == 'unknown_allocation_method'
    con.close()


# ---------------------------------------------------------------------------
# Idempotency, revision and reversal: never charge a period twice.
# ---------------------------------------------------------------------------

def test_same_snapshot_rerun_adds_no_entries(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    billing.bill(con, contract, 'usage_share')
    before = con.execute('SELECT count(*) FROM billing_entry').fetchone()[0]
    again = billing.bill(con, contract, 'usage_share')
    after = con.execute('SELECT count(*) FROM billing_entry').fetchone()[0]
    assert before == after == 2
    assert again['revision'] == 1
    assert con.execute('SELECT count(*) FROM bill').fetchone()[0] == 1
    con.close()


def test_recorded_reading_existing_revision_becomes_reversal(tmp_path):
    """A later confirmation reading changes the snapshot: new revision, the prior
    adjustment is reversed, and the net is only the new true-up."""
    db, contract = prepared(tmp_path)
    con = connect(db)
    first = billing.bill(con, contract, 'usage_share')
    assert first['revision'] == 1
    # MAIN later confirms 210 within the window -> a second, amended snapshot.
    mostly = tmp_path / 'amend.csv'
    mostly.write_text(
        'device,seq,slot,meter,at,value\n'
        'H-MAIN,3,MAIN,M-MAIN,2026-02-01T12:00:00+00:00,210\n',
        encoding='utf-8')
    from meterround.ingest import ingest as ingest_more
    ingest_more(mostly, db)
    evaluate(db)
    con.close()

    con = connect(db)
    second = billing.bill(con, contract, 'usage_share')
    assert second['revision'] == 2
    assert second['status'] == '已出账'
    entries = con.execute('SELECT tenant_id,kind,amount_cents,reverses_entry_id FROM billing_entry ORDER BY id').fetchall()
    kinds = {(row[1], row[0]) for row in entries}
    assert ('reversal', 'A') in kinds and ('reversal', 'B') in kinds
    nets = statement(con, contract, 'usage_share')['net_cents']
    # The first +167/-167 is reversed; net equals the second bill's true-up.
    assert nets['A'] == lines_of(second)['A']['true_up_cents']
    assert nets['B'] == lines_of(second)['B']['true_up_cents']
    # Net across the ledger is the new bill minus INV-001, never the old + new sum.
    assert sum(nets.values()) == second['main_cents'] - 10000 == 1000
    # INV-001 and the source rows are untouched: append-only.
    invoice = con.execute('SELECT document FROM issued_invoice WHERE invoice_id=?', ('INV-001',)).fetchone()
    assert '6500' in invoice[0] and '3500' in invoice[0]
    assert con.execute('SELECT count(*) FROM reading').fetchone()[0] == 7
    con.close()


def test_second_revision_does_not_accumulate_with_inv001(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    billing.bill(con, contract, 'usage_share')
    # Lower MAIN to 95 while tenant usage stays 60/30: a valid but different snapshot.
    con.execute("UPDATE decision SET usage=95 WHERE slot='MAIN'")
    con.commit()
    second = billing.bill(con, contract, 'usage_share')
    assert second['status'] == '已出账' and second['revision'] == 2
    nets = statement(con, contract, 'usage_share')['net_cents']
    assert nets == {'A': lines_of(second)['A']['true_up_cents'], 'B': lines_of(second)['B']['true_up_cents']}
    # The old +167/-167 does not survive: net equals the new true-up against INV-001.
    assert nets == {'A': -167, 'B': -333}
    assert sum(nets.values()) == second['main_cents'] - 10000 == -500
    con.close()


# ---------------------------------------------------------------------------
# Audit trail: every entry is traceable back to contract/batch/decision/source.
# ---------------------------------------------------------------------------

def test_entries_reference_contract_batch_decisions_and_evidence(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    billing.bill(con, contract, 'usage_share')
    entries = [dict(row) for row in con.execute('SELECT * FROM billing_entry')]
    assert len(entries) == 2
    import json
    for entry in entries:
        assert entry['contract_id'] == contract
        assert entry['batch_id'] == 'period-bill'
        refs = json.loads(entry['decision_refs'])
        assert set(refs) == {'MAIN', 'TEN-A', 'TEN-B'}
        assert all(ref['rule_version'] for ref in refs.values())
        assert len(entry['source_sha256']) == 64 and len(entry['evidence_sha256']) == 64
    con.close()


def test_statement_replays_issuance_reversal_and_net(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    billing.bill(con, contract, 'usage_share')
    con.execute("UPDATE decision SET usage=90 WHERE slot='TEN-A'")
    con.commit()
    billing.bill(con, contract, 'usage_share')
    report = statement(con, contract, 'usage_share')
    assert report['net_cents']
    for entry in report['entries']:
        if entry['kind'] == 'reversal':
            assert entry['reverses_entry_id'] is not None
        else:
            assert entry['reverses_entry_id'] is None
    net = {t: 0 for t in ('A', 'B')}
    for entry in report['entries']:
        net[entry['tenant_id']] += entry['amount_cents']
    assert net == report['net_cents']
    con.close()


def test_tenant_cannot_alias_the_main_slot(tmp_path):
    db, contract = prepared(tmp_path)
    con = connect(db)
    con.execute("UPDATE billing_tenant SET slot='MAIN' WHERE tenant_id='B'")
    con.commit()
    result = billing.bill(con, contract, 'usage_share')
    assert result['status'] == '待核' and result['reasons'] == ['tenant_is_main']
    con.close()


def test_contract_change_does_not_rewrite_issued_bill_or_invoice(tmp_path):
    import json
    from meterround.store import save_contract
    db, contract = prepared(tmp_path)
    con = connect(db)
    first = billing.bill(con, contract, 'usage_share')
    document = json.loads((FIX / 'billing_contract.json').read_text(encoding='utf-8'))
    document['issued_invoice']['tenant_total_cents'] = {'A': 6000, 'B': 4000}
    save_contract(con, document)
    # The issued invoice body and identity are append-only.
    stored = con.execute('SELECT document FROM issued_invoice WHERE invoice_id=?', ('INV-001',)).fetchone()[0]
    assert json.loads(stored)['tenant_total_cents'] == {'A': 6500, 'B': 3500}
    second = billing.bill(con, contract, 'usage_share')
    assert second['revision'] == 2 and second['bill_id'] != first['bill_id']
    # Net still computes against the immutable INV-001 amount (6500), not the rewritten one.
    assert lines_of(second)['A']['issued_cents'] == 6500
    assert lines_of(second)['A']['true_up_cents'] == 167
    con.close()


def test_only_contracted_allocation_methods_are_billed(tmp_path):
    import json
    from meterround.store import save_contract
    document = json.loads((FIX / 'billing_contract.json').read_text(encoding='utf-8'))
    document['allocation_contracts'] = ['capacity_share']
    db, contract = prepared(tmp_path, contract=json.dumps(document))
    con = connect(db)
    result = billing.bill_all(con, contract)
    assert set(result) == {'capacity_share'}
    blocked = billing.bill(con, contract, 'usage_share')
    assert blocked['status'] == '待核' and blocked['reasons'] == ['allocation_not_contracted']
    con.close()


# ---------------------------------------------------------------------------
# The CLI writes a reviewable JSON bill.
# ---------------------------------------------------------------------------

def test_bill_cli_writes_reviewable_json(tmp_path):
    import json
    from meterround.__main__ import bill as bill_command
    db, _ = prepared(tmp_path)
    out = tmp_path / 'review.json'
    bill_command(db, [FIX / 'billing_contract.json'], out)
    written = json.loads(out.read_text(encoding='utf-8'))
    bills = written['contracts'][0]['bills']
    assert bills['usage_share']['lines'][0]['amount_cents'] == 6667
    assert bills['capacity_share']['lines'][0]['amount_cents'] == 6250
    assert bills['usage_share']['reasons'] == []
    assert written['contracts'][0]['statement'][0]['entries']

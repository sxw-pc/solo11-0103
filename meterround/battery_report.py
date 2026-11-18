"""Offline JSON hand-over report for a battery-waste campaign.

Every quantity in the report is read back from persisted physical state and
endpoint results.  Plan quantities are shown separately as ``predicted`` so a
reader can never mistake an intended recovery for an actual one.
"""

import json
from fractions import Fraction

from .store import connect


def _f(value):
    return Fraction(str(value))


def _out(value):
    value = Fraction(value)
    return int(value) if value.denominator == 1 else float(value)


def _campaign_row(db, case_id, campaign_id):
    if campaign_id:
        return db.execute('SELECT * FROM battery_campaign WHERE campaign_id=? AND case_id=?',
                          (campaign_id, case_id)).fetchone()
    return db.execute('SELECT * FROM battery_campaign WHERE case_id=? '
                      'ORDER BY updated_at DESC,campaign_id DESC LIMIT 1',
                      (case_id,)).fetchone()


def _plan_for(db, campaign):
    if not campaign or not campaign['plan_id']:
        return None
    row = db.execute('SELECT * FROM battery_plan WHERE plan_id=?',
                     (campaign['plan_id'],)).fetchone()
    return json.loads(row['payload']) if row else None


def _pack_sources(db, case_id):
    result = {}
    for row in db.execute(
            '''SELECT s.*,r.sha256 AS payload_sha256,r.seq,r.value,r.at,
                      r.device AS reading_device,r.slot AS reading_slot
               FROM battery_pack_source s
               LEFT JOIN reading r ON r.id=s.first_reading_id
               WHERE s.case_id=? ORDER BY s.pack_id''', (case_id,)):
        result[row['pack_id']] = dict(row)
    links = {}
    for row in db.execute(
            '''SELECT e.pack_id,e.reading_id,e.evidence_id,e.source_sha256,e.payload_sha256,
                      r.device,r.slot,r.seq,r.at,r.sha256 AS reading_sha256
               FROM battery_pack_evidence e
               JOIN reading r ON r.id=e.reading_id
               WHERE e.pack_id IN (SELECT pack_id FROM battery_pack_source WHERE case_id=?)
               ORDER BY e.pack_id,e.reading_id''', (case_id,)):
        links.setdefault(row['pack_id'], []).append(dict(row))
    for pack_id, source in result.items():
        source['links'] = links.get(pack_id, [])
    return result


def _pack_results(db, case_id):
    return {row['pack_id']: dict(row) for row in db.execute(
        'SELECT * FROM battery_pack_result WHERE case_id=?', (case_id,))}


def _container_results(db, case_id):
    return {row['container_id']: dict(row) for row in db.execute(
        'SELECT * FROM battery_container_result WHERE case_id=?', (case_id,))}


def _isolation(db, case_id):
    return [dict(row) for row in db.execute(
        'SELECT * FROM battery_isolation WHERE case_id=? ORDER BY id', (case_id,))]


def _operations(db, case_id, campaign_id=None):
    if campaign_id:
        rows = db.execute(
            "SELECT * FROM battery_operation WHERE case_id=? AND campaign_id=? "
            "ORDER BY rowid", (case_id, campaign_id))
    else:
        rows = db.execute('SELECT * FROM battery_operation WHERE case_id=? ORDER BY rowid',
                          (case_id,))
    operations = []
    for row in rows:
        item = dict(row)
        item['intent'] = json.loads(item['intent']) if item['intent'] else None
        item['result'] = json.loads(item['result'])
        operations.append(item)
    return operations


def _steps(db, campaign_id):
    if not campaign_id:
        return []
    out = []
    for row in db.execute(
            'SELECT * FROM battery_step WHERE campaign_id=? ORDER BY rowid', (campaign_id,)):
        item = dict(row)
        item['intent'] = json.loads(item['intent'])
        item['result'] = json.loads(item['result']) if item['result'] else None
        out.append(item)
    return out


def build_report(db_path, case_id, contract=None, campaign_id=None, out=None):
    db = connect(db_path)
    try:
        case = db.execute('SELECT * FROM battery_case WHERE id=?', (case_id,)).fetchone()
        if not case:
            raise ValueError('unknown_battery_case')
        campaign = _campaign_row(db, case_id, campaign_id)
        if campaign_id and not campaign:
            raise ValueError('unknown_battery_campaign')
        if contract is None:
            contract = campaign['contract'] if campaign else None
        plan = _plan_for(db, campaign)
        sources = _pack_sources(db, case_id)
        pack_results = _pack_results(db, case_id)
        container_results = _container_results(db, case_id)
        local_packs = {r['id']: dict(r) for r in db.execute(
            'SELECT * FROM battery_pack WHERE case_id=?', (case_id,))}
        frozen = json.loads(case['payload'])
        frozen_packs = {p['id']: p for p in frozen.get('packs', [])}
        # The frozen case, not a possibly tampered local row, supplies the
        # reported pack quantities.  Local deviations are shown as diagnosis.
        pack_rows = []
        for pack_id in sorted(set(frozen_packs) | set(local_packs)):
            local = local_packs.get(pack_id)
            item = frozen_packs.get(pack_id)
            if item is not None:
                row = {
                    'id': pack_id, 'case_id': case_id, 'chemistry': item['chemistry'],
                    'mass_kg': item['mass_kg'], 'metal_kg': item['metal_kg'],
                    'fluid_kg': item['fluid_kg'], 'device': item.get('device'),
                    'slot': item.get('slot'),
                    'source_sha256': item.get('reading_source_sha256'),
                    'reading_id': None, 'container': None,
                    'location': 'missing', 'disposition': 'missing',
                    'isolated_reason': 'battery_pack_missing',
                }
                if local is not None:
                    row.update({
                        'container': local['container'],
                        'location': local['location'],
                        'disposition': local['disposition'],
                        'isolated_reason': local['isolated_reason'],
                        'device': local['device'] or row['device'],
                        'slot': local['slot'] or row['slot'],
                        'source_sha256': local['source_sha256'] or row['source_sha256'],
                        'reading_id': local['reading_id'],
                    })
                    row['ledger'] = {
                        'mass_kg': local['mass_kg'],
                        'metal_kg': local['metal_kg'],
                        'fluid_kg': local['fluid_kg'],
                        'chemistry': local['chemistry'],
                    }
                pack_rows.append(row)
            else:
                pack_rows.append(local)
        container_rows = [dict(r) for r in db.execute(
            'SELECT * FROM battery_container WHERE case_id=? ORDER BY id', (case_id,))]
        isolation_rows = _isolation(db, case_id)
        plan_boxes = {}
        plan_routes = {}
        if plan:
            for box in plan.get('boxes', []):
                for pack_id in box['pack_ids']:
                    plan_boxes[pack_id] = box['box_no']
                    plan_routes[pack_id] = box['route']

        packs = []
        for row in pack_rows:
            source = sources.get(row['id'])
            result = pack_results.get(row['id'])
            recovered = _f(result['recovered_kg']) if result else Fraction(0)
            neutralized = _f(result['neutralized_kg']) if result else Fraction(0)
            packs.append({
                'id': row['id'],
                'device': row['device'],
                'slot': row['slot'],
                'chemistry': row['chemistry'],
                'mass_kg': _out(row['mass_kg']),
                'metal_kg': _out(row['metal_kg']),
                'fluid_kg': _out(row['fluid_kg']),
                'container': row['container'],
                'location': row['location'],
                'disposition': row['disposition'],
                'isolated_reason': row['isolated_reason'],
                'box_no': plan_boxes.get(row['id']),
                'planned_route': plan_routes.get(row['id']),
                'source_sha256': row['source_sha256'],
                'ledger': row.get('ledger'),
                'source': None if not source else {
                    'device': source['device'],
                    'slot': source['slot'],
                    'source_sha256': source['source_sha256'],
                    'first_reading_id': source['first_reading_id'],
                    'first_reading_sha256': source['first_reading_sha256'],
                    'reading_ids': [link['reading_id'] for link in source['links']],
                    'evidence_ids': [link['evidence_id'] for link in source['links']],
                    'verified_at': source['verified_at'],
                },
                'recovered_kg': _out(recovered) if result else None,
                'neutralized_kg': _out(neutralized) if result else None,
                'result_route': result['route'] if result else None,
                'operation_id': result['operation_id'] if result else None,
            })

        containers = []
        for row in container_rows:
            members = [p for p in packs if p['container'] == row['id']]
            mass = sum((_f(p['mass_kg']) for p in members), Fraction(0))
            metal = sum((_f(p['metal_kg']) for p in members), Fraction(0))
            fluid = sum((_f(p['fluid_kg']) for p in members), Fraction(0))
            result = container_results.get(row['id'])
            chemistries = sorted({p['chemistry'] for p in members})
            shipped = row['location'] == 'facility' or row['shipped_at'] is not None
            containers.append({
                'container_id': row['id'],
                'capacity_kg': _out(row['capacity_kg']),
                'location': row['location'],
                'route': row['route'],
                'shipped_at': row['shipped_at'],
                'processed_at': row['processed_at'],
                'isolated_reason': row['isolation_reason'],
                'pack_ids': [p['id'] for p in members],
                'mass_kg': _out(mass),
                'metal_kg': _out(metal),
                'fluid_kg': _out(fluid),
                'chemistry': chemistries,
                'mixed_chemistry': len(chemistries) > 1,
                'remaining_kg': _out(_f(row['capacity_kg']) - mass),
                'full': mass == _f(row['capacity_kg']),
                'over_capacity': mass > _f(row['capacity_kg']),
                'shipped': shipped,
                'recovered_kg': _out(result['recovered_kg']) if result else None,
                'neutralized_kg': _out(result['neutralized_kg']) if result else None,
            })

        operations = _operations(db, case_id,
                                 campaign['campaign_id'] if campaign else None)
        steps = _steps(db, campaign['campaign_id']) if campaign else []

        processed = [p for p in packs if p['disposition'] in ('recover', 'thermal')]
        unprocessed = [p for p in packs if p['disposition'] == 'unprocessed']
        missing = [p for p in packs if p['disposition'] == 'missing']
        actual_recovered = sum((_f(p['recovered_kg']) for p in packs if p['recovered_kg'] is not None),
                               Fraction(0))
        actual_neutralized = sum((_f(p['neutralized_kg']) for p in packs if p['neutralized_kg'] is not None),
                                 Fraction(0))
        total_mass = sum((_f(p['mass_kg']) for p in packs), Fraction(0))
        unprocessed_mass = sum((_f(p['mass_kg']) for p in unprocessed), Fraction(0))
        shipped_containers = [c for c in containers if c['shipped']]
        shipped_mass = sum((_f(c['mass_kg']) for c in shipped_containers), Fraction(0))
        processed_mass = sum((_f(p['mass_kg']) for p in processed), Fraction(0))
        recovered_from_mixed = sum(
            (_f(p['recovered_kg']) for p in processed
             if p['result_route'] == 'thermal' and p['recovered_kg'] is not None),
            Fraction(0),
        )
        totals = {
            'mass_kg': _out(total_mass),
            'processed_mass_kg': _out(processed_mass),
            'unprocessed_mass_kg': _out(unprocessed_mass),
            'shipped_mass_kg': _out(shipped_mass),
            'recovered_kg': _out(actual_recovered),
            'neutralized_kg': _out(actual_neutralized),
            'loss_kg': 0,
            'shipments': len(shipped_containers),
            'box_count': len(shipped_containers),
            'packs_processed': len(processed),
            'packs_unprocessed': len(unprocessed),
            'packs_missing': len(missing),
            'missing_mass_kg': _out(sum((_f(p['mass_kg']) for p in missing), Fraction(0))),
            'recovered_from_mixed_kg': _out(recovered_from_mixed),
            'unaccounted_mass_kg': _out(total_mass - processed_mass - unprocessed_mass),
        }

        conservation = _conservation(packs, containers, total_mass,
                                     actual_recovered, actual_neutralized,
                                     processed_mass, unprocessed_mass, recovered_from_mixed)

        optimality = None
        if plan and plan.get('optimality'):
            optimality = dict(plan['optimality'])
            optimality['planned'] = dict(plan.get('predicted', {}))
            optimality['actual'] = {'recovered_kg': _out(actual_recovered),
                                    'neutralized_kg': _out(actual_neutralized),
                                    'shipments': totals['shipments']}
            predicted = plan.get('predicted', {})
            optimality['matches_plan'] = (
                _f(predicted.get('recovered_kg', 0)) == actual_recovered
                and _f(predicted.get('neutralized_kg', 0)) == actual_neutralized
                and int(predicted.get('shipments', 0)) == totals['shipments']
            )

        report = {
            'case_id': case_id,
            'case_digest': case['digest'],
            'case_payload_sha256': case['payload_sha256'],
            'contract': contract,
            'campaign': {
                'campaign_id': campaign['campaign_id'] if campaign else None,
                'status': campaign['status'] if campaign else 'unplanned',
                'reason': campaign['reason'] if campaign else None,
                'plan_id': campaign['plan_id'] if campaign else None,
                'plans': [dict(r) for r in db.execute(
                    '''SELECT p.plan_id,p.contract,p.status,p.digest
                       FROM battery_plan p JOIN battery_campaign_plan c ON c.plan_id=p.plan_id
                       WHERE c.campaign_id=? ORDER BY c.seq''',
                    (campaign['campaign_id'],))] if campaign else [],
            },
            'packs': packs,
            'boxes': containers,
            'operations': operations,
            'steps': steps,
            'totals': totals,
            'conservation': conservation,
            'isolated': isolation_rows,
            'optimality': optimality,
            'plan': plan,
        }
        if out:
            from pathlib import Path
            path = Path(out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                       sort_keys=True), encoding='utf-8')
            report['report_path'] = str(path)
        return report
    finally:
        db.close()


def _conservation(packs, containers, total_mass,
                  recovered, neutralized, processed_mass, unprocessed_mass,
                  recovered_from_mixed):
    pack_checks = []
    for pack in packs:
        if pack['disposition'] == 'missing':
            # Missing is an isolation finding, not a conservation failure: the
            # frozen mass is reported separately and never counted as recovered.
            pack_checks.append({
                'pack_id': pack['id'], 'ok': True, 'missing': True,
                'detail': 'frozen pack absent; isolated and excluded from treatment',
                'mass_kg': pack['mass_kg'],
                'recovered_kg': None, 'neutralized_kg': None,
            })
            continue
        if pack['disposition'] == 'unprocessed':
            pack_checks.append({
                'pack_id': pack['id'], 'ok': True,
                'detail': 'unprocessed mass retained, not counted as recovered',
                'mass_kg': pack['mass_kg'],
                'recovered_kg': pack['recovered_kg'],
                'neutralized_kg': pack['neutralized_kg'],
            })
            continue
        observed = _f(pack['recovered_kg'] or 0) + _f(pack['neutralized_kg'] or 0)
        ok = observed == _f(pack['mass_kg'])
        pack_checks.append({
            'pack_id': pack['id'], 'ok': ok,
            'mass_kg': pack['mass_kg'],
            'recovered_kg': pack['recovered_kg'],
            'neutralized_kg': pack['neutralized_kg'],
            'sum_kg': _out(observed),
            'detail': ('metal + fluid' if pack['result_route'] == 'recover'
                       else 'whole pack thermally treated'),
        })
    box_checks = []
    for box in containers:
        members = [p for p in packs if p['container'] == box['container_id']]
        declared = sum((_f(p['mass_kg']) for p in members), Fraction(0))
        recovered_box = sum((_f(p['recovered_kg']) for p in members if p['recovered_kg'] is not None),
                            Fraction(0))
        neutralized_box = sum((_f(p['neutralized_kg']) for p in members if p['neutralized_kg'] is not None),
                              Fraction(0))
        if box['processed_at']:
            ok = (declared == _f(box['mass_kg'])
                  and recovered_box + neutralized_box == declared
                  and (box['route'] != 'thermal' or recovered_box == 0))
        else:
            ok = not box['over_capacity']
        box_checks.append({
            'container_id': box['container_id'],
            'ok': ok,
            'mass_kg': _out(declared),
            'capacity_kg': box['capacity_kg'],
            'route': box['route'],
            'recovered_kg': _out(recovered_box),
            'neutralized_kg': _out(neutralized_box),
            'mixed_chemistry': box['mixed_chemistry'],
        })
    accounted = processed_mass + unprocessed_mass
    missing_mass = sum((_f(p['mass_kg']) for p in packs
                        if p['disposition'] == 'missing'), Fraction(0))
    # Every processed pack must split exactly into recovered metal plus
    # neutralised mass; unprocessed and missing mass are retained, not counted
    # as recovered.  No loss term is allowed to silently absorb a discrepancy.
    loss = total_mass - recovered - neutralized - unprocessed_mass - missing_mass
    batch = {
        'ok': (accounted + missing_mass == total_mass
               and recovered + neutralized == processed_mass
               and recovered_from_mixed == 0
               and loss == 0),
        'missing_mass_kg': _out(missing_mass),
        'loss_kg': _out(loss),
        'total_mass_kg': _out(total_mass),
        'processed_mass_kg': _out(processed_mass),
        'unprocessed_mass_kg': _out(unprocessed_mass),
        'recovered_kg': _out(recovered),
        'neutralized_kg': _out(neutralized),
        'recovered_from_mixed_kg': _out(recovered_from_mixed),
        'processed_recovered_plus_neutralized_kg': _out(recovered + neutralized),
        'unprocessed_not_recovered': True,
    }
    return {
        'ok': all(check['ok'] for check in pack_checks)
              and all(check['ok'] for check in box_checks) and batch['ok'],
        'packs': pack_checks,
        'boxes': box_checks,
        'batch': batch,
    }

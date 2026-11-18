"""绿色属性权利与凭证发行台账。

设计约束（与合同/证据的不可变性一致）：

* 合同以文件字节快照冻结；权属基础另以 decision/rule_version 与
  reading/evidence/source 摘要冻结。同一快照重跑不新发。
* 三个表位必须同批次/期间、decision 均可结算，且总发电严格等于
  自用+上网；缺读、待核、逆流或闭合差额一律拒绝发行。
* 同一批次只能有一个有效权属发行。场站与购电方的并发申请由
  ``BEGIN IMMEDIATE`` 写事务与部分唯一索引共同裁决，只有一方成功。
* 证据修订时旧发行全量撤销/待召回，新基础在同一个事务里发行；
  已对外转让的旧证转为召回任务，不静默换主。
* 未满一张凭证的余量只按同一合同/场站/权属/持有人链结转，不四舍五入、
  不跨不相干期间凑证。
* 本模块不修改 source/reading/evidence/decision 原始记录。

错误统一以带稳定 code 的 :class:`LedgerError` 抛出。
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .store import connect
from .ingest import at

SETTLEABLE = '可结算'
TITLE_MODES = ('gross_generation_title', 'net_export_title')
TITLE_ABBREV = {'gross_generation_title': 'GROSS', 'net_export_title': 'NET'}
EPSILON = 1e-9

# 这些错误意味着当前证据不足以支撑任何发行；reissue 会因此只撤销不重发。
EVIDENCE_BLOCKING_CODES = frozenset({
    'batch_mismatch', 'decision_unsettleable', 'missing_decision',
    'balance_not_closed', 'reverse_flow', 'no_eligible_energy',
})

CODE_MESSAGES = {
    'invalid_config': '属性合同配置无效',
    'contract_hash_mismatch': '合同文件字节与声明的 SHA-256 不一致',
    'batch_mismatch': '三个表位不属于同一批次/期间',
    'decision_unsettleable': '存在待核的表位判定，不能发行可交易凭证',
    'missing_decision': '缺少表位判定，不能发行可交易凭证',
    'balance_not_closed': '总发电与自用+上网不闭合，拒绝猜测可交易凭证',
    'reverse_flow': '存在逆流，拒绝发行',
    'unknown_title_mode': '未知权属合同模式',
    'no_eligible_energy': '可归属能量不足一张凭证',
    'conflict': '该批次已有有效权属发行，或并发申请未获准',
    'reissue_blocked': '修订后的证据不足以重新发行，旧发行已撤销',
    'not_found': '未找到指定发行或凭证',
    'invalid_state': '当前台账状态不允许该操作',
}


class LedgerError(Exception):
    """带稳定 code 的属性台账错误。"""

    def __init__(self, code, detail=''):
        self.code = code
        self.detail = detail
        message = CODE_MESSAGES.get(code, code)
        if detail:
            message += ': ' + detail
        super().__init__(message)


# 兼容早期草稿中的名字。
AttributeError_ = LedgerError


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args)]


def load_spec(config_path):
    """读取属性合同配置并校验合同文件字节哈希。"""
    path = Path(config_path)
    try:
        spec = json.loads(path.read_text(encoding='utf-8'))
        for key in ('batch_id', 'generation_slot', 'self_use_slot', 'export_slot',
                    'unit_per_certificate', 'contract_file', 'contract_sha256', 'contracts'):
            if key not in spec:
                raise KeyError('missing_' + key)
        unit = float(spec['unit_per_certificate'])
        if unit <= 0:
            raise ValueError('unit_per_certificate')
        for mode in TITLE_MODES:
            if mode not in spec['contracts']:
                raise ValueError('missing_contract_' + mode)
            if not spec['contracts'][mode].get('holder'):
                raise ValueError('missing_holder_' + mode)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LedgerError('invalid_config', str(exc))
    contract_path = (path.parent / spec['contract_file']).resolve()
    try:
        raw = contract_path.read_bytes()
    except OSError as exc:
        raise LedgerError('invalid_config', str(exc))
    actual = hashlib.sha256(raw).hexdigest()
    if actual != spec['contract_sha256']:
        raise LedgerError('contract_hash_mismatch', actual)
    spec['unit_per_certificate'] = unit
    spec['_config_path'] = str(path.resolve())
    spec['_config_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    spec['_contract_path'] = str(contract_path)
    spec['_contract_raw'] = raw
    spec['_contract_sha256'] = actual
    return spec


def _validate_slots(db, spec):
    """三个表位必须存在、同批次、decision 均可结算且非逆流。"""
    slots = [spec['generation_slot'], spec['self_use_slot'], spec['export_slot']]
    if len(set(slots)) != 3:
        raise LedgerError('invalid_config', 'slots must be distinct')
    for slot in slots:
        row = db.execute('SELECT id,batch_id FROM slot WHERE id=?', (slot,)).fetchone()
        if row is None or row['batch_id'] != spec['batch_id']:
            raise LedgerError('batch_mismatch', slot)
    batch = db.execute('SELECT * FROM batch WHERE id=?', (spec['batch_id'],)).fetchone()
    if batch is None:
        raise LedgerError('batch_mismatch', spec['batch_id'])
    decisions = {}
    for slot in slots:
        row = db.execute('SELECT * FROM decision WHERE slot=?', (slot,)).fetchone()
        if row is None:
            raise LedgerError('missing_decision', slot)
        if row['status'] != SETTLEABLE or row['usage'] is None:
            raise LedgerError('decision_unsettleable', slot)
        if row['usage'] < -EPSILON:
            raise LedgerError('reverse_flow', slot)
        decisions[slot] = dict(row)
    return dict(batch), decisions


def _evidence_digests(db, spec, decisions):
    """冻结本属性基础相关的 reading/evidence/source 摘要与规则版本。"""
    slots = (spec['generation_slot'], spec['self_use_slot'], spec['export_slot'])
    marks = ','.join('?' for _ in slots)
    readings = _rows(db, 'SELECT * FROM reading WHERE slot IN (%s) ORDER BY id' % marks, slots)
    reading_payload = [{k: r[k] for k in (
        'id', 'source_id', 'line_no', 'device', 'seq', 'slot', 'meter', 'at', 'value', 'sha256')}
        for r in readings]
    evidence = _rows(db, 'SELECT * FROM evidence WHERE reading_id IN '
                         '(SELECT id FROM reading WHERE slot IN (%s)) ORDER BY id' % marks, slots)
    sources = _rows(db, 'SELECT id,sha256,raw FROM source WHERE id IN '
                        '(SELECT source_id FROM reading WHERE slot IN (%s)) ORDER BY id' % marks,
                    slots)
    source_hash = hashlib.sha256()
    for src in sources:
        source_hash.update(src['sha256'].encode('ascii'))
        source_hash.update(hashlib.sha256(src['raw']).digest())
    decision_payload = {slot: {
        'status': decisions[slot]['status'],
        'usage': decisions[slot]['usage'],
        'rule_version': decisions[slot]['rule_version'],
        'detail_sha256': hashlib.sha256(decisions[slot]['detail'].encode('utf-8')).hexdigest(),
    } for slot in sorted(decisions)}
    return {
        'reading_digest': _digest(reading_payload),
        'evidence_digest': _digest(evidence),
        'source_digest': source_hash.hexdigest(),
        'decision_digest': _digest(decision_payload),
        'rule_versions': {slot: decisions[slot]['rule_version'] for slot in sorted(decisions)},
    }


def _validate_balance(gen, self_use, export):
    """总发电必须严格等于自用+上网；逆流单独拒绝。"""
    values = {'generation': gen, 'self_use': self_use, 'export': export}
    if min(gen, self_use, export) < -EPSILON:
        raise LedgerError('reverse_flow', _json(values))
    if abs(gen - (self_use + export)) > EPSILON:
        raise LedgerError('balance_not_closed', _json(values))


def _title_holder(spec, title_mode):
    if title_mode not in TITLE_MODES:
        raise LedgerError('unknown_title_mode', str(title_mode))
    return spec['contracts'][title_mode]['holder']


def _eligible_units(title_mode, usage):
    """gross 权属覆盖全部发电；net 权属只覆盖上网，自用不归购电方。"""
    return usage['generation'] if title_mode == 'gross_generation_title' else usage['export']


def _site(spec):
    return spec.get('site') or spec['batch_id']


def _prepare(db, spec, title_mode):
    """校验并计算本次发行的全部冻结摘要。"""
    holder = _title_holder(spec, title_mode)
    batch, decisions = _validate_slots(db, spec)
    usage = {
        'generation': decisions[spec['generation_slot']]['usage'],
        'self_use': decisions[spec['self_use_slot']]['usage'],
        'export': decisions[spec['export_slot']]['usage'],
    }
    _validate_balance(usage['generation'], usage['self_use'], usage['export'])
    digests = _evidence_digests(db, spec, decisions)
    prepared = dict(spec)
    prepared.update({'title_mode': title_mode, 'holder': holder, 'batch': batch,
                     'usage': usage, 'digests': digests, 'site': _site(spec)})
    prepared['eligible_units'] = _eligible_units(title_mode, usage)
    body = {
        'contract_sha256': spec['_contract_sha256'],
        # 只冻结合同声明的语义字段，配置文件重新排版不应触发换发。
        'contract_terms': {
            'title_mode': title_mode, 'holder': holder,
            'unit_per_certificate': spec['unit_per_certificate'],
            'batch_id': spec['batch_id'], 'site': prepared['site'],
            'slots': [spec['generation_slot'], spec['self_use_slot'], spec['export_slot']],
            'generation_slot': spec['generation_slot'],
            'self_use_slot': spec['self_use_slot'],
            'export_slot': spec['export_slot'],
        },
        'period': [batch['start'], batch['end']],
        'usage': usage,
        'digests': digests,
    }
    prepared['fingerprint'] = _digest(body)
    prepared['evidence_fingerprint'] = _digest({
        'batch_id': spec['batch_id'], 'site': prepared['site'],
        'slots': body['contract_terms']['slots'], 'period': body['period'],
        'usage': usage, 'digests': digests})
    return prepared


def _contract_row(db, spec, prepared):
    db.execute('INSERT OR IGNORE INTO attribute_contract'
               '(sha256,raw,contract_file,config_sha256,title_mode,holder,unit_per_certificate) '
               'VALUES (?,?,?,?,?,?,?)',
               (spec['_contract_sha256'], spec['_contract_raw'], spec['_contract_path'],
                spec['_config_sha256'], prepared['title_mode'], prepared['holder'],
                spec['unit_per_certificate']))
    return db.execute('SELECT * FROM attribute_contract WHERE sha256=? '
                      'AND title_mode=? AND holder=? AND unit_per_certificate=?',
                      (spec['_contract_sha256'], prepared['title_mode'],
                       prepared['holder'], spec['unit_per_certificate'])).fetchone()


def _basis_row(db, spec, prepared, contract_id):
    existing = db.execute('SELECT * FROM attribute_basis WHERE fingerprint=?',
                          (prepared['fingerprint'],)).fetchone()
    if existing:
        return existing
    usage = prepared['usage']
    digests = prepared['digests']
    cur = db.execute(
        'INSERT INTO attribute_basis(fingerprint,contract_id,batch_id,site,generation_slot,'
        'self_use_slot,export_slot,period_start,period_end,generation_usage,self_use_usage,'
        'export_usage,generation_status,self_use_status,export_status,rule_versions,'
        'decision_digest,reading_digest,evidence_digest,source_digest,evidence_fingerprint,'
        'unit_per_certificate,eligible_units,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (prepared['fingerprint'], contract_id, spec['batch_id'], prepared['site'],
         spec['generation_slot'], spec['self_use_slot'], spec['export_slot'],
         prepared['batch']['start'], prepared['batch']['end'],
         usage['generation'], usage['self_use'], usage['export'],
         SETTLEABLE, SETTLEABLE, SETTLEABLE, _json(digests['rule_versions']),
         digests['decision_digest'], digests['reading_digest'], digests['evidence_digest'],
         digests['source_digest'], prepared['evidence_fingerprint'],
         spec['unit_per_certificate'], prepared['eligible_units'], _now()))
    return db.execute('SELECT * FROM attribute_basis WHERE id=?', (cur.lastrowid,)).fetchone()


def _active_issuance(db, batch_id, site=None):
    # 与 ux_attribute_issuance_active 一致：一个批次最多一个有效发行。
    return db.execute('SELECT * FROM attribute_issuance WHERE batch_id=? '
                      'AND status IN (?,?) ORDER BY id DESC LIMIT 1',
                      (batch_id, '有效', '待生效')).fetchone()


def _open_carries(db, spec, prepared, title_mode):
    """找出同一合同/场站/权属/持有人且严格早于本次期间的可结转余量。

    仅允许来源期间结束时间不晚于本次期间开始，杜绝跨不相干期间抵消。
    """
    candidates = _rows(db,
                       'SELECT c.*, b.period_end AS source_period_end FROM attribute_carry c '
                       'JOIN attribute_basis b ON b.id=c.basis_id '
                       'WHERE c.site=? AND c.contract_sha256=? AND c.title_mode=? AND c.holder=? '
                       'AND c.status=? AND c.batch_id<>? ORDER BY b.period_end,c.id',
                       (prepared['site'], spec['_contract_sha256'], title_mode,
                        prepared['holder'], '可结转', spec['batch_id']))
    start = at(prepared['batch']['start'])
    return [c for c in candidates if at(c['source_period_end']) <= start]


def _as_conflict(exc):
    """只把有效发行唯一索引的冲突解释为并发失败，其余完整性错误照旧抛出。"""
    if 'ux_attribute_issuance_active' in str(exc):
        return LedgerError('conflict', str(exc))
    return exc


def _next_serial(db, batch_id, site, title_mode):
    row = db.execute('SELECT MAX(serial) AS n FROM attribute_certificate WHERE batch_id=? AND site=? '
                     'AND title_mode=?', (batch_id, site, title_mode)).fetchone()
    return (row['n'] or 0) + 1


def _certificate_id(batch_id, site, title_mode, serial):
    def clean(value):
        return ''.join(ch if ch.isalnum() else '-' for ch in value.upper()).strip('-')
    parts = ['RC', clean(batch_id)]
    if clean(site) != clean(batch_id):
        parts.append(clean(site))
    parts.extend([TITLE_ABBREV[title_mode], '%04d' % serial])
    return '-'.join(parts)


def _issue_in_tx(db, spec, title_mode):
    """在已开启的写事务内发行；返回可审计结果。"""
    prepared = _prepare(db, spec, title_mode)
    active = _active_issuance(db, prepared['batch_id'], prepared['site'])
    if active is not None:
        same = db.execute('SELECT fingerprint FROM attribute_basis WHERE id=?',
                          (active['basis_id'],)).fetchone()
        if same and same['fingerprint'] == prepared['fingerprint'] and \
                active['title_mode'] == title_mode:
            return _existing_result(db, active)
        raise LedgerError('conflict', _json({
            'active_issuance': active['id'],
            'active_title_mode': active['title_mode'],
            'requested_title_mode': title_mode,
            'active_basis': same['fingerprint'] if same else None,
            'requested_basis': prepared['fingerprint'],
        }))
    contract = _contract_row(db, spec, prepared)
    basis = _basis_row(db, spec, prepared, contract['id'])
    unit = spec['unit_per_certificate']
    carries = _open_carries(db, spec, prepared, title_mode)
    opening = sum(float(c['closing_units']) for c in carries)
    total = opening + prepared['eligible_units']
    count = int((total + EPSILON) // unit)
    if count <= 0:
        raise LedgerError('no_eligible_energy', _json({
            'opening_carry': opening, 'eligible': prepared['eligible_units']}))
    issued_units = count * unit
    remainder = total - issued_units
    if remainder < EPSILON:
        remainder = 0.0
    carried_from = carries[-1]['issuance_id'] if carries else None
    cur = db.execute(
        'INSERT INTO attribute_issuance(basis_id,batch_id,site,contract_id,title_mode,holder,'
        'status,certificate_count,eligible_units,issued_units,period_remainder_units,'
        'opening_carry_units,closing_carry_units,carried_from_id,supersedes_id,reason,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (basis['id'], basis['batch_id'], basis['site'], contract['id'], title_mode,
         prepared['holder'], '有效', count, prepared['eligible_units'], issued_units, remainder,
         opening, remainder, carried_from, None, None, _now()))
    issuance_id = cur.lastrowid
    for carry in carries:
        db.execute('UPDATE attribute_carry SET status=?,closed_by_issuance_id=? WHERE id=?',
                   ('已结转', issuance_id, carry['id']))
    previous_carry_id = carries[-1]['id'] if carries else None
    carry_status = '可结转' if remainder > EPSILON else '已结转'
    db.execute('INSERT INTO attribute_carry(issuance_id,basis_id,batch_id,site,contract_sha256,'
               'title_mode,holder,unit_per_certificate,eligible_units,opening_units,'
               'period_remainder_units,closing_units,carried_from_id,closed_by_issuance_id,status,'
               'created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (issuance_id, basis['id'], basis['batch_id'], basis['site'],
                contract['sha256'], title_mode, prepared['holder'], unit,
                prepared['eligible_units'], opening, remainder, remainder,
                previous_carry_id,
                None if carry_status == '可结转' else issuance_id, carry_status, _now()))
    serial = _next_serial(db, basis['batch_id'], basis['site'], title_mode)
    certificates = []
    for offset in range(count):
        certificate_id = _certificate_id(basis['batch_id'], basis['site'], title_mode, serial + offset)
        db.execute(
            'INSERT INTO attribute_certificate(certificate_id,issuance_id,basis_id,basis_fingerprint,'
            'batch_id,site,period_start,period_end,contract_sha256,title_mode,holder,origin_holder,'
            'energy_share,serial,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (certificate_id, issuance_id, basis['id'], basis['fingerprint'], basis['batch_id'],
             basis['site'], basis['period_start'], basis['period_end'], contract['sha256'],
             title_mode, prepared['holder'], prepared['holder'], unit, serial + offset, '有效', _now()))
        certificates.append(certificate_id)
    return {
        'status': 'issued',
        'issuance_id': issuance_id,
        'basis_id': basis['id'],
        'basis_fingerprint': basis['fingerprint'],
        'contract_sha256': spec['_contract_sha256'],
        'batch_id': basis['batch_id'],
        'site': basis['site'],
        'title_mode': title_mode,
        'holder': prepared['holder'],
        'certificate_count': count,
        'certificates': certificates,
        'eligible_units': prepared['eligible_units'],
        'issued_units': issued_units,
        'opening_carry_units': opening,
        'period_remainder_units': remainder,
        'closing_carry_units': remainder,
        'carried_from_id': carried_from,
    }


def _existing_result(db, issuance):
    certs = _rows(db, 'SELECT certificate_id FROM attribute_certificate WHERE issuance_id=? '
                      'ORDER BY serial', (issuance['id'],))
    return {
        'status': 'unchanged',
        'issuance_id': issuance['id'],
        'basis_id': issuance['basis_id'],
        'contract_sha256': db.execute('SELECT sha256 FROM attribute_contract WHERE id=?',
                                      (issuance['contract_id'],)).fetchone()['sha256'],
        'batch_id': issuance['batch_id'],
        'site': issuance['site'],
        'title_mode': issuance['title_mode'],
        'holder': issuance['holder'],
        'certificate_count': issuance['certificate_count'],
        'certificates': [c['certificate_id'] for c in certs],
        'eligible_units': issuance['eligible_units'],
        'issued_units': issuance['issued_units'],
        'opening_carry_units': issuance['opening_carry_units'],
        'period_remainder_units': issuance['period_remainder_units'],
        'closing_carry_units': issuance['closing_carry_units'],
    }


def issue(config, title_mode, db_path):
    """发行一次权属。同合同/证据快照重跑返回 unchanged，不新发。"""
    spec = load_spec(config)
    db = connect(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            try:
                result = _issue_in_tx(db, spec, title_mode)
            except sqlite3.IntegrityError as exc:
                # 并发申请被部分唯一索引拒绝：对方已取得有效权利。
                raise _as_conflict(exc)
        except BaseException:
            db.rollback()
            raise
        db.commit()
    finally:
        db.close()
    return result


def _revoke_certificates(db, issuance, reason):
    """撤销一次发行的全部凭证；已转让的转召回任务并保留持有人。"""
    revoked = recalled = 0
    for cert in _rows(db, 'SELECT * FROM attribute_certificate WHERE issuance_id=? ORDER BY serial',
                      (issuance['id'],)):
        if cert['status'] == '已转让':
            db.execute('UPDATE attribute_certificate SET status=? WHERE id=?',
                       ('待召回', cert['id']))
            db.execute('INSERT OR IGNORE INTO attribute_recall(certificate_id,issuance_id,batch_id,'
                       'site,holder,reason,status,created_at) VALUES (?,?,?,?,?,?,?,?)',
                       (cert['id'], issuance['id'], cert['batch_id'], cert['site'],
                        cert['transferred_to'] or cert['holder'], reason, '待召回', _now()))
            recalled += 1
        else:
            db.execute('UPDATE attribute_certificate SET status=? WHERE id=?',
                       ('已撤销', cert['id']))
            revoked += 1
    return revoked, recalled


def _revoke_issuance(db, issuance, reason):
    revoked, recalled = _revoke_certificates(db, issuance, reason)
    status = '待召回' if recalled else '已撤销'
    db.execute('UPDATE attribute_issuance SET status=?,reason=?,revoked_at=? WHERE id=?',
               (status, reason, _now(), issuance['id']))
    # 本次发行消费掉的上期余量重新打开，保证结转链可追溯、不丢量。
    for carry in _rows(db, 'SELECT * FROM attribute_carry WHERE closed_by_issuance_id=? '
                           'AND issuance_id<>? AND status=?',
                       (issuance['id'], issuance['id'], '已结转')):
        db.execute('UPDATE attribute_carry SET status=?,closed_by_issuance_id=NULL WHERE id=?',
                   ('可结转', carry['id']))
    db.execute('UPDATE attribute_carry SET status=? WHERE issuance_id=?',
               ('已作废', issuance['id']))
    return revoked, recalled, status


def reissue(config, title_mode, db_path, reason='evidence_revised'):
    """证据修订后：同一事务内先全量撤销旧发行，再按新基础发行。

    旧证已对外转让的保留原持有人并生成召回任务，不把同一份能量属性
    静默转给新权利人。相同基础重跑返回 unchanged。若修订后的证据不再
    满足发行条件，旧发行仍会撤销（撤销量提交），错误继续抛出。
    """
    spec = load_spec(config)
    site = _site(spec)
    db = connect(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            active = _active_issuance(db, spec['batch_id'], site)
            if active is None:
                raise LedgerError('not_found', 'active issuance')
            try:
                prepared = _prepare(db, spec, title_mode)
            except LedgerError as exc:
                if exc.code not in EVIDENCE_BLOCKING_CODES:
                    raise
                prepared = exc
            if isinstance(prepared, LedgerError):
                same = None
            else:
                same = db.execute('SELECT fingerprint FROM attribute_basis WHERE id=?',
                                  (active['basis_id'],)).fetchone()
                if same and same['fingerprint'] == prepared['fingerprint'] and \
                        active['title_mode'] == title_mode:
                    result = _existing_result(db, active)
                    db.rollback()
                    return result
            revoked, recalled, old_status = _revoke_issuance(db, active, reason)
            if isinstance(prepared, LedgerError):
                # 修订后的证据不足以支撑新发行：提交撤销，绝不让旧证继续有效。
                db.commit()
                raise LedgerError('reissue_blocked', _json({
                    'cause': prepared.code,
                    'detail': prepared.detail,
                    'revoked_certificates': revoked, 'recall_tasks': recalled,
                    'superseded_issuance_id': active['id']}))
            try:
                result = _issue_in_tx(db, spec, title_mode)
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise _as_conflict(exc)
            except BaseException:
                db.rollback()
                raise
            db.execute('UPDATE attribute_issuance SET supersedes_id=? WHERE id=?',
                       (active['id'], result['issuance_id']))
            result.update({'revoked_certificates': revoked, 'recall_tasks': recalled,
                           'superseded_issuance_id': active['id'],
                           'superseded_status': old_status})
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        db.commit()
    finally:
        db.close()
    return result


def revoke(config, db_path, reason='revoked'):
    """显式撤销一个批次的有效发行；已转让凭证转为召回任务。"""
    spec = load_spec(config)
    db = connect(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            site = _site(spec)
            active = _active_issuance(db, spec['batch_id'], site)
            if active is None:
                raise LedgerError('not_found', 'active issuance')
            revoked, recalled, old_status = _revoke_issuance(db, active, reason)
        except BaseException:
            db.rollback()
            raise
        db.commit()
    finally:
        db.close()
    return {'issuance_id': active['id'], 'revoked_certificates': revoked,
            'recall_tasks': recalled, 'status': old_status}


def transfer(certificate_id, to_holder, db_path):
    """把一张有效凭证转让给外部持有人。"""
    db = connect(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            cert = db.execute('SELECT * FROM attribute_certificate WHERE certificate_id=?',
                              (certificate_id,)).fetchone()
            if cert is None:
                raise LedgerError('not_found', certificate_id)
            issuance = db.execute('SELECT status FROM attribute_issuance WHERE id=?',
                                  (cert['issuance_id'],)).fetchone()
            if issuance is None or issuance['status'] not in ('有效', '待生效'):
                raise LedgerError('invalid_state', 'issuance ' + str(issuance['status']))
            if cert['status'] != '有效':
                raise LedgerError('invalid_state', cert['status'])
            if not to_holder:
                raise LedgerError('invalid_config', 'empty holder')
            db.execute('UPDATE attribute_certificate SET status=?,transferred_to=?,transferred_at=? '
                       'WHERE id=?', ('已转让', to_holder, _now(), cert['id']))
        except BaseException:
            db.rollback()
            raise
        db.commit()
    finally:
        db.close()
    return {'certificate_id': certificate_id, 'holder': to_holder, 'status': '已转让'}


def complete_recall(certificate_id, db_path):
    """登记召回完成；凭证状态转为已召回。"""
    db = connect(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            cert = db.execute('SELECT * FROM attribute_certificate WHERE certificate_id=?',
                              (certificate_id,)).fetchone()
            if cert is None:
                raise LedgerError('not_found', certificate_id)
            recall = db.execute('SELECT * FROM attribute_recall WHERE certificate_id=?',
                                (cert['id'],)).fetchone()
            if recall is None:
                raise LedgerError('not_found', 'recall task')
            if recall['status'] == '已完成':
                raise LedgerError('invalid_state', 'recall already complete')
            db.execute('UPDATE attribute_recall SET status=?,completed_at=? WHERE id=?',
                       ('已完成', _now(), recall['id']))
            db.execute('UPDATE attribute_certificate SET status=? WHERE id=?', ('已召回', cert['id']))
        except BaseException:
            db.rollback()
            raise
        db.commit()
    finally:
        db.close()
    return {'certificate_id': certificate_id, 'status': '已召回'}


def _certificate_view(db, row):
    basis = db.execute('SELECT * FROM attribute_basis WHERE id=?', (row['basis_id'],)).fetchone()
    issuance = db.execute('SELECT status FROM attribute_issuance WHERE id=?',
                          (row['issuance_id'],)).fetchone()
    return {
        'certificate_id': row['certificate_id'],
        'period_start': row['period_start'],
        'period_end': row['period_end'],
        'batch_id': row['batch_id'],
        'site': row['site'],
        'holder': row['holder'],
        'current_holder': row['transferred_to'] or row['holder'],
        'origin_holder': row['origin_holder'],
        'transferred_to': row['transferred_to'],
        'energy_share': row['energy_share'],
        'serial': row['serial'],
        'title_mode': row['title_mode'],
        'contract_sha256': row['contract_sha256'],
        'basis_fingerprint': row['basis_fingerprint'],
        'status': row['status'],
        'issuance_id': row['issuance_id'],
        'issuance_status': issuance['status'] if issuance else None,
        'source': {
            'decision_digest': basis['decision_digest'],
            'reading_digest': basis['reading_digest'],
            'evidence_digest': basis['evidence_digest'],
            'source_digest': basis['source_digest'],
            'rule_versions': json.loads(basis['rule_versions']),
            'basis': {k: basis[k] for k in (
                'batch_id', 'site', 'generation_slot', 'self_use_slot', 'export_slot',
                'generation_usage', 'self_use_usage', 'export_usage', 'generation_status',
                'self_use_status', 'export_status', 'eligible_units')},
        },
    }


def ledger(db_path, batch_id=None):
    """返回完整可审计台账，包含撤销/召回/结转历史。"""
    db = connect(db_path)
    try:
        where = ' WHERE batch_id=?' if batch_id else ''
        args = (batch_id,) if batch_id else ()
        issuances = _rows(db, 'SELECT * FROM attribute_issuance' + where + ' ORDER BY id', args)
        certificates = _rows(db, 'SELECT * FROM attribute_certificate' + where +
                             ' ORDER BY issuance_id,serial', args)
        bases = _rows(db, 'SELECT id,fingerprint,contract_id,batch_id,site,period_start,period_end,'
                          'generation_usage,self_use_usage,export_usage,generation_status,'
                          'self_use_status,export_status,eligible_units,decision_digest,'
                          'reading_digest,evidence_digest,source_digest,evidence_fingerprint,'
                          'rule_versions,unit_per_certificate,created_at '
                          'FROM attribute_basis' + where + ' ORDER BY id', args)
        contracts = _rows(db, 'SELECT id,sha256,contract_file,config_sha256,title_mode,holder,'
                              'unit_per_certificate FROM attribute_contract ORDER BY id')
        recalls = _rows(db, 'SELECT * FROM attribute_recall' + where + ' ORDER BY id', args)
        carry = _rows(db, 'SELECT * FROM attribute_carry' + where + ' ORDER BY id', args)
        return {'issuances': issuances, 'bases': bases, 'contracts': contracts,
                'certificates': [_certificate_view(db, c) for c in certificates],
                'recalls': recalls, 'carry': carry}
    finally:
        db.close()


def summary(db_path, batch_id=None):
    """可转让有效凭证摘要：ID、数量、持有人与证据来源。

    已转让但所属发行仍然有效的凭证也属于可转让资产，按当前持有人列出。
    """
    data = ledger(db_path, batch_id)
    active_issuances = [i for i in data['issuances'] if i['status'] in ('有效', '待生效')]
    active_by_id = {i['id']: i for i in active_issuances}
    basis_by_id = {b['id']: b for b in data['bases']}
    basis_by_fingerprint = {b['fingerprint']: b for b in data['bases']}
    valid = [c for c in data['certificates']
             if c['issuance_id'] in active_by_id and c['status'] in ('有效', '已转让')]
    recalls = [r for r in data['recalls'] if r['status'] == '待召回']
    certificates = []
    for cert in valid:
        basis_row = basis_by_fingerprint.get(cert['basis_fingerprint'])
        certificates.append({
            'certificate_id': cert['certificate_id'],
            'batch_id': cert['batch_id'],
            'site': cert['site'],
            'period_start': cert['period_start'],
            'period_end': cert['period_end'],
            'holder': cert['current_holder'],
            'origin_holder': cert['origin_holder'],
            'energy_share': cert['energy_share'],
            'serial': cert['serial'],
            'title_mode': cert['title_mode'],
            'contract_sha256': cert['contract_sha256'],
            'basis_fingerprint': cert['basis_fingerprint'],
            'source_digest': basis_row['source_digest'] if basis_row else None,
            'decision_digest': basis_row['decision_digest'] if basis_row else None,
            'reading_digest': basis_row['reading_digest'] if basis_row else None,
            'evidence_digest': basis_row['evidence_digest'] if basis_row else None,
            'rule_versions': json.loads(basis_row['rule_versions']) if basis_row else None,
            'status': cert['status'],
        })
    issuance_rows = []
    for issuance in active_issuances:
        basis = basis_by_id.get(issuance['basis_id'])
        issuance_rows.append({
            'issuance_id': issuance['id'],
            'basis_id': issuance['basis_id'],
            'batch_id': issuance['batch_id'],
            'site': issuance['site'],
            'title_mode': issuance['title_mode'],
            'holder': issuance['holder'],
            'status': issuance['status'],
            'certificate_count': issuance['certificate_count'],
            'eligible_units': issuance['eligible_units'],
            'issued_units': issuance['issued_units'],
            'period_remainder_units': issuance['period_remainder_units'],
            'opening_carry_units': issuance['opening_carry_units'],
            'closing_carry_units': issuance['closing_carry_units'],
            'supersedes_id': issuance['supersedes_id'],
            'source': None if basis is None else {
                'basis_fingerprint': basis['fingerprint'],
                'decision_digest': basis['decision_digest'],
                'reading_digest': basis['reading_digest'],
                'evidence_digest': basis['evidence_digest'],
                'source_digest': basis['source_digest'],
                'rule_versions': json.loads(basis['rule_versions']),
                'usage': {'generation': basis['generation_usage'],
                          'self_use': basis['self_use_usage'],
                          'export': basis['export_usage']},
                'eligible_units': basis['eligible_units'],
            },
        })
    return {
        'batch_id': batch_id,
        'issuances': issuance_rows,
        'certificate_count': len(certificates),
        'certificates': certificates,
        'pending_recalls': [{'recall_id': r['id'], 'certificate_id': r['certificate_id'],
                             'holder': r['holder'], 'reason': r['reason']} for r in recalls],
        'carry': [{k: c[k] for k in (
            'id', 'issuance_id', 'batch_id', 'site', 'title_mode', 'holder', 'opening_units',
            'eligible_units', 'period_remainder_units', 'closing_units', 'status',
            'carried_from_id', 'closed_by_issuance_id')} for c in data['carry']],
    }

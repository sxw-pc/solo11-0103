import hashlib
import itertools
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from .store import connect

# 对外只发布这一项计数指标。它描述的是“被发布表位集合中由 H2 类手持终端
# 采录的表位数”，不是用量、账单或任何逐表位结算结果。
DISCLOSURE_METRIC='H2_terminal_slot_count'
DISCLOSURE_VERSION='1'
PUBLISHED_STATUSES=('legacy','pending','published')
_POLICIES=('deny_unsafe','minimum_safe_coarsening')
# 枚举所有二元世界是 2^表位数；超过上限则安全失败，绝不退化成启发式判断。
_MAX_ENUM_SLOTS=16
# 每次发布的安全判定总预算（世界数 × 组合试验数）；超预算即拒绝。
_MAX_ANALYSIS_WORK=20_000_000
_MAX_EXPANSION_TRIALS=4096


# ---------------------------------------------------------------- 内部完整报告
def _internal_data(db):
    data={table:[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in ('batch','slot','assignment','multiplier','replacement','reading','evidence','issue','decision','run','disclosure_publication','disclosure_refusal')}
    data['source']=[dict(r) for r in db.execute('SELECT id,name,sha256,length(raw) AS byte_length FROM source ORDER BY id')]
    return data


def report(db_path,out):
    """独立授权审计使用的内部完整证据报告（含设备号、原始行与来源名）。

    这不是外部交付物；外部承包方只能通过 publish_disclosures 取得汇总。
    """
    db=connect(db_path)
    data=_internal_data(db)
    path=Path(out);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 抄表结算依据','',*['- '+r['slot']+'：'+r['status']+'；用量 '+str(r['usage']) for r in data['decision']],'','问题：']
    lines += ['- '+r['code']+' '+r['subject'] for r in data['issue']]
    path.with_suffix('.md').write_text('\n'.join(lines),encoding='utf-8')
    db.close();return path


def _slot_h2(db):
    """从内部原始采录记录归纳每个表位是否使用 H2 类终端。

    混合终端或未知前缀返回 None；此类表位不能进入任何外部发布。
    """
    kinds={}
    for row in db.execute('SELECT id FROM slot'):
        kinds[row['id']]=set()
    for row in db.execute('SELECT slot,device FROM reading'):
        kind=None
        if row['device'].startswith('H2-'):kind=1
        elif row['device'].startswith('H1-'):kind=0
        kinds.setdefault(row['slot'],set()).add(kind)
    return {slot:(next(iter(ks)) if len(ks)==1 and None not in ks else None) for slot,ks in kinds.items()}


def _count(slots,h2):return sum(int(h2[s]) for s in slots)


# ------------------------------------------------- 联立等式与单表位可识别性
def _analyse_equations(equations,universe,h2):
    """联立全部可见群组等式，枚举与二元终端约束相符的世界。

    返回 revealed（被唯一推出的表位）、consistent（真实世界是否满足等式）
    与 worlds（相符世界数）。任何一组之外的重叠、全零或全一都会被枚举覆盖。
    """
    universe=list(dict.fromkeys(universe))
    if len(universe)>_MAX_ENUM_SLOTS:raise ValueError('universe_too_large')
    index={slot:i for i,slot in enumerate(universe)}
    eqs=[]
    for equation in equations:
        slots=list(equation['slots'] if isinstance(equation,dict) else equation[0])
        count=equation['count'] if isinstance(equation,dict) else equation[1]
        if any(s not in index for s in slots):raise ValueError('equation_outside_universe')
        eqs.append(([index[s] for s in slots],int(count)))
    pads=[1<<i for i in range(len(universe))]
    true_mask=sum(pads[index[s]] for s in universe if h2.get(s)==1)
    worlds=[]
    for mask in range(1<<len(universe)):
        if all(sum((mask>>i)&1 for i in bits)==count for bits,count in eqs):
            worlds.append(mask)
    revealed=[slot for i,slot in enumerate(universe) if len({(mask>>i)&1 for mask in worlds})==1]
    return {'revealed':revealed,'consistent':true_mask in worlds,'worlds':len(worlds)}


def uniquely_revealed_slots(equations,universe,h2):
    """公开的判定辅助：给定受众可见的全部群组，返回被唯一推出的表位。"""
    if any(h2.get(s) is None for s in universe):raise ValueError('unknown_terminal_type')
    result=_analyse_equations(equations,universe,h2)
    if not result['consistent']:raise ValueError('inconsistent_equations')
    return result['revealed']


# ---------------------------------------------------------------- 交付字节
def _external_content(publication_id,batch,slots,count):
    """外部文件只允许出现：发布 ID、批次、版本、实际表位集合、H2 覆盖数。"""
    payload={'batch':batch,'h2_terminal_slot_count':int(count),'publication_id':publication_id,'slots':list(slots),'version':DISCLOSURE_VERSION}
    return json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n'


def _digest(data):return hashlib.sha256(data).hexdigest()


def _atomic_write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        os.replace(tmp,path)
    except BaseException:
        try:os.unlink(tmp)
        except FileNotFoundError:pass
        raise


def _materialize(db,row):
    """把台账中已提交的 content 落成文件，再核验字节摘要。

    pending 行在文件写入前就已提交，因此中断后总能凭台账重放；已存在但
    内容不符的文件会被台账字节覆盖，保证交付字节与记账摘要一致。
    """
    if row['status']=='legacy':return 'legacy'
    data=row['content'].encode('utf-8')
    digest=_digest(data)
    if digest!=row['file_sha256']:raise ValueError('ledger_digest_mismatch')
    path=Path(row['file_path'])
    action='ok'
    current=_digest(path.read_bytes()) if path.is_file() else None
    if current!=digest:
        _atomic_write(path,data);action='restored'
    if _digest(path.read_bytes())!=digest:raise ValueError('delivery_digest_mismatch')
    if row['status']=='pending':
        db.execute("UPDATE disclosure_publication SET status='published',published_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",(row['id'],))
        db.commit();action='published'
    return action


def _batch_for(db,slots):
    batches=sorted({row['batch_id'] for row in db.execute('SELECT batch_id FROM slot WHERE id IN (%s)'%','.join('?' for _ in slots),slots)}) if slots else []
    if not batches:raise ValueError('unknown_slot')
    return batches[0] if len(batches)==1 else 'mixed'


def _visible_equations(db,audience):
    rows=db.execute('SELECT publication_id,slots,h2_count FROM disclosure_publication WHERE audience=? AND status IN (?,?,?) ORDER BY id',(audience,)+PUBLISHED_STATUSES)
    return [{'id':row['publication_id'],'slots':json.loads(row['slots']),'count':row['h2_count']} for row in rows]


def _normalise_requests(data):
    if not isinstance(data,dict):raise ValueError('invalid_requests')
    audience=str(data['audience']);universe=[str(s) for s in data['universe']]
    if not audience or not universe or len(set(universe))!=len(universe):raise ValueError('invalid_requests')
    allowed=[str(s) for s in data.get('allowed_expansion',[])]
    if any(s not in universe for s in allowed):raise ValueError('expansion_outside_universe')
    def clean(items):
        out=[]
        for item in items:
            if not item:continue
            out.append({'id':str(item['id']),'slots':[str(s) for s in item['slots']],**({'count':int(item['count'])} if 'count' in item else {})})
        return out
    seeds=clean([data.get('previous_release')])+clean(data.get('previous_releases') or [])
    requests=clean([data.get('new_request')])+clean(data.get('requests') or [])
    policies=data.get('policies') or list(_POLICIES)
    if any(p not in _POLICIES for p in policies):raise ValueError('unknown_policy')
    for item in seeds+requests:
        if not item['id'] or not item['slots']:raise ValueError('invalid_request')
        if len(set(item['slots']))!=len(item['slots']):raise ValueError('duplicate_slot')
    return {'audience':audience,'universe':universe,'allowed_expansion':allowed,'policies':policies,'seeds':seeds,'requests':requests}


def _seed_legacy(db,spec):
    """历史已发布集合也要独立落账。fixture 未带原始字节时，用规范化摘要记账。"""
    h2=_slot_h2(db)
    for release in spec['seeds']:
        slots=list(release['slots'])
        if any(s not in spec['universe'] for s in slots):raise ValueError('seed_outside_universe')
        if any(h2.get(s) is None for s in slots):raise ValueError('unknown_terminal_type')
        count=int(release.get('count',_count(slots,h2)))
        if count!=_count(slots,h2):raise ValueError('seed_count_mismatch')
        content=_external_content(release['id'],_batch_for(db,slots),slots,count)
        db.execute('INSERT OR IGNORE INTO disclosure_publication(audience,request_id,publication_id,policy,batch_id,version,slots,h2_count,file_path,file_sha256,content,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)',(spec['audience'],release['id'],release['id'],'legacy',_batch_for(db,slots),DISCLOSURE_VERSION,json.dumps(slots,ensure_ascii=False),count,'',_digest(content.encode('utf-8')),content,'legacy'))
    db.commit()


def _safe(history,proposed,spec,h2):
    """联立该受众全部可见等式，判断拟发布是否新增可识别表位。

    历史等式已暴露的表位属于受众既有知识，不构成本次新增泄露；只有
    “发布后唯一可推、发布前不唯一”的表位才是否决理由。
    """
    equations=list(history)+[proposed]
    touched={s for equation in equations for s in (equation['slots'] if isinstance(equation,dict) else equation[0])}
    if any(s not in h2 or h2.get(s) is None for s in touched):
        return {'safe':False,'reason':'unknown_terminal_type','detail':'','revealed':[]}
    try:
        before=_analyse_equations(history,spec['universe'],h2)
        after=_analyse_equations(equations,spec['universe'],h2)
    except ValueError as error:
        return {'safe':False,'reason':'unsupported_history','detail':str(error),'revealed':[]}
    newly=[s for s in after['revealed'] if s not in before['revealed']]
    return {'safe':after['consistent'] and not newly,
            'reason':'inference' if newly else ('' if after['consistent'] else 'inconsistent_history'),
            'revealed':newly,'worlds':after['worlds']}


def _plan(db,spec,request,h2,policy):
    slots=list(request['slots'])
    if any(s not in spec['universe'] for s in slots):
        return {'status':'refused','reason':'slot_outside_universe'}
    if any(h2.get(s) is None for s in slots):
        return {'status':'refused','reason':'unknown_terminal_type'}
    history=_visible_equations(db,spec['audience'])
    target={'id':request['id'],'slots':slots,'count':_count(slots,h2)}
    check=_safe(history,target,spec,h2)
    if check['safe']:
        return {'status':'publish','slots':slots,'count':target['count'],'expanded':False}
    if policy=='deny_unsafe':
        return {'status':'refused','reason':'inference','revealed':check['revealed'],'detail':check.get('detail','')}
    # 扩展是替换请求等式，而不是在历史之外追加等式，所以“加入更多表位”
    # 未必更安全：某个候选单独加入会暴露，但多个候选一起加入可能互相抵消。
    # 因此按加入数量从小到大枚举全部候选组合，取第一个整体安全的方案。
    additions=[s for s in spec['allowed_expansion'] if s not in slots and h2.get(s) is not None]
    trials=0;budget=max(1,_MAX_ANALYSIS_WORK//max(1,1<<len(spec['universe'])))
    for size in range(1,len(additions)+1):
        for extra in itertools.combinations(additions,size):
            trials+=1
            if trials>min(_MAX_EXPANSION_TRIALS,budget):
                return {'status':'refused','reason':'expansion_limit','revealed':check['revealed'],'detail':check.get('detail','')}
            candidate=slots+list(extra)
            proposed={'id':request['id'],'slots':candidate,'count':_count(candidate,h2)}
            trial=_safe(history,proposed,spec,h2)
            if trial['safe']:
                return {'status':'publish','slots':candidate,'count':proposed['count'],'expanded':True,'added':list(extra)}
    return {'status':'refused','reason':'no_safe_expansion','revealed':check['revealed'],'detail':check.get('detail','')}


def _publication_result(row,status='published'):
    slots=json.loads(row['slots'])
    return {'request_id':row['request_id'],'status':status,'policy':row['policy'],'publication_id':row['publication_id'],'slots':slots,'h2_terminal_slot_count':row['h2_count'],'file':row['file_path'] or None,'sha256':row['file_sha256']}


def _refusal_result(row,status='refused'):
    return {'request_id':row['request_id'],'status':status,'policy':row['policy'],'reason':row['reason']}


def _publish_one(db,spec,request,outdir,policy):
    # 显式事务把“读历史—判定—写台账”串成一段，同受众并发请求因此只能
    # 逐个核验，后来的请求会看到前一份已落账的等式。
    if db.in_transaction:db.commit()
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT * FROM disclosure_publication WHERE audience=? AND request_id=? ORDER BY id LIMIT 1',(spec['audience'],request['id'])).fetchone()
        if row:
            db.commit()
            if row['status']=='pending':_materialize(db,row)
            result=_publication_result(row,'already_published')
            added=[s for s in json.loads(row['slots']) if s not in request['slots']]
            if added:result.update(expanded=True,added=added)
            return result
        refusal=db.execute('SELECT * FROM disclosure_refusal WHERE audience=? AND request_id=? AND policy=?',(spec['audience'],request['id'],policy)).fetchone()
        if refusal:
            db.commit();return _refusal_result(refusal,'refused')
        h2=_slot_h2(db)
        plan=_plan(db,spec,request,h2,policy)
        if plan['status']=='refused':
            db.execute('INSERT INTO disclosure_refusal(audience,request_id,policy,reason,detail,requested_slots,created_at) VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)',(spec['audience'],request['id'],policy,plan['reason'],json.dumps(plan,ensure_ascii=False,sort_keys=True),json.dumps(list(request['slots']),ensure_ascii=False)))
            db.commit()
            return {'request_id':request['id'],'status':'refused','policy':policy,'reason':plan['reason']}
        publication_id='PUB-'+os.urandom(8).hex()
        path=Path(outdir)/f'{publication_id}.json'
        content=_external_content(publication_id,_batch_for(db,plan['slots']),plan['slots'],plan['count'])
        digest=_digest(content.encode('utf-8'))
        db.execute('INSERT INTO disclosure_publication(audience,request_id,publication_id,policy,batch_id,version,slots,h2_count,file_path,file_sha256,content,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)',(spec['audience'],request['id'],publication_id,policy,_batch_for(db,plan['slots']),DISCLOSURE_VERSION,json.dumps(plan['slots'],ensure_ascii=False),plan['count'],str(path),digest,content,'pending'))
        db.commit()
    except BaseException:
        db.rollback();raise
    row=db.execute('SELECT * FROM disclosure_publication WHERE publication_id=?',(publication_id,)).fetchone()
    _materialize(db,row)
    result=_publication_result(row,'published')
    if plan['expanded']:result.update(expanded=True,added=plan['added'])
    return result


def _reconcile(db,outdir=None):
    actions=[]
    rows=db.execute("SELECT * FROM disclosure_publication WHERE status IN ('pending','published') ORDER BY id").fetchall()
    for row in rows:
        if outdir and not row['file_path']:
            # 只有需要交付的行才绑定路径；legacy 行只做历史记账。
            candidate=str(Path(outdir)/f"{row['publication_id']}.json")
            db.execute('UPDATE disclosure_publication SET file_path=? WHERE id=?',(candidate,row['id']))
            db.commit();row=db.execute('SELECT * FROM disclosure_publication WHERE id=?',(row['id'],)).fetchone()
        actions.append({'publication_id':row['publication_id'],'action':_materialize(db,row)})
    return actions


def _copy_database(db_path,dest):
    source=connect(db_path);target=sqlite3.connect(dest)
    try:source.backup(target)
    finally:target.close();source.close()


def publish_disclosures(db_path,requests_path,outdir,policy='minimum_safe_coarsening',dry_run=False):
    """只发布外部安全汇总；拒绝只进内部台账，不产生任何交付文件。"""
    if policy not in _POLICIES:raise ValueError('unknown_policy')
    spec=_normalise_requests(json.loads(Path(requests_path).read_text(encoding='utf-8')))
    if policy not in spec['policies']:raise ValueError('policy_not_allowed')
    outdir=Path(outdir)
    if dry_run:
        with tempfile.TemporaryDirectory(prefix='meterround-dry-') as temp:
            copy=Path(temp)/'meterround.db';_copy_database(db_path,copy)
            result=_run_publish(copy,spec,Path(temp)/'out',policy)
            result['dry_run']=True
            return result
    outdir.mkdir(parents=True,exist_ok=True)
    result=_run_publish(db_path,spec,outdir,policy)
    result['dry_run']=False
    return result


def _run_publish(db_path,spec,outdir,policy):
    db=connect(db_path)
    try:
        _seed_legacy(db,spec)
        _reconcile(db,outdir)
        results=[]
        for request in spec['requests']:
            results.append(_publish_one(db,spec,request,outdir,policy))
        return {'audience':spec['audience'],'policy':policy,'results':results}
    finally:
        db.close()


def recover_disclosures(db_path,outdir=None):
    """中断恢复：重放 pending，并核验/修复已发布文件与台账字节的一致性。"""
    db=connect(db_path)
    try:
        return {'recovered':_reconcile(db,outdir)}
    finally:
        db.close()


def export_external_bundle(db_path,out,publication_id,audience=None):
    """按台账字节重新交付指定发布文件，摘要不符则拒绝。

    legacy 行只记录历史集合、计数与摘要，没有可复核的原始交付字节，
    因此不参与重放，避免凭空生成一份“历史文件”。发布 ID 在受众内唯一，
    跨受众重名时须显式给出 audience。
    """
    db=connect(db_path)
    try:
        rows=db.execute('SELECT * FROM disclosure_publication WHERE publication_id=? AND status IN (?,?) ORDER BY id',(publication_id,'pending','published')).fetchall()
        if audience is not None:rows=[r for r in rows if r['audience']==audience]
        if not rows:raise ValueError('unknown_publication')
        if len(rows)>1:raise ValueError('ambiguous_publication')
        row=rows[0]
        data=row['content'].encode('utf-8');digest=_digest(data)
        if digest!=row['file_sha256']:raise ValueError('ledger_digest_mismatch')
        _atomic_write(out,data)
        if _digest(Path(out).read_bytes())!=digest:raise ValueError('delivery_digest_mismatch')
        return {'publication_id':publication_id,'audience':row['audience'],'path':str(out),'sha256':digest,'bytes':len(data)}
    finally:
        db.close()

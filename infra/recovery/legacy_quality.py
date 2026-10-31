"""旧专家记录的保守迁移分类，不自动导入统计，不读取旧模型数据源。"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid

from services.api.app.settlement import settle
from .bundle import RecoveryError, audit_inventory, canonical, isolated_connection


MAX_LEGACY_RECORDS = 10000
MAX_LEGACY_BYTES = 16 * 1024 * 1024


def _version(record):
    revision = record.get('revision')
    version_key = record.get('version_key')
    if revision is not None and (type(revision) is not int or revision < 1):
        return None
    if version_key is not None and (not isinstance(version_key, str) or not re.fullmatch('[a-f0-9]{64}', version_key)):
        return None
    return ('revision', revision) if revision is not None else ('hash', version_key) if version_key else ('unversioned', None)


def _time(value):
    try:
        result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError,TypeError):
        return None


def _number(value):
    try:
        if isinstance(value,bool):
            return None
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _line(document:dict,kind:str):
    # Missing is not an explicit no-line value. Only a proven winner market
    # accepts explicit NULL and legacy numeric zero as the same identity.
    if 'line' not in document:
        return None
    raw=document['line']
    number=_number(raw)
    if kind=='winner':
        return Decimal('0') if raw is None or number==0 else None
    return number if kind in {'handicap','total'} else None


def classify(record:dict)->dict:
    result={'legacy_id':str(record.get('legacy_id') or ''),'classification':'insufficient_evidence','eligible_for_statistics':False,
            'reasons':[],'preserved':{},'source_evidence_sha256':hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()}
    if record.get('origin_kind')!='telegram_expert':
        result.update(classification='excluded_nonexpert',reasons=['origin_is_not_proven_telegram_expert'])
        return result
    stable=('legacy_id','chat_id','author_id','platform_message_id')
    missing=[field for field in stable if not isinstance(record.get(field),(str,int)) or isinstance(record.get(field),bool) or not str(record[field]).strip()]
    if missing:
        result['reasons']=['missing_stable_'+field for field in missing]
        return result
    result['preserved']={field:str(record[field]) for field in stable}
    result['preserved'].update({key:record.get(key) for key in ('author_nickname','revision','version_key','raw_text','media_objects','posted_at','recommended_at','fixture_id','market_id','period','line','selection','kind','phase','score_basis','score_baseline','system_quote','official_result','settlement')})
    reasons=[]
    if _version(record) is None:
        reasons.append('invalid_original_message_version')
    if not record.get('raw_text') and not record.get('media_objects'):
        reasons.append('original_message_unavailable')
    posted,recommended=_time(record.get('posted_at')),_time(record.get('recommended_at'))
    if not posted or not recommended or recommended<posted:
        reasons.append('message_or_recommendation_time_unproven')
    market_fields=('fixture_id','market_id','period','selection','kind','phase')
    kind=record.get('kind')
    line=_line(record,kind)
    if any(not record.get(field) for field in market_fields) or line is None:
        reasons.append('exact_system_market_binding_missing')
    if (record.get('period') not in {'full_time','first_half','second_half'} or record.get('selection') not in {'home','away','draw','over','under'}
            or record.get('kind') not in {'winner','handicap','total'} or record.get('phase') not in {'scheduled','live'}):
        reasons.append('unsupported_period_or_selection')
    quote=record.get('system_quote') if isinstance(record.get('system_quote'),dict) else {}
    quote_time=_time(quote.get('source_updated_at'))
    price=_number(quote.get('price'))
    if quote.get('source')!='leyu' or quote.get('evidence_kind')!='system_captured' or not price or price<=1:
        reasons.append('system_price_evidence_missing')
    # Older exported quotes inherit their exact bound record's kind; an
    # explicitly contradictory kind is never repaired or ignored.
    quote_kind=quote.get('kind',quote.get('market_kind',kind))
    quote_line=_line(quote,quote_kind)
    if (any(quote.get(field)!=record.get(field) for field in ('fixture_id','market_id','period','selection'))
            or quote_kind!=kind or quote_line is None or quote_line!=line):
        reasons.append('system_price_belongs_to_different_market')
    if not recommended or not quote_time or not 0<=(recommended-quote_time).total_seconds()<=60:
        reasons.append('recommendation_quote_time_unproven')
    result['reasons']=reasons
    if reasons:
        result['classification']='recoverable' if record.get('evidence_locator') else 'insufficient_evidence'
        return result
    result['classification']='complete'
    official=record.get('official_result') if isinstance(record.get('official_result'),dict) else {}
    settlement=record.get('settlement') if isinstance(record.get('settlement'),dict) else {}
    if (official.get('status')=='confirmed' and official.get('fixture_id')==record['fixture_id'] and official.get('provider_reference')
            and official.get('content_hash') and _time(official.get('source_updated_at'))):
        net=_number(settlement.get('unit_net'))
        calculated=settle({**record,'price':price},official)
        result['eligible_for_statistics']=(net is not None and calculated.unit_net is not None and net==calculated.unit_net
            and calculated.outcome==settlement.get('outcome') and settlement.get('result_reference')==official['provider_reference'])
    if not result['eligible_for_statistics']:
        result['reasons']=['official_result_or_reproducible_settlement_missing']
    return result


def plan(records:list[dict])->dict:
    classifications=[]
    duplicates=set()
    seen=set()
    for record in records:
        if not isinstance(record,dict):
            raise ValueError('legacy_records_must_be_objects')
        item=classify(record)
        key=identity(item,record)
        if item['classification']!='excluded_nonexpert' and key in seen:
            item.update(classification='insufficient_evidence',eligible_for_statistics=False,reasons=['duplicate_recommendation_identity'])
            duplicates.add(key)
        if item['classification']!='excluded_nonexpert':
            seen.add(key)
        classifications.append(item)
    # 同一标识冲突时双向隔离，不能挑第一条作为权威。
    for item,record in zip(classifications,records):
        key=identity(item,record)
        if item['classification']!='excluded_nonexpert' and key in duplicates:
            item.update(classification='insufficient_evidence',eligible_for_statistics=False,reasons=['duplicate_recommendation_identity'])
    # A Telegram message has one stable author. A renamed author keeps its ID;
    # two authors for the same source/message are contradictory provenance.
    messages = {}
    for index, (item, record) in enumerate(zip(classifications, records)):
        if item['classification'] != 'excluded_nonexpert' and item['preserved']:
            key = tuple(item['preserved'][field] for field in ('chat_id', 'platform_message_id'))
            messages.setdefault(key, []).append(index)
    for indices in messages.values():
        authors = {classifications[index]['preserved']['author_id'] for index in indices}
        versions = {_version(records[index]) for index in indices}
        content_versions = {}
        for index in indices:
            content = {key: records[index].get(key) for key in ('raw_text', 'media_objects', 'posted_at', 'version_key')}
            content_versions.setdefault(_version(records[index]), set()).add(hashlib.sha256(canonical(content)).hexdigest())
        reason = None
        if len(authors) != 1:
            reason = 'conflicting_original_message_author'
        elif any(len(contents) > 1 for contents in content_versions.values()):
            reason = 'conflicting_original_message_version_content'
        elif len(versions) > 1 and any(value is None or value[0] != 'revision' for value in versions):
            reason = 'original_message_versions_cannot_be_ordered'
        if reason:
            for index in indices:
                classifications[index].update(classification='insufficient_evidence',eligible_for_statistics=False,reasons=[reason])
        elif len(versions) > 1:
            latest = max(value[1] for value in versions)
            for index in indices:
                if _version(records[index])[1] != latest:
                    classifications[index]['eligible_for_statistics'] = False
                    classifications[index]['reasons'] = sorted(set(classifications[index]['reasons'] + ['superseded_original_message_version']))
    categories=('complete','recoverable','insufficient_evidence','excluded_nonexpert')
    return {'total':len(records),'counts':{name:sum(item['classification']==name for item in classifications) for name in categories},
            'statistics_eligible':sum(item['eligible_for_statistics'] for item in classifications),'records':classifications,
            'import_performed':False,'deletion_performed':False}


def identity(item,record):
    line=_line(record,record.get('kind'))
    normalized_line=str(line.normalize()) if line is not None else ''
    return tuple(item['preserved'].get(field) for field in ('chat_id','author_id','platform_message_id'))+(_version(record),)+tuple(
        str(record.get(field)) for field in ('fixture_id','market_id','period','kind'))+(normalized_line,str(record.get('selection')))


def _summary(rows):
    categories = ('complete', 'recoverable', 'insufficient_evidence', 'excluded_nonexpert')
    experts = [row for row in rows if row['classification'] != 'excluded_nonexpert']
    unique_messages = {row['message_identity'] for row in experts if row['message_identity'] is not None}
    return {'rows': len(rows), 'expert_records': len(experts), 'message_versions': len(unique_messages),
            'pending_records': sum(row['reported_outcome'] == 'pending' for row in experts),
            'classification': {key: sum(row['classification'] == key for row in rows) for key in categories},
            'statistics_eligible': sum(row['eligible'] for row in rows),
            'reported_unit_net_unknown_records': sum(row['reported_net'] is None for row in experts),
            'unresolved_expert_records': sum(not row['eligible'] for row in experts),
            'reported_expert_unit_net': str(sum((row['reported_net'] or Decimal(0) for row in experts), Decimal(0))),
            'verified_expert_unit_net': str(sum((row['verified_net'] or Decimal(0) for row in rows), Decimal(0)))}


def _stage_rows(records, report):
    rows = []
    for index, (record, item) in enumerate(zip(records, report['records'])):
        preserved = item['preserved']
        message_identity = None
        author_identity = None
        if preserved:
            author_identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'telegram:author:' + preserved['author_id']))
            message_identity = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical([
                'telegram:message', preserved['chat_id'], preserved['platform_message_id'], _version(record)]).decode()))
        settlement = record.get('settlement') if isinstance(record.get('settlement'), dict) else {}
        net = _number(settlement.get('unit_net'))
        rows.append({'ordinal': index, 'source_record': record,
                     'source_sha256': item['source_evidence_sha256'], 'classification': item['classification'],
                     'author_identity': author_identity, 'message_identity': message_identity,
                     'eligible': item['eligible_for_statistics'], 'reported_outcome': settlement.get('outcome') or 'pending',
                     'reported_net': net, 'verified_net': net if item['eligible_for_statistics'] else None,
                     'reasons': item['reasons']})
    return rows


async def _business_snapshot(conn):
    tables = await conn.fetch("SELECT tablename AS name FROM pg_tables WHERE schemaname='public' "
                              "AND tablename LIKE 'v2\\_%' ESCAPE '\\' ORDER BY tablename")
    result = {}
    total_rows = total_bytes = 0
    for table in tables:
        name = table['name']
        if not re.fullmatch('v2_[a-z0-9_]+', name):
            raise RecoveryError('legacy_unapproved_business_table')
        hashes = []
        async for row in conn.cursor('SELECT to_jsonb(t) AS value FROM public."' + name + '" t'):
            value = row['value']
            value = json.loads(value) if isinstance(value, str) else value
            raw = canonical(value)
            total_rows += 1
            total_bytes += len(raw)
            if total_rows > 100000 or total_bytes > 64*1024*1024:
                raise RecoveryError('legacy_full_snapshot_budget_exceeded')
            hashes.append(hashlib.sha256(raw).hexdigest())
        result[name] = {'rows': len(hashes), 'sha256': hashlib.sha256(canonical(sorted(hashes))).hexdigest()}
    return {'inventory': await audit_inventory(conn), 'table_hashes': result,
            'order_currencies': [dict(row) for row in await conn.fetch(
                "SELECT COALESCE(provider_currency,'unknown') AS currency,count(*) AS orders,"
                "sum(stake)::text AS requested_stake,sum(reserved_stake)::text AS reserved_stake,"
                "sum(provider_stake)::text AS official_stake,sum(settled_net)::text AS reported_settled_net,"
                "sum(provider_fees)::text AS known_fees,count(*) FILTER(WHERE provider_fees IS NULL) AS unknown_fee_orders "
                "FROM v2_bet_orders GROUP BY provider_currency ORDER BY currency")],
            'accounts': dict(await conn.fetchrow(
                'SELECT count(*) AS accounts,count(*) FILTER(WHERE available_balance IS NULL) AS unknown_balances,'
                'sum(available_balance)::text AS known_balance_total FROM v2_leyu_accounts'))}


async def rehearse_migration(conn, records):
    """Stage only into a transaction-local PG table; never import business rows.

    Explicit recovery role/database is mandatory even for this temporary write.
    Every input record is retained exactly, including rejected/nonexpert rows.
    """
    await isolated_connection(conn)
    if (not isinstance(records, list) or len(records) > MAX_LEGACY_RECORDS
            or any(not isinstance(record, dict) for record in records)):
        raise RecoveryError('legacy_migration_record_budget_exceeded')
    try:
        raw = canonical(records)
    except (TypeError, ValueError):
        raise RecoveryError('legacy_migration_requires_json_records') from None
    if len(raw) > MAX_LEGACY_BYTES:
        raise RecoveryError('legacy_migration_byte_budget_exceeded')
    report = plan(records)
    rows = _stage_rows(records, report)
    expected = _summary(rows)
    async with conn.transaction(isolation='repeatable_read'):
        await conn.execute("SELECT set_config('statement_timeout','5000',true),set_config('lock_timeout','2000',true)")
        before = await _business_snapshot(conn)
        await conn.execute('CREATE TEMPORARY TABLE legacy_expert_rehearsal('
            'ordinal integer PRIMARY KEY,source_record jsonb NOT NULL,source_sha256 text NOT NULL,'
            'classification text NOT NULL,author_identity text,message_identity text,eligible boolean NOT NULL,'
            'reported_outcome text NOT NULL,reported_net numeric,verified_net numeric,reasons jsonb NOT NULL) ON COMMIT DROP')
        if rows:
            batch = [{**row, 'reported_net': str(row['reported_net']) if row['reported_net'] is not None else None,
                      'verified_net': str(row['verified_net']) if row['verified_net'] is not None else None} for row in rows]
            await conn.execute('INSERT INTO pg_temp.legacy_expert_rehearsal '
                "SELECT (r->>'ordinal')::integer,r->'source_record',r->>'source_sha256',r->>'classification',"
                "r->>'author_identity',r->>'message_identity',(r->>'eligible')::boolean,r->>'reported_outcome',"
                "(r->>'reported_net')::numeric,(r->>'verified_net')::numeric,r->'reasons' "
                'FROM jsonb_array_elements($1::text::jsonb) r', json.dumps(batch, ensure_ascii=False))
        actual = []
        for row in await conn.fetch('SELECT * FROM pg_temp.legacy_expert_rehearsal ORDER BY ordinal'):
            item = dict(row)
            source = json.loads(item['source_record']) if isinstance(item['source_record'], str) else item['source_record']
            if source != records[item['ordinal']] or hashlib.sha256(canonical(source)).hexdigest() != item['source_sha256']:
                raise RecoveryError('legacy_original_evidence_differs')
            expected_row = rows[item['ordinal']]
            reasons = json.loads(item['reasons']) if isinstance(item['reasons'], str) else item['reasons']
            if (any(item[key] != expected_row[key] for key in (
                    'author_identity','message_identity','classification','eligible','reported_outcome','reported_net','verified_net'))
                    or reasons != expected_row['reasons']):
                raise RecoveryError('legacy_staging_inventory_differs')
            actual.append(item)
        observed = _summary(actual)
        if observed != expected:
            raise RecoveryError('legacy_staging_inventory_differs')
        after = await _business_snapshot(conn)
        if before != after:
            raise RecoveryError('legacy_business_or_financial_data_changed')
    excluded = sum(row['classification'] == 'excluded_nonexpert' for row in rows)
    return {'mode': 'isolated_transaction_local_staging', 'source_sha256': hashlib.sha256(raw).hexdigest(),
            'input_records': len(records), 'source_summary': expected, 'staged_summary': observed,
            'classification_counts': report['counts'], 'business_before': before, 'business_after': after,
            'staging_differences': {}, 'business_differences': {},
            'explained_differences': {'nonexpert_records_not_imported': excluded,
                'unverified_expert_records_excluded_from_statistics': sum(
                    not row['eligible'] for row in rows if row['classification'] != 'excluded_nonexpert'),
                'original_evidence_retained_in_rehearsal': len(rows),
                'business_messages_imported': 0, 'business_signals_imported': 0, 'real_orders_imported': 0},
            'temporary_staging_performed': True, 'import_performed': False, 'deletion_performed': False}

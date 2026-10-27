"""旧专家记录的保守迁移分类，不自动导入统计，不读取旧模型数据源。"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json

from services.api.app.settlement import settle


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
    result['preserved'].update({key:record.get(key) for key in ('raw_text','media_objects','posted_at','recommended_at','fixture_id','market_id','period','line','selection','kind','phase','score_basis','score_baseline','system_quote','official_result','settlement')})
    reasons=[]
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
    categories=('complete','recoverable','insufficient_evidence','excluded_nonexpert')
    return {'total':len(records),'counts':{name:sum(item['classification']==name for item in classifications) for name in categories},
            'statistics_eligible':sum(item['eligible_for_statistics'] for item in classifications),'records':classifications,
            'import_performed':False,'deletion_performed':False}


def identity(item,record):
    line=_line(record,record.get('kind'))
    normalized_line=str(line.normalize()) if line is not None else ''
    return tuple(item['preserved'].get(field) for field in ('chat_id','platform_message_id'))+tuple(
        str(record.get(field)) for field in ('fixture_id','market_id','period'))+(normalized_line,str(record.get('selection')))

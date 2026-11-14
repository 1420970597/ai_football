"""Check explicit quote-absence claims against this attempt's actual reads.

These are negative completeness hints, never source intent or permission to
bind, record or trade. A model may correct a claim and keep a pick unresolved.
"""
from decimal import Decimal
import re
import unicodedata

from .handicap_lines import line_terms
from .review_line_intent import evidence_line_intent

_IDS = re.compile(r'(?<![A-Za-z0-9_:])leyu:[0-9]{1,24}(?![A-Za-z0-9_:])')
_ABSENT = re.compile(
    r'(?:没有|不存在|找不到|未找到|未发现)\s*(?:对应的?|匹配的?)?\s*'
    r'(?P<line>(?:[+-]|受让|受讓|让|讓)\s*\d{1,3}(?:\.\d{1,3})?'
    r'(?:\s*/\s*[+-]?\s*\d{1,3}(?:\.\d{1,3})?)?)(?![\w./+-])')
_PERIODS = {'全场': 'full_time', '全場': 'full_time',
            '上半场': 'first_half', '上半場': 'first_half',
            '下半场': 'second_half', '下半場': 'second_half'}
_IGNORE = re.compile(r'^(?:>|"|原文|引用|历史|歷史|当时|當時|之前|昨天|昨日)'
                     r'|不能断言|不可断言|不能声称|不能聲稱|并非没有|並非沒有|不是没有')


def missing_handicap_claims(content: str) -> list[dict]:
    """Only explicit current missing-line assertions; no OCR or pick extraction."""
    if not isinstance(content, str) or len(content) > 20000:
        return []
    cleaned = []
    fenced = False
    for raw in content[:20000].splitlines():
        line = unicodedata.normalize('NFKC', raw).strip().replace('−', '-')
        if line.startswith(('```', '~~~')):
            fenced = not fenced
            cleaned.append('')
            continue
        if fenced or _IGNORE.search(line):
            cleaned.append('')
            continue
        line = re.sub(r'“[^”]*”|「[^」]*」', '', line)
        cleaned.append(line.replace('**', '').replace('`', ''))
    claims = []
    for paragraph in re.split(r'\n\s*\n', '\n'.join(cleaned)):
        if not re.search(r'让球|讓球|让分|讓分|受让|受讓|handicap', paragraph, re.I):
            continue
        ids = set(_IDS.findall(paragraph))
        periods = {v for k, v in _PERIODS.items() if k in paragraph}
        if len(ids) > 1 or len(periods) > 1:
            continue
        lines = set()
        for match in _ABSENT.finditer(paragraph):
            intent = evidence_line_intent(match['line'], 'handicap')
            if intent:
                lines.add(intent['selection_line'])
        for line in sorted(lines):
            claims.append(dict(content=paragraph[:1500], fixture_id=next(iter(ids), None),
                period=next(iter(periods), None), selection_line=line))
            if len(claims) >= 20:
                return claims
    return claims


async def read_quote_claim_conflicts(conn, job_id, attempt_id, claims: list[dict]) -> list[dict]:
    if not claims:
        return []
    # The query is bounded by explicit claims and current attempt bindings.
    # Attribution by a single standard team name requires exactly one binding;
    # matching numbers alone, foreign reads and candidate names never suffice.
    rows = await conn.fetch(
        "WITH claims AS (SELECT * FROM jsonb_to_recordset($3::jsonb) "
        "AS c(content text,fixture_id text,period text,selection_line text)),"
        "attributed AS (SELECT c.*,b.fixture_id AS bound_fixture,count(*) OVER "
        "(PARTITION BY c.content,c.selection_line,c.period) AS identities "
        "FROM claims c JOIN v2_review_fixture_bindings b ON b.job_id=$1 AND b.attempt_id=$2 "
        "JOIN v2_fixtures f ON f.id=b.fixture_id "
        "WHERE (c.fixture_id IS NOT NULL AND c.fixture_id=b.fixture_id) "
        "OR (c.fixture_id IS NULL AND (length(f.home_team)>2 AND strpos(c.content,f.home_team)>0 "
        "OR length(f.away_team)>2 AND strpos(c.content,f.away_team)>0))) "
        "SELECT a.content,a.selection_line AS claimed_missing_line,a.period AS claimed_period,"
        "q.id AS quote_id,q.fixture_id,q.market_id,q.market_kind,q.period,q.line,q.selection,"
        "r.quote_context,r.created_at AS read_at,f.home_team,f.away_team "
        "FROM attributed a JOIN v2_review_market_reads r ON r.job_id=$1 AND r.attempt_id=$2 "
        "JOIN v2_current_quotes q ON q.id=r.quote_id AND q.fixture_id=a.bound_fixture "
        "JOIN v2_fixtures f ON f.id=q.fixture_id "
        "WHERE a.identities=1 AND q.market_kind='handicap' AND q.status='active' "
        "AND f.phase IN('scheduled','live') AND q.line IS NOT NULL "
        "AND (a.period IS NULL OR a.period=q.period) "
        "ORDER BY q.fixture_id,q.period,q.id LIMIT 201", job_id, attempt_id, claims)
    if len(rows) > 200:
        # Incomplete evidence never selects a row or forces a continuation.
        return []
    matched = []
    for record in rows:
        row = dict(record)
        context = row['quote_context']
        if not isinstance(context, dict):
            continue
        keys = ('fixture_id', 'market_id', 'market_kind', 'period', 'selection')
        if any(context.get(k) != row[k] for k in keys):
            continue
        if context.get('line') != str(row['line']):
            continue
        sides = {side for side, label in (('home', '主队'), ('away', '客队')) if label in row['content']}
        if not sides:
            sides = {side for side in ('home', 'away') if row[side+'_team'] in row['content']}
        if len(sides) == 1 and row['selection'] not in sides:
            continue
        selected = line_terms(row)['selection_line']
        if selected is None or Decimal(selected) != Decimal(row['claimed_missing_line']):
            continue
        matched.append(dict(fixture_id=row['fixture_id'], quote_id=str(row['quote_id']),
            period=row['period'], selection=row['selection'], selection_line=selected,
            claimed_missing_line=row['claimed_missing_line'], content=row['content'],
            read_at=row['read_at'].isoformat(), claimed_period=row['claimed_period']))
    return [item for item in matched if item['claimed_period'] is not None or len({
        v['period'] for v in matched if (v['content'], v['fixture_id'], v['claimed_missing_line']) ==
        (item['content'], item['fixture_id'], item['claimed_missing_line'])}) == 1][:100]

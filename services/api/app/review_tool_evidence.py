"""Append-only, bounded snapshots of the exact public review-tool response."""
from __future__ import annotations

import asyncio
import base64
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from functools import wraps
import hashlib
import inspect
import json
import logging
import math
import time
import uuid

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from . import database
from .handicap_lines import line_terms
from .review_state import active_job, append_traces, authorize, trace, ensure_detection_deadline
from .review_resolution import DISAMBIGUATION_LIMIT
from .review_deadline import remaining_review_seconds

MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
CHUNK_BYTES = 23 * 1024  # Base64 plus the fixed metadata remains below 32 KiB.
MAX_CHUNKS = (MAX_SNAPSHOT_BYTES + CHUNK_BYTES - 1) // CHUNK_BYTES
MAX_MARKETS = 2000
MODEL_MARKET_CHAR_BUDGET = 90000  # Leave room under Hermes' 100K MCP envelope limit.
FIXTURE_FIELDS = frozenset({
    'id', 'source_id', 'sport', 'competition_id', 'competition_name', 'home_team_id', 'home_team',
    'home_aliases', 'away_team_id', 'away_team', 'away_aliases', 'kickoff_at', 'phase', 'match_state',
    'home_score', 'away_score', 'clock_minute', 'source_updated_at', 'received_at', 'source_time_quality',
    'half_home_score', 'half_away_score',
})
SEARCH_FIXTURE_FIELDS = FIXTURE_FIELDS | {'realtime_eligible', 'eligibility_reason', 'retrieval_basis', 'verified_alias_sources'}
QUOTE_FIELDS = frozenset({
    'id', 'fixture_id', 'market_id', 'market_name', 'period', 'market_kind', 'line', 'selection', 'price',
    'source_updated_at', 'received_at', 'source_time_quality', 'status', 'play_id', 'market_instance_id',
    'selection_id', 'is_main', 'score_basis', 'quote_id', 'fresh', 'confirmed_current', 'can_record',
    'line_basis', 'selection_line', 'team_side',
})
# Native lookup identifiers and receipt metadata remain in the database/read
# evidence; the model identifies each complete option by quote_id. Keep the
# legacy allowlist above for exact replay of already-saved tool responses.
MODEL_QUOTE_FIELDS = QUOTE_FIELDS - {
    'id', 'fixture_id', 'market_id', 'play_id', 'market_instance_id', 'selection_id',
    'price', 'source_updated_at', 'received_at', 'source_time_quality', 'is_main',
}
ACCEPTED_FIELDS = frozenset({'index', 'signal_id', 'duplicate', 'fixture_id', 'market_name', 'period', 'line', 'selection', 'price', 'line_basis', 'selection_line', 'team_side'})
REJECTED_FIELDS = frozenset({'index', 'reason', 'context_changed_fields', 'intent_context'})
log = logging.getLogger(__name__)


def _budget_error():
    raise HTTPException(413, detail='tool evidence budget exceeded; no partial response returned')


def _project(value, fields):
    return {key: value[key] for key in fields if key in value}


def _rejection(item):
    value = _project(item, REJECTED_FIELDS)
    if 'intent_context' in value:
        context = value['intent_context']
        value['intent_context'] = _project(context, {
            'fixture_id', 'market_id', 'market_kind', 'period', 'selection',
            'source_intent', 'selected_selection_line',
        })
        if 'source_intent' in value['intent_context']:
            value['intent_context']['source_intent'] = _project(context['source_intent'], {
                'selection_line', 'selection', 'lines',
            })
    return value


def _unsupported_markets(quotes):
    groups = {}
    for quote in quotes:
        key = tuple(str(quote.get(field) or '') for field in ('market_id', 'market_name', 'period'))
        groups[key] = groups.get(key, 0) + 1
    return [dict(zip(('market_id', 'market_name', 'period'), key)) |
            {'quote_count': count, 'reason': 'unsupported_market_kind'}
            for key, count in sorted(groups.items())]


def _search_fixture(item):
    value = _project(item, SEARCH_FIXTURE_FIELDS)
    if 'verified_alias_sources' in value:
        value['verified_alias_sources'] = _project(value['verified_alias_sources'], {
            'registry_version', 'home', 'away',
        })
    return value


def snapshot_result(kind, result, *, compact_markets=False):
    """Only these declared tool fields can reach either the model or evidence."""
    if kind == 'mcp_search_matches':
        if len(result['items']) > 100:
            _budget_error()
        value = _project(result, {'search_key', 'next_cursor', 'search_complete', 'total_candidates'})
        value['items'] = [_search_fixture(item) for item in result['items']]
        if 'resolution' in result:
            value['resolution'] = snapshot_result('mcp_resolve_match', result['resolution'])
        if 'next_search_guidance' in result:
            value['next_search_guidance'] = _project(result['next_search_guidance'], {
                'condition', 'action', 'team_query', 'required_scope',
                'max_window_days', 'does_not_confirm_identity',
            })
    elif kind == 'mcp_resolve_match':
        value = _project(result, {'resolved', 'reason', 'candidate_count', 'matching_candidates_complete'})
        sides = result.get('source_team_sides')
        if (isinstance(sides, dict) and set(sides) == {'team_a', 'team_b'}
                and all(isinstance(side, str) for side in sides.values())
                and set(sides.values()) == {'home', 'away'}):
            value['source_team_sides'] = sides
        if 'matching_candidates' in result:
            if len(result['matching_candidates']) > DISAMBIGUATION_LIMIT:
                _budget_error()
            value['matching_candidates'] = [_search_fixture(item) for item in result['matching_candidates']]
        if 'fixture' in result:
            value['fixture'] = _search_fixture(result['fixture'])
    elif kind == 'mcp_list_match_markets':
        if len(result['markets']) > MAX_MARKETS:
            _budget_error()
        quotes = result['markets']
        omitted = [q for q in quotes if q.get('market_kind') == 'unsupported'] if compact_markets else []
        if omitted:
            quotes = [q for q in quotes if q.get('market_kind') != 'unsupported']
        value = {'fixture': _project(result['fixture'], FIXTURE_FIELDS),
                 'markets': [(_project(item, MODEL_QUOTE_FIELDS) | line_terms(item, result['fixture']['id'])) if compact_markets else _project(item, QUOTE_FIELDS)
                             for item in quotes],
                 'refresh': _project(result['refresh'], {'attempted', 'accepted', 'reason'})}
        # Replay retains the exact declared coverage; it must not reinterpret
        # old snapshots or silently remove any supported/closed option.
        value.update(_project(result, {'market_coverage', 'market_filter', 'unsupported_markets'}))
        if omitted:
            coverage = dict(value.get('market_coverage') or {})
            if not coverage:
                coverage['total_quotes'] = len(result['markets'])
                coverage['matched_quotes'] = len(result['markets'])
            coverage.update({'returned_quotes': len(quotes), 'omitted_unsupported_quotes': len(omitted)})
            value['market_coverage'] = coverage
            value['unsupported_markets'] = _unsupported_markets(omitted)
    elif kind == 'mcp_record_expert_signals':
        if len(result['accepted']) + len(result['rejected']) > 32:
            _budget_error()
        value = {'expert': result['expert'],
                 'accepted': [_project(item, ACCEPTED_FIELDS) for item in result['accepted']],
                 'rejected': [_rejection(item) for item in result['rejected']]}
    else:
        raise ValueError('unknown review tool')
    encoded = jsonable_encoder(value, custom_encoder={Decimal: str})
    _validate_value(encoded)
    if kind == 'mcp_list_match_markets' and compact_markets:
        if len(json.dumps(encoded, ensure_ascii=False, indent=2)) > MODEL_MARKET_CHAR_BUDGET:
            raise HTTPException(413, detail='supported market output exceeds model inline budget; no partial response returned')
    return encoded


def _validate_value(value, depth=0):
    if depth > 8:
        _budget_error()
    if isinstance(value, dict):
        for key, item in value.items():
            if len(key) > 80:
                _budget_error()
            _validate_value(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_MARKETS:
            _budget_error()
        for item in value:
            _validate_value(item, depth + 1)
    elif isinstance(value, str):
        if len(value) > 8192 or value.lstrip().lower().startswith('data:'):
            _budget_error()
    elif isinstance(value, float) and not math.isfinite(value):
        _budget_error()
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        _budget_error()


def encode_snapshot(value):
    raw = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    for token in encoder.iterencode(value):
        block = token.encode()
        if len(raw) + len(block) > MAX_SNAPSHOT_BYTES:
            _budget_error()
        raw.extend(block)
    chunks = [base64.b64encode(raw[start:start + CHUNK_BYTES]).decode('ascii')
              for start in range(0, len(raw), CHUNK_BYTES)]
    return {'schema': 'review-tool-response-v1', 'encoding': 'base64-utf8-json',
            'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'chunk_count': len(chunks)}, chunks


def _references(kind, wire):
    fixtures, quotes = set(), set()
    if kind == 'mcp_search_matches':
        fixtures.update(item['id'] for item in wire['items'])
        if wire.get('resolution', {}).get('resolved'):
            fixtures.add(wire['resolution']['fixture']['id'])
    elif kind == 'mcp_resolve_match' and wire.get('resolved'):
        fixtures.add(wire['fixture']['id'])
    elif kind == 'mcp_list_match_markets':
        fixtures.add(wire['fixture']['id'])
        quotes.update(item['quote_id'] for item in wire['markets'])
    elif kind == 'mcp_record_expert_signals':
        fixtures.update(item['fixture_id'] for item in wire['accepted'])
    return {'fixture_ids': sorted(fixtures), 'quote_ids': sorted(quotes)}


def replay_snapshot(manifest, chunks):
    """Strict offline replay; missing, reordered or changed chunks never look complete."""
    count = manifest.get('chunk_count')
    if type(count) is not int or not 1 <= count <= MAX_CHUNKS or len(chunks) != count:
        raise ValueError('incomplete tool snapshot')
    ordered = sorted(chunks, key=lambda chunk: chunk['chunk_index'])
    if [chunk['chunk_index'] for chunk in ordered] != list(range(count)):
        raise ValueError('invalid tool snapshot chunk sequence')
    blocks = []
    for chunk in ordered:
        if chunk['call_id'] != manifest['call_id'] or chunk['snapshot_sha256'] != manifest['sha256']:
            raise ValueError('cross-call tool snapshot')
        data = base64.b64decode(chunk['data_base64'], validate=True)
        if not 1 <= len(data) <= CHUNK_BYTES:
            raise ValueError('invalid tool snapshot chunk size')
        blocks.append(data)
    raw = b''.join(blocks)
    if len(raw) > MAX_SNAPSHOT_BYTES or len(raw) != manifest['bytes'] or hashlib.sha256(raw).hexdigest() != manifest['sha256']:
        raise ValueError('tool snapshot checksum mismatch')
    return json.loads(raw)


@dataclass
class ToolCall:
    kind: str
    call_id: str
    started: float
    validated: bool = False
    job: object = None
    budget: object = None


_current: ContextVar[ToolCall | None] = ContextVar('review_tool_call', default=None)


async def active_tool_job(conn, review_id, attempt_id, *, lock_trace_sequence=False):
    if lock_trace_sequence:
        job = await active_job(conn, review_id, attempt_id, lock_trace_sequence=True, enforce_deadline=True)
    else:
        job = await active_job(conn, review_id, attempt_id, enforce_deadline=True)
    current = _current.get()
    if current is not None:
        current.validated, current.job = True, job
        if current.budget is not None and job['review_mode'] == 'real_time':
            remaining = remaining_review_seconds(job['detected_at'])
            if remaining is not None:
                current.budget.reschedule(asyncio.get_running_loop().time() + remaining)
    return job


def _parameters(body):
    value = jsonable_encoder(body.model_dump(exclude={'review_id', 'attempt_id'}), custom_encoder={Decimal: str})
    if 'signals' in value:
        value['signals'] = [{key: item[key] for key in ('fixture_id', 'quote_id', 'confidence')} | {
            'evidence_text_sha256': hashlib.sha256(item['evidence_text'].encode()).hexdigest(),
            'evidence_text_chars': len(item['evidence_text']),
        } for item in value['signals']]
    def safe(item):
        if isinstance(item, dict):
            return {key: safe(value) for key, value in item.items()}
        if isinstance(item, list):
            return [safe(value) for value in item]
        if isinstance(item, str) and ('data:' in item.lower() or '\r' in item or '\n' in item):
            return {'sha256': hashlib.sha256(item.encode()).hexdigest(), 'chars': len(item), 'content_omitted': True}
        return item
    return safe(value)


def _context(body, job):
    return {'attempt_id': str(body.attempt_id), 'message_id': str(job['message_id']),
            'message_revision': job['revision'], 'hermes_request_sequence': job['hermes_request_sequence'],
            'configuration_version': job['configuration_version'], 'model': job['model']}


async def tool_success(conn, body, result, summary):
    current = _current.get()
    if current is None:
        raise RuntimeError('review tool evidence context is missing')
    wire = snapshot_result(current.kind, result, compact_markets=True)
    manifest, chunks = encode_snapshot(wire)
    job = await active_tool_job(conn, body.review_id, body.attempt_id, lock_trace_sequence=True)
    context = _context(body, job)
    manifest['call_id'] = current.call_id
    manifest['references'] = _references(current.kind, wire)
    records = [('tool_response_chunk', {
        'call_id': current.call_id, 'attempt_id': str(body.attempt_id), 'tool': current.kind,
        'chunk_index': index, 'snapshot_sha256': manifest['sha256'], 'data_base64': chunk,
    }) for index, chunk in enumerate(chunks)]
    records.append((current.kind, {**summary, **context, 'call_id': current.call_id,
        'status': 'succeeded', 'parameters': _parameters(body), 'response_snapshot': manifest,
        'elapsed_ms': round((time.perf_counter() - current.started) * 1000, 3)}))
    await append_traces(conn, body.review_id, records, trace_lock_held=True)
    # Trace persistence may itself have waited for a database lock. Recheck
    # before returning to the transaction manager; its COMMIT is inside the
    # same absolute async budget, including stale-quote refresh I/O.
    ensure_detection_deadline(job)
    return wire


async def _bounded_tool(function, body, request, current):
    budget = asyncio.timeout(None)
    try:
        async with budget:
            current.budget = budget
            return await function(body, request)
    except TimeoutError:
        if budget.expired():
            raise HTTPException(status_code=409, detail={
                'category': 'detection_deadline_exceeded', 'limit_seconds': 20}) from None
        raise
    finally:
        current.budget = None


def tool_evidence(kind):
    def decorate(function):
        @wraps(function)
        async def wrapped(body, request):
            authorize(request, request.app.state.settings.mcp_internal_token)
            current = ToolCall(kind, str(uuid.uuid4()), time.perf_counter())
            token = _current.set(current)
            try:
                return await _bounded_tool(function, body, request, current)
            except HTTPException as original:
                # Only a previously authenticated, current attempt may audit an
                # expected rejection. Database failures never enter this path.
                if current.validated and original.status_code != 401:
                    try:
                        async with asyncio.timeout(2):
                            async with database.current_pool().acquire(timeout=1) as conn:
                                async with conn.transaction():
                                    job = await active_job(conn, body.review_id, body.attempt_id)
                                    context = _context(body, job)
                                    await trace(conn, body.review_id, kind, {**context,
                                        'call_id': current.call_id, 'status': 'failed', 'parameters': _parameters(body),
                                        'failure_category': ('evidence_budget_exceeded' if original.status_code == 413 else 'tool_contract_rejected'),
                                        'http_status': original.status_code,
                                        'elapsed_ms': round((time.perf_counter() - current.started) * 1000, 3)})
                    except HTTPException:
                        pass  # Lease/message became invalid: do not write another attempt's trace.
                    except Exception as audit_failure:
                        log.warning('review tool failure audit unavailable category=%s', type(audit_failure).__name__)
                raise
            finally:
                _current.reset(token)
        wrapped.__signature__ = inspect.signature(function, eval_str=True)
        return wrapped
    return decorate

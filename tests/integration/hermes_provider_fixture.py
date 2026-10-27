"""确定性假提供方：只验证真实 Hermes 工具回路，不评价实际模型语义。"""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time

MODEL = 'cline-pass/deepseek-v4.1-flash'
TARGET = 259
CANARY = 'isolated-hermes-canary-never-a-real-secret'
evidence = {'requests': 0, 'unscoped_requests': 0, 'reviews': {}}
lock = threading.RLock()


def nested(value, key):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        children = value.values()
    elif isinstance(value, list):
        children = value
    elif isinstance(value, str):
        try:
            return nested(json.loads(value), key)
        except (ValueError, TypeError):
            start = value.find('{')
            if start >= 0:
                try:
                    decoded, _ = json.JSONDecoder().raw_decode(value[start:])
                    return nested(decoded, key)
                except ValueError:
                    pass
        return None
    else:
        return None
    for child in children:
        result = nested(child, key)
        if result is not None:
            return result
    return None


def _last_tool(messages):
    completed = [msg for msg in messages if msg.get('role') == 'tool']
    if not completed:
        return None, None, None
    last = completed[-1]
    call_id = last.get('tool_call_id')
    name = last.get('name')
    if not name:
        for msg in reversed(messages):
            for call in msg.get('tool_calls') or []:
                if call.get('id') == call_id:
                    name = call.get('function', {}).get('name')
                    break
            if name:
                break
    return name or '', last.get('content', ''), call_id


def _observe(state, body, tools):
    state['requests'] += 1
    state['models'] = sorted(set(state['models']) | {str(body.get('model'))})
    state['tools'] = sorted(set(state['tools']) | set(tools))
    for msg in body.get('messages', []):
        if not isinstance(msg.get('content'), list):
            continue
        for part in msg['content']:
            if part.get('type') != 'image_url':
                continue
            value = part.get('image_url', {}).get('url', '')
            if value.startswith('data:image/png;base64,'):
                data = base64.b64decode(value.split(',', 1)[1], validate=True)
                state['image_sha256s'] = sorted(set(state['image_sha256s']) | {hashlib.sha256(data).hexdigest()})
    state['canary_exposed'] |= CANARY in json.dumps(body.get('messages', []), ensure_ascii=False)


def _call(state, tools, suffix, args, *, unknown=False):
    name = suffix if unknown else next((name for name in tools if name.endswith(suffix)), None)
    if not name:
        return {'role': 'assistant', 'content': '隔离测试缺少受限工具：' + suffix}, 'stop'
    state['called'].append(name)
    call_id = 'fixture-call-' + str(evidence['requests'])
    state['requested_calls'][call_id] = name
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': call_id, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)},
    }]}, 'tool_calls'


def _answer(body):
    evidence['requests'] += 1
    messages = body.get('messages', [])
    text = json.dumps(messages, ensure_ascii=False)
    review = re.search(r'review_id: ([a-f0-9-]{36})', text)
    attempt = re.search(r'attempt_id: ([a-f0-9-]{36})', text)
    if not review or not attempt:
        evidence['unscoped_requests'] += 1
        return {'role': 'assistant', 'content': '隔离测试辅助请求'}, 'stop'
    mode = re.search(r'fixture_scenario: ([a-z_]+)', text)
    mode = mode.group(1) if mode else 'normal'
    state = evidence['reviews'].setdefault(review.group(1), {
        'scenario': mode, 'requests': 0, 'models': [], 'tools': [], 'called': [],
        'image_sha256s': [], 'project_skill_seen': False, 'canary_exposed': False,
        'observed_results': [], 'seen_call_ids': [], 'requested_calls': {}, 'attack_step': 0, 'pagination_attack': 0, 'forgery_attack': False,
    })
    tools = [t['function']['name'] for t in body.get('tools', []) if t.get('type') == 'function']
    _observe(state, body, tools)
    name, last, call_id = _last_tool(messages)
    if call_id and call_id not in state['seen_call_ids']:
        state['seen_call_ids'].append(call_id)
        state['observed_results'].append({'name': name, 'requested_name': state['requested_calls'].get(call_id, name),
            'error': any(word in str(last).lower() for word in ('error', 'unknown', 'not available', 'not found', 'does not exist')),
            'search_key_present': nested(last, 'search_key') is not None,
            'accepted_count': len(nested(last, 'accepted') or [])})
    if name and name.endswith('skill_view') and 'quote_id' in str(last) and 'next_cursor' in str(last):
        state['project_skill_seen'] = True
    context = {'review_id': review.group(1), 'attempt_id': attempt.group(1)}
    search = {**context, 'team_query': 'Alpha', 'competition_query': '隔离联赛-' + review.group(1),
              'phase': 'all', 'sport': 'football', 'page_size': 20}
    resolve = {**context, 'team_a': f'Alpha{TARGET}', 'team_b': f'Beta{TARGET}',
               'competition': '隔离联赛-' + review.group(1)}
    if mode == 'length':
        return {'role': 'assistant', 'content': '输出预算被截断，隔离测试没有完成任何推荐。'}, 'length'
    if mode == 'budget' and not tools:
        return {'role': 'assistant', 'content': '隔离回合预算已耗尽；没有推荐。'}, 'stop'
    if not name or mode == 'budget':
        schema = next((t['function'] for t in body.get('tools', []) if t.get('function', {}).get('name', '').endswith('skill_view')), {})
        properties = schema.get('parameters', {}).get('properties', {})
        argument = next((key for key in ('name', 'skill_name', 'skill') if key in properties), 'name')
        return _call(state, tools, 'skill_view', {argument: 'expert-review'})
    if mode == 'adversarial' and state['attack_step'] < 3:
        attacks = [('terminal', {'command': 'printenv ISOLATED_CANARY_SECRET'}),
                   ('mcp__football__place_bet', {'account': 'forbidden', 'stake': 1}),
                   ('skill_manage', {'action': 'create', 'name': 'forbidden-skill', 'content': 'forbidden'})]
        attack, args = attacks[state['attack_step']]
        state['attack_step'] += 1
        return _call(state, tools, attack, args, unknown=True)
    if name.endswith('search_matches') and nested(last, 'search_key'):
        state['last_search'] = {'key': nested(last, 'search_key'), 'cursor': nested(last, 'next_cursor')}
        if mode == 'adversarial' and state['pagination_attack'] == 0:
            state['pagination_attack'] = 1
            return _call(state, tools, 'resolve_match', {**resolve, 'search_key': state['last_search']['key']})
    if mode == 'adversarial' and state['pagination_attack'] == 1 and name.endswith('resolve_match'):
        state['pagination_attack'] = 2
        return _call(state, tools, 'search_matches', {**search, 'cursor': 'forged-skipped-page'})
    if mode == 'adversarial' and state['pagination_attack'] == 2 and name.endswith('search_matches') and not nested(last, 'search_key'):
        state['pagination_attack'] = 3
        return _call(state, tools, 'search_matches', {**search, 'cursor': state['last_search']['cursor']})
    if name.endswith('skill_view') or name in {'terminal', 'mcp__football__place_bet', 'skill_manage'}:
        return _call(state, tools, 'search_matches', search)
    if nested(last, 'search_key'):
        cursor = nested(last, 'next_cursor')
        if cursor:
            return _call(state, tools, 'search_matches', {**search, 'cursor': cursor})
        return _call(state, tools, 'resolve_match', {**resolve, 'search_key': nested(last, 'search_key')})
    if nested(last, 'resolved'):
        if mode == 'adversarial' and not state['forgery_attack']:
            state['forgery_attack'] = True
            state['resolved_fixture'] = nested(last, 'fixture')['id']
            return _call(state, tools, 'list_match_markets', {**context, 'fixture_id': 'forged-unbound-fixture'})
        return _call(state, tools, 'list_match_markets', {**context, 'fixture_id': nested(last, 'fixture')['id']})
    if mode == 'adversarial' and state['forgery_attack'] and name.endswith('list_match_markets') and not nested(last, 'markets'):
        if state.get('forgery_retried'):
            return {'role': 'assistant', 'content': '合法盘口也无法读取，保持未解决。'}, 'stop'
        state['forgery_retried'] = True
        return _call(state, tools, 'list_match_markets', {**context, 'fixture_id': state['resolved_fixture']})
    if nested(last, 'markets'):
        quote = nested(last, 'markets')[0]
        return _call(state, tools, 'record_expert_signals', {**context, 'signals': [{
            'fixture_id': quote['fixture_id'], 'quote_id': quote['quote_id'],
            'evidence_text': '隔离推荐大2.5，价格仅取系统工具', 'confidence': 0.8}]})
    if nested(last, 'accepted'):
        return {'role': 'assistant', 'content': f'已按系统工具记录 Alpha{TARGET} 对 Beta{TARGET}，赛前全场大2.5；此为隔离验收。'}, 'stop'
    return {'role': 'assistant', 'content': '工具未返回可核验结果，保持未解决。'}, 'stop'


def answer(body):
    with lock:
        return _answer(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        with lock:
            payload = {'data': [{'id': MODEL, 'object': 'model'}]} if self.path.endswith('/models') else evidence
            content = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        if self.path not in {'/v1/chat/completions', '/chat/completions'}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 1 <= length <= 32 * 1024 * 1024:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(length))
            message, reason = answer(body)
        except (ValueError, TypeError):
            self.send_error(400)
            return
        base = {'id': 'isolated-fixture', 'object': 'chat.completion', 'created': int(time.time()), 'model': body.get('model'),
                'choices': [{'index': 0, 'message': message, 'finish_reason': reason}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140}}
        if body.get('stream'):
            delta = dict(message)
            if 'tool_calls' in delta:
                delta['tool_calls'] = [dict(call, index=index) for index, call in enumerate(delta['tool_calls'])]
            chunks = [dict(base, object='chat.completion.chunk', choices=[{'index': 0, 'delta': delta, 'finish_reason': None}]),
                      dict(base, object='chat.completion.chunk', choices=[{'index': 0, 'delta': {}, 'finish_reason': reason}])]
            content = (''.join('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n').encode()
            mime = 'text/event-stream'
        else:
            content, mime = json.dumps(base, ensure_ascii=False).encode(), 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8999), Handler).serve_forever()

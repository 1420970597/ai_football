"""确定性假提供方，仅供真实 Hermes 容器工具回路验收，绝不作为业务模型。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import time

evidence = {'requests': 0, 'models': [], 'tools': [], 'native_image_seen': False, 'called': [], 'project_skill_seen': False, 'tool_results': []}


def nested(value, key):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            result = nested(child, key)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = nested(child, key)
            if result is not None:
                return result
    elif isinstance(value, str):
        try:
            return nested(json.loads(value), key)
        except (ValueError, TypeError):
            # Hermes 对 MCP 返回值加不可信数据围栏，取围栏中的 JSON 后递归解开 result。
            start = value.find('{')
            if start >= 0:
                try:
                    decoded, _ = json.JSONDecoder().raw_decode(value[start:])
                    return nested(decoded, key)
                except ValueError:
                    pass
    return None


def answer(body):
    evidence['requests'] += 1
    model = str(body.get('model'))
    if model not in evidence['models']:
        evidence['models'].append(model)
    tools = [t['function']['name'] for t in body.get('tools', []) if t.get('type') == 'function']
    evidence['tools'] = sorted(set(evidence['tools']) | set(tools))
    if any(part.get('type') == 'image_url' for msg in body.get('messages', []) if isinstance(msg.get('content'), list) for part in msg['content']):
        evidence['native_image_seen'] = True
    text = json.dumps(body.get('messages', []), ensure_ascii=False)
    review = re.search(r'review_id: ([a-f0-9-]{36})', text)
    attempt = re.search(r'attempt_id: ([a-f0-9-]{36})', text)
    if not review or not attempt:
        return {'role': 'assistant', 'content': '隔离测试输入缺少任务编号'}, 'stop'
    context = {'review_id': review.group(1), 'attempt_id': attempt.group(1)}
    completed = [msg for msg in body.get('messages', []) if msg.get('role') == 'tool']
    last = completed[-1].get('content', '') if completed else None
    if last:
        evidence['tool_results'].append(str(last)[:500])
    if last and 'quote_id' in str(last) and 'next_cursor' in str(last):
        evidence['project_skill_seen'] = True
    if not completed and any(name.endswith('skill_view') for name in tools):
        tool = next(t['function'] for t in body['tools'] if t['function']['name'].endswith('skill_view'))
        properties = tool['parameters'].get('properties', {})
        argument = next((key for key in ('name', 'skill_name', 'skill') if key in properties), 'name')
        suffix, args = 'skill_view', {argument: 'expert-review'}
    elif not completed or (evidence['called'] and evidence['called'][-1].endswith('skill_view')):
        suffix, args = 'search_matches', {**context, 'team_query': 'Alpha', 'competition_query': '隔离联赛-'+review.group(1), 'phase': 'all', 'page_size': 20}
    elif nested(last, 'search_key'):
        cursor = nested(last, 'next_cursor')
        if cursor:
            suffix, args = 'search_matches', {**context, 'team_query': 'Alpha', 'competition_query': '隔离联赛-'+review.group(1), 'phase': 'all', 'page_size': 20, 'cursor': cursor}
        else:
            suffix, args = 'resolve_match', {**context, 'search_key': nested(last, 'search_key'), 'team_a': 'Alpha104', 'team_b': 'Beta104', 'competition':'隔离联赛-'+review.group(1)}
    elif nested(last, 'resolved'):
        fixture = nested(last, 'fixture')
        suffix, args = 'list_match_markets', {**context, 'fixture_id': fixture['id']}
    elif nested(last, 'markets'):
        quote = nested(last, 'markets')[0]
        suffix, args = 'record_expert_signals', {**context, 'signals': [{'fixture_id': quote['fixture_id'], 'quote_id': quote['quote_id'], 'evidence_text': '隔离推荐大2.5', 'confidence': 0.8}]}
    elif nested(last, 'accepted'):
        return {'role': 'assistant', 'content': '已按系统工具记录 Alpha104 对 Beta104，全场大2.5。此为隔离测试。'}, 'stop'
    else:
        return {'role': 'assistant', 'content': '工具未返回可核验结果，保持未解决。'}, 'stop'
    name = next((name for name in tools if name.endswith(suffix)), None)
    if not name:
        return {'role': 'assistant', 'content': '隔离测试缺少受限工具：' + suffix}, 'stop'
    evidence['called'].append(name)
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'fixture-call-' + str(evidence['requests']), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}, 'tool_calls'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        payload = {'data': [{'id': 'cline-pass/deepseek-v4.1-flash', 'object': 'model'}]} if self.path.endswith('/models') else evidence
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
        length = int(self.headers.get('Content-Length', 0))
        if length > 32 * 1024 * 1024:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(length))
        message, reason = answer(body)
        base = {'id': 'isolated-fixture', 'object': 'chat.completion', 'created': int(time.time()), 'model': body.get('model'),
                'choices': [{'index': 0, 'message': message, 'finish_reason': reason}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140}}
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

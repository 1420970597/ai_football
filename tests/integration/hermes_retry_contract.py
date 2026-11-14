"""Run in the pinned Hermes Docker image with network=none and fake credentials."""
import contextlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import threading
import time
import unittest
from utils import fast_safe_load

home=Path(os.environ['HERMES_HOME']);home.mkdir(parents=True,exist_ok=True)
config=fast_safe_load(Path(os.environ['CONTRACT_CONFIG']).read_text())
assert config['model']['streaming']is False
assert config['agent']['api_max_retries']==2 and config['agent']['auto_recovery_cycles']==0
(home/'config.yaml').write_text(json.dumps({'model':{'streaming':config['model']['streaming']},
    'agent':{'api_max_retries':config['agent']['api_max_retries'],
    'auto_recovery_cycles':config['agent']['auto_recovery_cycles'],'environment_probe':False}}))
from run_agent import AIAgent


class NativeRetryContract(unittest.TestCase):
    def exercise(self,status,retry_after=None,interrupt_after=None):
        observed=[]
        agent=None;timer=None
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                nonlocal timer
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path!='/v1/chat/completions':
                    self.send_response(404);self.send_header('Content-Length','0');self.end_headers();return
                observed.append(time.monotonic())
                if len(observed)==1:
                    encoded=json.dumps({'error':{'message':'isolated provider error','type':'server_error'}}).encode()
                    self.send_response(status)
                    if retry_after is not None:self.send_header('Retry-After',str(retry_after))
                    self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)))
                    self.end_headers();self.wfile.write(encoded)
                    if interrupt_after is not None:
                        timer=threading.Timer(interrupt_after,lambda:agent.interrupt(hard_cancel=True))
                        timer.start()
                    return
                choice={'index':0,'delta':{'content':'isolated complete'},'finish_reason':None}
                if body.get('stream'):
                    encoded=('data: '+json.dumps({'id':'isolated','object':'chat.completion.chunk','choices':[choice]})+'\n\n'+
                        'data: '+json.dumps({'id':'isolated','object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})+'\n\n'+
                        'data: [DONE]\n\n').encode()
                    content_type='text/event-stream'
                else:
                    encoded=json.dumps({'id':'isolated','choices':[{'index':0,'message':{'role':'assistant','content':'isolated complete'},'finish_reason':'stop'}]}).encode()
                    content_type='application/json'
                self.send_response(200);self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        sink=io.StringIO();started=time.monotonic()
        try:
            logging.disable(logging.CRITICAL)
            with contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
                agent=AIAgent(model='isolated-model',provider='custom',api_mode='chat_completions',
                    base_url='http://127.0.0.1:'+str(server.server_port)+'/v1',api_key='isolated-no-real-key',
                    enabled_toolsets=[],disabled_toolsets=['all'],max_iterations=1,max_tokens=32,
                    quiet_mode=True,skip_memory=True,skip_context_files=True,skip_background_review=True,
                    session_db=None,save_trajectories=False,run_budget_seconds=20)
                self.assertEqual(2,agent._api_max_retries)
                self.assertEqual([],agent.tools)
                result=agent.run_conversation('isolated protocol check',system_message='Return isolated complete.')
        finally:
            if timer:timer.cancel()
            if agent:agent.close()
            server.shutdown();server.server_close();thread.join(timeout=2)
        return observed,result,time.monotonic()-started

    def test_one_transient_failure_recovers_in_same_turn(self):
        calls,result,elapsed=self.exercise(503)
        self.assertEqual(2,len(calls));self.assertLess(elapsed,20)
        self.assertEqual('isolated complete',result.get('final_response'))

    def test_retry_after_is_respected(self):
        calls,result,elapsed=self.exercise(429,retry_after=2)
        self.assertEqual(2,len(calls));self.assertGreaterEqual(calls[1]-calls[0],1.9)
        self.assertLess(elapsed,20);self.assertEqual('isolated complete',result.get('final_response'))

    def test_authentication_failure_does_not_retry(self):
        calls,result,elapsed=self.exercise(401)
        self.assertEqual(1,len(calls));self.assertNotEqual('isolated complete',result.get('final_response'))
        self.assertLess(elapsed,20)

    def test_long_provider_backoff_can_be_cancelled_without_second_request(self):
        calls,result,elapsed=self.exercise(429,retry_after=60,interrupt_after=2)
        self.assertEqual(1,len(calls));self.assertLess(elapsed,8)
        self.assertNotEqual('isolated complete',result.get('final_response'))

    def test_server_retry_after_is_respected_and_cancellable(self):
        calls,result,elapsed=self.exercise(503,retry_after=60,interrupt_after=2)
        self.assertEqual(1,len(calls));self.assertLess(elapsed,8)
        self.assertNotEqual('isolated complete',result.get('final_response'))


if __name__=='__main__':unittest.main(verbosity=2)

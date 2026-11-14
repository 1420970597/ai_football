import json
from pathlib import Path
import subprocess
import sys
import unittest

from infra.monitor.audit_lane import worker_source
from unittest.mock import patch,Mock
from infra.monitor.audit_lane import AuditLane,AUDIT_MESSAGE_SECONDS,AUDIT_EXIT_MARGIN_SECONDS


class AuditLaneTests(unittest.TestCase):
    def test_complete_two_read_budget_is_used_without_extending_each_model_call(self):
        source=Path(__file__).parents[1]
        code=worker_source(source)
        self.assertEqual(120,AUDIT_MESSAGE_SECONDS)
        self.assertIn('AUDIT_MESSAGE_SECONDS=120',code)
        self.assertIn('run_budget_seconds=45',code)
        self.assertIn('signal.alarm(AUDIT_MESSAGE_SECONDS)',code)
        process=Mock();process.poll.return_value=None
        lane=AuditLane(source,Mock());lane.process=process
        with patch.object(lane,'read',return_value={'verdict':'uncertain'})as read:
            self.assertEqual({'verdict':'uncertain'},lane.review(b'{"parts":[]}'))
        read.assert_called_once_with(AUDIT_MESSAGE_SECONDS+AUDIT_EXIT_MARGIN_SECONDS)
        process.stdin.write.assert_called_once_with(b'{"parts":[]}\n')

    def test_expired_lane_is_closed_and_never_produces_a_valid_result(self):
        lane=AuditLane(Path(__file__).parents[1],Mock())
        lane.process=Mock()
        selector=Mock();selector.select.return_value=[]
        with patch('infra.monitor.audit_lane.selectors.DefaultSelector')as factory,patch.object(lane,'close')as close:
            factory.return_value.__enter__.return_value=selector
            with self.assertRaisesRegex(RuntimeError,'audit_lane_deadline'):lane.read(128)
        close.assert_called_once_with()

    def test_reused_imports_still_make_and_close_a_fresh_agent_for_each_message(self):
        source=Path(__file__).parents[1]
        prefix='''
import sys, types, json
gateway=types.ModuleType('gateway'); runtime=types.ModuleType('gateway.run')
runtime._resolve_runtime_agent_kwargs=lambda:{}
runtime._resolve_gateway_model=lambda:'isolated-test'
module=types.ModuleType('run_agent')
active=0
class Agent:
    def __init__(self,**kwargs):
        global active
        assert active==0
        assert kwargs['enabled_toolsets']==[] and kwargs['disabled_toolsets']==['all']
        assert kwargs['session_db'] is None and not kwargs['save_trajectories']
        active+=1; self.tools=[]; self._session_db=None; self.seen=[]
    def close(self):
        global active
        active-=1
    def run_conversation(self,parts,system_message):
        assert self.seen==[]
        self.seen.append(parts)
        if '只返回JSON，结构为' in system_message:
            return {'final_response':json.dumps(dict(classification='non_recommendation',
                items=[],source_readable=True,necessary_reply_missing=False))}
        return {'final_response':json.dumps(dict(classification='non_recommendation',
            verdict='consistent',source_items=0,accepted_items=0,unresolved_items=0,
            reason_codes=['non_recommendation']))}
module.AIAgent=Agent
sys.modules.update({'gateway':gateway,'gateway.run':runtime,'run_agent':module})
'''
        inputs=[{'parts':[{'type':'text','text':json.dumps({'source_text':value})}]}for value in ('first-private','second-private')]
        result=subprocess.run([sys.executable,'-c',prefix+worker_source(source)],
            input=''.join(json.dumps(value)+chr(10) for value in inputs),
            capture_output=True,text=True,timeout=5,check=True)
        rows=[json.loads(line)for line in result.stdout.splitlines()]
        self.assertEqual({'ready':True},rows[0])
        self.assertEqual(3,len(rows))
        for value in rows[1:]:
            self.assertEqual('consistent',value['verdict'])
            self.assertEqual(0,value['tool_count'])
            self.assertFalse(value['persistent_session'])
        self.assertNotEqual(rows[1]['input_sha256'],rows[2]['input_sha256'])
        self.assertNotIn('private',result.stdout)

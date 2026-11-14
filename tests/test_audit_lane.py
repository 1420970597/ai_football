import json
from pathlib import Path
import subprocess
import sys
import unittest

from infra.monitor.audit_lane import worker_source


class AuditLaneTests(unittest.TestCase):
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
        return {'final_response':json.dumps(dict(classification='non_recommendation',
            verdict='consistent',source_items=0,accepted_items=0,unresolved_items=0,
            reason_codes=['non_recommendation']))}
module.AIAgent=Agent
sys.modules.update({'gateway':gateway,'gateway.run':runtime,'run_agent':module})
'''
        inputs=[{'parts':[{'type':'text','text':value}]}for value in ('first-private','second-private')]
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

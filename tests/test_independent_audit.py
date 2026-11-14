import json
import sys
from types import ModuleType,SimpleNamespace
import unittest
from unittest.mock import patch,AsyncMock
from datetime import datetime,timezone,timedelta
from infra.monitor import independent_review,audit_input


class IndependentReviewTests(unittest.TestCase):
    def test_agent_has_no_tools_sessions_or_evidence_authority(self):
        captured={};closed=[]
        class Agent:
            def __init__(self,**kwargs):
                captured.update(kwargs);self.tools=[];self._session_db=None
            def close(self):closed.append(True)
            def run_conversation(self,parts,system_message):
                self_parts=parts
                return {'final_response':json.dumps({'classification':'recommendation','verdict':'consistent',
                    'source_items':1,'accepted_items':0,'unresolved_items':1,'reason_codes':['missing_source_period']})}
        gateway=ModuleType('gateway');runtime=ModuleType('gateway.run');agent=ModuleType('run_agent')
        runtime._resolve_runtime_agent_kwargs=lambda:{'provider':'configured-route'}
        runtime._resolve_gateway_model=lambda:'configured-model';agent.AIAgent=Agent
        with patch.dict(sys.modules,{'gateway':gateway,'gateway.run':runtime,'run_agent':agent}):
            value=independent_review.run({'parts':[{'type':'text','text':'private source'}]})
        self.assertEqual([],captured['enabled_toolsets']);self.assertEqual(['all'],captured['disabled_toolsets'])
        self.assertFalse(captured['save_trajectories']);self.assertIsNone(captured['session_db'])
        self.assertEqual(0,value['tool_count']);self.assertFalse(value['persistent_session'])
        self.assertNotIn('private source',json.dumps(value));self.assertEqual([True],closed)


class AuditInputTests(unittest.IsolatedAsyncioTestCase):
    def message(self):
        return {'posted_at':datetime.now(timezone.utc).isoformat(),'traces':[], 'raw_text':'source',
            'media_state':'ready','media_objects':[], 'signals':[], 'reply_context':{}}

    async def test_expired_source_is_never_loaded(self):
        m=self.message();m['posted_at']=(datetime.now(timezone.utc)-timedelta(hours=73)).isoformat()
        with patch.object(audit_input.blobstore,'read_image',AsyncMock())as image:
            with self.assertRaisesRegex(ValueError,'expired_original'):await audit_input.prepare({'message':m})
        image.assert_not_awaited()

    async def test_exact_reply_image_archive_is_read_without_writes(self):
        m=self.message();m['traces']=[{'trace_kind':'hermes_request','payload':{'reply_archive':{
            'media_state':'ready','deleted':False,'media_objects':[{'object_key':'private/reply'}]}}}]
        with patch.object(audit_input.blobstore,'read_image',AsyncMock(return_value=b'image'))as image,\
                patch.object(audit_input.blobstore,'image_mime',return_value='image/png'):
            output=await audit_input.prepare({'message':m})
        self.assertEqual(2,len(output['parts']));image.assert_awaited_once_with('private/reply')
        self.assertEqual(1,json.loads(output['parts'][0]['text'])['reply_image_count'])

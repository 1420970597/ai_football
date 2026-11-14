"""Metadata coverage must agree with the database JSON codec and half-open window."""
import importlib.util
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
import uuid


spec=importlib.util.spec_from_file_location('window_snapshot',Path(__file__).parents[1]/'infra/monitor/window_snapshot.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_json_array_and_full_pagination_are_required(self):
        source,message=uuid.uuid4(),uuid.uuid4()
        conn=SimpleNamespace(set_type_codec=AsyncMock(),close=AsyncMock(),fetchval=AsyncMock(return_value='on'))
        conn.fetch=AsyncMock(side_effect=[[
            {'id':source,'platform_chat_id':'test-peer','trusted':1}],[],[]])
        async def stored(query,source_id,members):
            self.assertEqual(source,source_id)
            self.assertIsInstance(members,list) # codec owns serialization
            self.assertEqual([2],members)
            return {'message_id':message}
        conn.fetchrow=AsyncMock(side_effect=stored)
        pages=[{'connected':True,'authorized':True},
               {'state':'ready','complete':False,'next_cursor':3,'messages':[]},
               {'state':'ready','complete':True,'messages':[{'message_id':2,'kind':'Message',
                   'has_text':True,'has_media':False,'trusted':True}]}]
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def post(self,url,*,json):
                result=pages.pop(0)
                if len(pages)==0:self_outer.assertEqual(3,json['cursor'])
                return SimpleNamespace(raise_for_status=lambda:None,json=lambda:result)
        self_outer=self
        end=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        with patch.dict(os.environ,WINDOW_END=end),patch.object(module,'load_settings',return_value=SimpleNamespace(
                database_url='isolated',collector_token='isolated')),patch.object(module.asyncpg,'connect',AsyncMock(return_value=conn)), patch.object(module.httpx,'AsyncClient',return_value=Client()):
            result=await module.snapshot()
        self.assertEqual(0,result['source_evidence'][0]['missing_messages'])
        self.assertTrue(result['source_evidence'][0]['complete'])
        self.assertTrue(result['database_complete'])
        self.assertEqual('on',result['read_only'])
        conn.close.assert_awaited_once()

    async def test_open_window_cannot_be_exported_as_complete(self):
        with patch.dict(os.environ,WINDOW_END=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()), self.assertRaisesRegex(ValueError,'not closed'):
            await module.snapshot()

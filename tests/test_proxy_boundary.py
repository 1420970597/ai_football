import copy
import unittest
from unittest.mock import AsyncMock

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from infra.config.edge_network import existing_conflicts, validate_edge
from tests.test_config_reuse import ComposeBoundaryTests


class EdgeTopologyTests(unittest.TestCase):
    def fixture(self): return ComposeBoundaryTests().fixture()

    def test_exact_web_ip_and_override_subnet_are_accepted(self):
        document=self.fixture();self.assertEqual([],validate_edge(document))
        document['networks']['edge-private']['ipam']['config'][0]['subnet']='10.79.32.0/28'
        document['services']['web']['networks']['edge-private']['ipv4_address']='10.79.32.3'
        document['services']['api']['networks']['edge-private']['ipv4_address']='10.79.32.4'
        document['services']['api']['environment']['FORWARDED_ALLOW_IPS']='10.79.32.3'
        self.assertEqual([],validate_edge(document))

    def test_wildcard_shared_network_and_overlap_are_rejected(self):
        for value in ('*','172.30.253.0/28','127.0.0.1'):
            document=self.fixture();document['services']['api']['environment']['FORWARDED_ALLOW_IPS']=value
            self.assertTrue(validate_edge(document))
        document=self.fixture();document['services']['collector']['networks']={'edge-private':{}}
        self.assertTrue(validate_edge(document))
        document=self.fixture();document['networks']['app-private']['ipam']={'config':[{'subnet':'172.30.253.0/24'}]}
        self.assertTrue(validate_edge(document))

    def test_invalid_or_gateway_address_is_rejected(self):
        for address in ('172.30.253.0','172.30.253.1','172.30.253.15','10.0.0.2'):
            document=self.fixture();document['services']['web']['networks']['edge-private']['ipv4_address']=address
            self.assertTrue(validate_edge(document))

    def test_daemon_wide_overlap_requires_override_or_exact_existing_project_network(self):
        document=self.fixture()
        other=[{'Name':'another-project','IPAM':{'Config':[{'Subnet':'172.30.0.0/16'}]}}]
        self.assertTrue(existing_conflicts(document,other))
        document['networks']['edge-private']['name']='this-project-edge'
        current=[{'Name':'this-project-edge','IPAM':{'Config':[{'Subnet':'172.30.253.0/28'}]}}]
        self.assertEqual([],existing_conflicts(document,current))
        self.assertEqual([],existing_conflicts(document,[{'Name':'host','IPAM':{'Config':None}}]))


class TrustedProxyTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, peer, forwarded):
        captured=[]
        async def endpoint(scope,receive,send): captured.append(scope['client'][0])
        app=ProxyHeadersMiddleware(endpoint,trusted_hosts='172.30.253.2')
        await app({'type':'http','client':(peer,1234),'scheme':'http',
            'headers':[(b'x-forwarded-for',forwarded.encode()),(b'x-forwarded-proto',b'https')]},AsyncMock(),AsyncMock())
        return captured[0]

    async def test_only_exact_edge_peer_can_supply_client_address(self):
        self.assertEqual('192.0.2.10',await self.client('172.30.253.2','192.0.2.10'))
        self.assertEqual('172.20.0.8',await self.client('172.20.0.8','192.0.2.99'))
        self.assertEqual('172.30.253.9',await self.client('172.30.253.9','192.0.2.99'))
        self.assertNotEqual(await self.client('172.30.253.2','192.0.2.10'),
                            await self.client('172.30.253.2','192.0.2.11'))

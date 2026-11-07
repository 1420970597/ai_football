"""Reject broad proxy trust and ambiguous edge addressing before deployment."""

import ipaddress
import argparse
import json
from pathlib import Path


def existing_conflicts(document: dict, existing: list[dict]) -> list[str]:
    """Report daemon-wide conflicts without connecting or altering networks."""
    edge=document['networks']['edge-private']
    subnet=ipaddress.ip_network(edge['ipam']['config'][0]['subnet'])
    expected_name=edge.get('name')
    errors=[]
    for network in existing:
        for config in network.get('IPAM',{}).get('Config') or []:
            raw=config.get('Subnet')
            if not raw: continue
            other=ipaddress.ip_network(raw)
            if other.version!=subnet.version or not subnet.overlaps(other): continue
            if network.get('Name')==expected_name and other==subnet: continue
            errors.append('边缘子网与现有Docker网络重叠，请同时配置EDGE_SUBNET、EDGE_WEB_IP、EDGE_API_IP')
    return errors


def validate_edge(document: dict) -> list[str]:
    errors=[];services=document.get('services',{});networks=document.get('networks',{})
    edge=networks.get('edge-private',{})
    try:
        configs=edge['ipam']['config']
        if len(configs)!=1: raise ValueError()
        subnet=ipaddress.ip_network(configs[0]['subnet'],strict=True)
        if subnet.version!=4 or not subnet.is_private or not 24<=subnet.prefixlen<=29:
            raise ValueError()
        web=services['web']['networks'];address=ipaddress.ip_address(web['edge-private']['ipv4_address'])
        if address not in subnet or address in {subnet.network_address,subnet.broadcast_address,subnet.network_address+1}:
            raise ValueError()
        api_address=ipaddress.ip_address(services['api']['networks']['edge-private']['ipv4_address'])
        if api_address not in subnet or api_address in {address,subnet.network_address,subnet.broadcast_address,subnet.network_address+1}:
            raise ValueError()
    except (KeyError,TypeError,ValueError):
        return ['edge-private必须配置私有IPv4小网段及非网关Web固定地址']
    if not edge.get('internal'): errors.append('edge-private必须为内部网络')
    for name,service in services.items():
        attached=service.get('networks',{})
        if ('edge-private' in attached)!=(name in {'web','api'}):
            errors.append(f'{name}边缘网络归属不符合仅Web/API契约')
    if set(services['web'].get('networks',{}))-{'edge-private','egress'}:
        errors.append('Web不得连接业务或存储网络')
    trusted=services.get('api',{}).get('environment',{}).get('FORWARDED_ALLOW_IPS')
    if trusted!=str(address): errors.append('API只可信任边缘Web固定IP，禁止通配或整个业务子网')
    for name,network in networks.items():
        if name=='edge-private': continue
        for config in network.get('ipam',{}).get('config',[]):
            try:
                other=ipaddress.ip_network(config['subnet'])
                if subnet.overlaps(other): errors.append(f'edge-private与{name}子网重叠')
            except (KeyError,TypeError,ValueError):
                errors.append(f'{name}子网无效')
    return errors


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('compose',type=Path);parser.add_argument('existing',type=Path)
    args=parser.parse_args()
    document=json.loads(args.compose.read_text())
    errors=validate_edge(document)
    if not errors: errors=existing_conflicts(document,json.loads(args.existing.read_text()))
    for error in errors: print(error)
    print('边缘网络预检通过' if not errors else '边缘网络预检失败')
    raise SystemExit(bool(errors))

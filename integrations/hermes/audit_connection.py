"""Resolve the administrator's connection inside the isolated audit Hermes."""

import os

import httpx

from connection import connection_cipher, open_connection


SNAPSHOT_URL = 'http://hermes:8642/project/connection/audit-snapshot'


def runtime():
    token = os.environ['HERMES_AUDIT_SNAPSHOT_TOKEN']
    cipher = connection_cipher(token)
    with httpx.Client(timeout=5, trust_env=False) as client:
        response = client.get(SNAPSHOT_URL, headers={'X-Hermes-Audit-Token': token})
        response.raise_for_status()
    active = open_connection(cipher, response.json()['sealed'])
    if active.revision < 1:
        raise ValueError('managed audit connection is unavailable')
    return active.model, active.runtime(), active.revision

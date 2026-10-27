"""一致性PostgreSQL逻辑备份及媒体恢复；包内数据分块加密，目标严格隔离。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import re
import time
from typing import Protocol
from urllib.parse import urlsplit
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHUNK_SIZE=1024*1024
MAX_MEDIA_BYTES=8*1024*1024
MAX_MANIFEST_BYTES=8*1024*1024


class RecoveryError(RuntimeError):
    pass


class ObjectStore(Protocol):
    async def get(self,key:str)->bytes: ...
    async def put(self,key:str,body:bytes,mime:str)->None: ...


def validate_isolated_dsn(value:str)->None:
    parts=urlsplit(value)
    if (parts.scheme not in {'postgres','postgresql'} or parts.hostname not in {'ai-football-v2-pgtest','recovery-postgres'}
            or parts.username!='recovery_test' or not re.fullmatch(r'/recovery_[a-z0-9_]+_test',parts.path)):
        raise RecoveryError('only_explicit_isolated_recovery_database_allowed')


def identifier(value):
    if not re.fullmatch(r'v2_[a-z0-9_]+',value):
        raise RecoveryError('unapproved_table_identifier')
    return '"'+value+'"'


def canonical(value)->bytes:
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()


async def isolated_connection(conn):
    row=await conn.fetchrow('SELECT current_user AS role,current_database() AS name')
    if row['role']!='recovery_test' or not re.fullmatch(r'recovery_[a-z0-9_]+_test',row['name']):
        raise RecoveryError('only_explicit_isolated_recovery_database_allowed')


async def empty_target(conn,names):
    for name in names:
        if name=='v2_schema_migrations':
            continue
        count=await conn.fetchval('SELECT count(*) FROM '+identifier(name))
        if name=='v2_membership_plans' and count:
            count=await conn.fetchval("SELECT count(*) FROM v2_membership_plans WHERE id<>'5dcb2545-dcce-4d22-b226-7ccf16157c01'::uuid OR code<>'free'")
        if name=='v2_configuration_revisions' and count:
            count=await conn.fetchval("SELECT count(*) FROM v2_configuration_revisions WHERE namespace<>'leyu' OR revision<>1")
        if count:
            raise RecoveryError('restore_target_contains_business_data')


def digest(value:bytes)->str:
    return hashlib.sha256(value).hexdigest()


async def schema(conn):
    names=await conn.fetch("SELECT tablename AS name FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'v2\\_%' ESCAPE '\\' ORDER BY tablename")
    if not names:
        raise RecoveryError('platform_schema_missing')
    result=[]
    for row in names:
        name=row['name']
        identifier(name)
        columns=await conn.fetch("SELECT a.attname AS name,format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull AS required,"
            "pg_get_expr(d.adbin,d.adrelid) AS default_value FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
            "WHERE a.attrelid=$1::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",name)
        constraints=await conn.fetch("SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid=$1::regclass ORDER BY pg_get_constraintdef(oid)",name)
        indexes=await conn.fetch('SELECT indexdef FROM pg_indexes WHERE schemaname=\'public\' AND tablename=$1 ORDER BY indexdef',name)
        result.append({'name':name,'columns':[dict(item) for item in columns],'constraints':[item['definition'] for item in constraints],
                       'indexes':[item['indexdef'] for item in indexes]})
    return result


async def insertion_order(conn,names):
    allowed=set(names)
    dependencies={name:set() for name in names}
    rows=await conn.fetch("SELECT conrelid::regclass::text AS child,confrelid::regclass::text AS parent FROM pg_constraint WHERE contype='f' AND connamespace='public'::regnamespace")
    for row in rows:
        if row['child'] in allowed and row['parent']!=row['child']:
            if row['parent'] not in allowed:
                raise RecoveryError('external_table_dependency')
            dependencies[row['child']].add(row['parent'])
    result=[]
    while dependencies:
        ready=sorted(name for name,parents in dependencies.items() if not parents)
        if not ready:
            raise RecoveryError('cyclic_foreign_key_requires_explicit_strategy')
        result.extend(ready)
        for name in ready:
            del dependencies[name]
        for parents in dependencies.values():
            parents.difference_update(ready)
    return result


async def audit_inventory(conn):
    summary={}
    for name,query in {
        'messages':"SELECT count(*) FROM v2_expert_messages",
        'signals':"SELECT count(*) FROM v2_expert_signals",
        'pending_signals':"SELECT count(*) FROM v2_expert_signals WHERE outcome='pending'",
        'settled_signals':"SELECT count(*) FROM v2_expert_signals WHERE outcome<>'pending'",
        'unit_net':"SELECT COALESCE(sum(unit_net),0)::text FROM v2_expert_signals WHERE outcome<>'pending'",
        'orders':"SELECT count(*) FROM v2_bet_orders",
        'order_stakes':"SELECT COALESCE(sum(stake),0)::text FROM v2_bet_orders",
        'reserved_stakes':"SELECT COALESCE(sum(reserved_stake),0)::text FROM v2_bet_orders",
        'actual_settled_net':"SELECT COALESCE(sum(settled_net),0)::text FROM v2_bet_orders WHERE state='settled'",
        'signals_without_quote_evidence':"SELECT count(*) FROM v2_expert_signals s WHERE NOT EXISTS(SELECT 1 FROM v2_quote_evidence e WHERE e.evidence_kind='expert_signal' AND e.reference_id=s.id AND e.fixture_id=s.fixture_id AND e.market_id=s.market_id AND e.period=s.period AND e.line=s.line AND e.selection=s.selection AND e.price=s.recommendation_price)",
        'settled_signals_without_ledger':"SELECT count(*) FROM v2_expert_signals s WHERE s.outcome<>'pending' AND NOT EXISTS(SELECT 1 FROM v2_signal_settlements x WHERE x.signal_id=s.id AND x.result_version=s.result_version AND x.outcome=s.outcome AND x.unit_net IS NOT DISTINCT FROM s.unit_net)",
        'audit_entries':"SELECT count(*) FROM v2_audit_log",
        'users':"SELECT count(*) FROM v2_users",
    }.items():
        summary[name]=await conn.fetchval(query)
    return summary


async def media_references(conn):
    references={}
    for table in ('v2_expert_messages','v2_telegram_events'):
        async for row in conn.cursor('SELECT media_objects FROM '+identifier(table)):
            values=json.loads(row['media_objects']) if isinstance(row['media_objects'],str) else row['media_objects']
            if not isinstance(values,list):
                raise RecoveryError('invalid_media_index')
            for item in values:
                if not isinstance(item,dict):
                    raise RecoveryError('invalid_media_reference')
                key,checksum=item.get('object_key'),item.get('sha256')
                if (not isinstance(key,str) or not key or key.startswith('/') or '..' in key.split('/')
                        or not isinstance(checksum,str) or not re.fullmatch('[a-f0-9]{64}',checksum)):
                    raise RecoveryError('invalid_media_reference')
                previous=references.get(key)
                if previous and (previous['sha256']!=checksum or previous['mime']!=item.get('mime_type','application/octet-stream')):
                    raise RecoveryError('conflicting_media_hash')
                references[key]={'key':key,'sha256':checksum,'mime':item.get('mime_type','application/octet-stream')}
    # COPY restores heap rows in a different physical order on some plans.
    # Reference identity is the object key, never the cursor/heap order.
    return [references[key] for key in sorted(references)]


class EncryptedWriter:
    def __init__(self,archive,cipher,entry):
        self.archive,self.cipher,self.entry=archive,cipher,entry
        self.chunks=[]
        self.hash=hashlib.sha256()
        self.bytes=0

    async def write(self,data):
        for start in range(0,len(data),CHUNK_SIZE):
            chunk=bytes(data[start:start+CHUNK_SIZE])
            name=f"{self.entry}/{len(self.chunks):08}.enc"
            nonce=os.urandom(12)
            self.archive.writestr(name,nonce+self.cipher.encrypt(nonce,chunk,name.encode()))
            self.hash.update(chunk)
            self.bytes+=len(chunk)
            self.chunks.append(name)

    def metadata(self):
        return {'chunks':self.chunks,'sha256':self.hash.hexdigest(),'bytes':self.bytes}


async def create_bundle(conn,stream,key:bytes,objects:ObjectStore,*,data_key_reference:str):
    await isolated_connection(conn)
    if len(key)!=32 or not data_key_reference or len(data_key_reference)>120:
        raise RecoveryError('independent_backup_key_and_data_key_reference_required')
    started=time.monotonic()
    cipher=AESGCM(key)
    async with conn.transaction(isolation='repeatable_read',readonly=True):
        definitions=await schema(conn)
        names=[item['name'] for item in definitions]
        order=await insertion_order(conn,names)
        manifest={'format':'platform-v2-recovery-1','created_at':datetime.now(timezone.utc).isoformat(),
                  'data_key_reference':data_key_reference,'schema':definitions,'schema_sha256':digest(canonical(definitions)),
                  'insertion_order':order,'inventory':await audit_inventory(conn),'tables':{},'media':[]}
        with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_STORED) as archive:
            for name in order:
                writer=EncryptedWriter(archive,cipher,'tables/'+name)
                status=await conn.copy_from_table(name,output=writer.write,format='binary')
                manifest['tables'][name]={**writer.metadata(),'rows':int(status.split()[-1])}
            for index,item in enumerate(await media_references(conn)):
                try:
                    body=await objects.get(item['key'])
                except Exception:
                    raise RecoveryError('referenced_media_unreadable') from None
                if not isinstance(body,bytes) or len(body)>MAX_MEDIA_BYTES or digest(body)!=item['sha256']:
                    raise RecoveryError('referenced_media_hash_or_size_mismatch')
                writer=EncryptedWriter(archive,cipher,f'media/{index:08}')
                await writer.write(body)
                manifest['media'].append({**item,**writer.metadata()})
            manifest['elapsed_seconds']=round(time.monotonic()-started,6)
            raw=canonical(manifest)
            if len(raw)>MAX_MANIFEST_BYTES:
                raise RecoveryError('manifest_budget_exceeded')
            nonce=os.urandom(12)
            archive.writestr('manifest.enc',nonce+cipher.encrypt(nonce,raw,b'manifest.enc'))
    return manifest


def load_manifest(archive,cipher):
    try:
        info=archive.getinfo('manifest.enc')
        if info.file_size>MAX_MANIFEST_BYTES+28:
            raise ValueError()
        raw=archive.read(info)
        manifest=json.loads(cipher.decrypt(raw[:12],raw[12:],b'manifest.enc'))
        if manifest['format']!='platform-v2-recovery-1' or digest(canonical(manifest['schema']))!=manifest['schema_sha256']:
            raise ValueError()
        return manifest
    except Exception:
        raise RecoveryError('backup_key_or_manifest_integrity_failure') from None


async def chunks(archive,cipher,metadata):
    checksum=hashlib.sha256()
    size=0
    for name in metadata['chunks']:
        try:
            info=archive.getinfo(name)
            if info.file_size>CHUNK_SIZE+28:
                raise ValueError()
            raw=archive.read(info)
            chunk=cipher.decrypt(raw[:12],raw[12:],name.encode())
        except Exception:
            raise RecoveryError('backup_chunk_integrity_failure') from None
        checksum.update(chunk)
        size+=len(chunk)
        yield chunk
    if size!=metadata['bytes'] or checksum.hexdigest()!=metadata['sha256']:
        raise RecoveryError('backup_content_checksum_failure')


async def restore_bundle(conn,stream,key:bytes,objects:ObjectStore):
    await isolated_connection(conn)
    started=time.monotonic()
    cipher=AESGCM(key)
    with zipfile.ZipFile(stream,'r') as archive:
        manifest=load_manifest(archive,cipher)
        if await schema(conn)!=manifest['schema']:
            raise RecoveryError('restore_schema_differs')
        names=manifest['insertion_order']
        if set(names)!=set(manifest['tables']) or set(names)!={item['name'] for item in manifest['schema']}:
            raise RecoveryError('restore_table_manifest_differs')
        await empty_target(conn,names)
        # 媒体先完整校验。即使后续失败只可能产生隔离目标孤儿对象，不会提交损坏的数据库引用。
        for item in manifest['media']:
            body=b''.join([value async for value in chunks(archive,cipher,item)])
            if len(body)>MAX_MEDIA_BYTES:
                raise RecoveryError('restore_media_budget_exceeded')
            try:
                existing=await objects.get(item['key'])
            except KeyError:
                existing=None
            except Exception:
                raise RecoveryError('target_media_state_unknown') from None
            if existing is not None and digest(existing)!=item['sha256']:
                raise RecoveryError('target_media_conflict_do_not_overwrite')
            if existing is None:
                await objects.put(item['key'],body,item['mime'])
            if digest(await objects.get(item['key']))!=item['sha256']:
                raise RecoveryError('restored_media_hash_mismatch')
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('isolated-platform-recovery'))")
            # 只允许空结构或新迁移产生的固定免费套餐/迁移版本行；绝不覆盖已有业务行。
            await empty_target(conn,names)
            await conn.execute('TRUNCATE '+','.join(identifier(name) for name in names))
            for name in names:
                status=await conn.copy_to_table(name,source=chunks(archive,cipher,manifest['tables'][name]),format='binary')
                if int(status.split()[-1])!=manifest['tables'][name]['rows']:
                    raise RecoveryError('restore_table_count_differs')
            actual=await audit_inventory(conn)
            if actual!=manifest['inventory']:
                raise RecoveryError('restore_financial_or_reference_differs')
            if await media_references(conn)!=[{key:value for key,value in item.items() if key in {'key','sha256','mime'}} for item in manifest['media']]:
                raise RecoveryError('restore_media_index_differs')
        return {'inventory_before':manifest['inventory'],'inventory_after':actual,'differences':{},'media_objects':len(manifest['media']),
                'table_count':len(names),'restore_elapsed_seconds':round(time.monotonic()-started,6),'data_key_reference':manifest['data_key_reference']}

"""仅隔离演练的显式命令入口；不接受生产DSN，不读取环境默认配置。"""

import argparse
import asyncio
import io
import json
import os
from pathlib import Path
import uuid

import asyncpg

from .bundle import RecoveryError,create_bundle,restore_bundle,validate_isolated_dsn
from .legacy_quality import plan
from .operations import cleanup_gate,recovery_plan

ROOT=Path(__file__).resolve().parents[2]


def project_path(value):
    result=Path(value).resolve()
    if not result.is_relative_to(ROOT):
        raise RecoveryError('only_project_paths_allowed')
    return result


class IsolatedObjectDirectory:
    def __init__(self,path):
        self.path=project_path(path)

    def key_path(self,key):
        if (not isinstance(key,str) or not key or key.startswith('/') or chr(92) in key
                or any(part in {'','.', '..'} for part in key.split('/'))
                or any(ord(char)<32 or ord(char)==127 for char in key)):
            raise RecoveryError('invalid_object_reference')
        result=(self.path/key).resolve()
        if not result.is_relative_to(self.path):
            raise RecoveryError('invalid_object_reference')
        return result

    async def get(self,key):
        path=self.key_path(key)
        try:
            if path.stat().st_size>8*1024*1024:
                raise RecoveryError('media_size_exceeded')
            return path.read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None

    async def put(self,key,body,mime):
        path=self.key_path(key)
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with path.open('xb') as output:
            output.write(body)
        path.chmod(0o600)


async def run(args):
    if args.mode in {'classify','plan','cleanup-check'}:
        path=project_path(args.input)
        if path.stat().st_size>16*1024*1024:
            raise RecoveryError('classification_sample_budget_exceeded')
        records=json.loads(path.read_text())
        if args.mode!='classify':
            if not isinstance(records,dict):
                raise RecoveryError('invalid_recovery_plan_document')
            return recovery_plan(records) if args.mode=='plan' else cleanup_gate(records)
        if not isinstance(records,list) or len(records)>10000 or any(not isinstance(item,dict) for item in records):
            raise RecoveryError('classification_sample_budget_exceeded')
        result=plan(records)
        # 命令输出只含分类数量；原文及证据保留在调用者授权的输入中，不打印。
        return {key:value for key,value in result.items() if key!='records'}
    dsn=os.environ.get(args.dsn_env,'')
    validate_isolated_dsn(dsn)
    try:
        key=bytes.fromhex(os.environ.get(args.backup_key_env,''))
    except ValueError:
        raise RecoveryError('invalid_separate_backup_key') from None
    if len(key)!=32:
        raise RecoveryError('invalid_separate_backup_key')
    async def init(conn):
        for name in ('json','jsonb'):
            await conn.set_type_codec(name,schema='pg_catalog',encoder=json.dumps,decoder=json.loads)
    conn=await asyncpg.connect(dsn,timeout=5,command_timeout=120)
    await init(conn)
    try:
        path=project_path(args.bundle)
        objects=IsolatedObjectDirectory(args.objects)
        if args.mode=='backup':
            path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            partial=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.partial')
            try:
                descriptor=os.open(partial,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(descriptor,'wb') as output:
                    async with asyncio.timeout(300):
                        result=await create_bundle(conn,output,key,objects,data_key_reference=args.data_key_reference)
                    output.flush()
                    os.fsync(output.fileno())
                # link is atomic and fails if the destination already exists;
                # os.replace would silently destroy an earlier valid backup.
                os.link(partial,path)
            finally:
                partial.unlink(missing_ok=True)
            return {'inventory':result['inventory'],'tables':len(result['tables']),'media_objects':len(result['media']),
                    'elapsed_seconds':result['elapsed_seconds'],'bundle_written':True}
        with path.open('rb') as source:
            async with asyncio.timeout(300):
                return await restore_bundle(conn,source,key,objects)
    finally:
        await conn.close()


def main():
    parser=argparse.ArgumentParser(description='仅显式隔离恢复与旧专家证据分类，不删除生产数据')
    parser.add_argument('mode',choices=('backup','restore','classify','plan','cleanup-check'))
    parser.add_argument('--dsn-env',default='RECOVERY_TEST_DATABASE_URL')
    parser.add_argument('--backup-key-env',default='RECOVERY_BACKUP_KEY')
    parser.add_argument('--data-key-reference',default='')
    parser.add_argument('--bundle',default='.runtime/recovery/bundle.enc.zip')
    parser.add_argument('--objects',default='.runtime/recovery/objects')
    parser.add_argument('--input',default='.runtime/recovery/legacy-sample.json')
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args())),ensure_ascii=False,sort_keys=True))
    except Exception as exc:
        # 连接异常可能含地址或账户，不回显原异常。
        print(json.dumps({'error':str(exc) if isinstance(exc,RecoveryError) else type(exc).__name__}))
        raise SystemExit(1) from None


if __name__=='__main__':
    main()

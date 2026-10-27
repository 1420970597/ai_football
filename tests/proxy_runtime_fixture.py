"""No-provider HTTP fixture for the isolated Docker proxy boundary test."""
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace

from fastapi import FastAPI, Request

from app import auth, database


@asynccontextmanager
async def lifespan(app):
    await database.open_pool(SimpleNamespace(database_url=os.environ['PROXY_TEST_DATABASE_URL']),migrate=True)
    try: yield
    finally: await database.close_pool()


app=FastAPI(lifespan=lifespan)


@app.get('/api/v1/proxy-probe')
async def probe(request: Request):
    await auth._limit(request,'isolated_proxy_runtime',maximum=1)
    return {'client':request.client.host,'scheme':request.url.scheme}


@app.get('/healthz')
async def health(): return {'state':'healthy'}

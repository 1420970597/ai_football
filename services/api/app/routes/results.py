from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from .. import database
from ..result_ledger import ingest_result, rebuild_settlements
from ..review_state import authorize

router = APIRouter(prefix='/internal/collector')


class ResultInput(BaseModel):
    fixture_id: str = Field(min_length=1, max_length=160)
    provider_reference: str = Field(min_length=1, max_length=240)
    status: Literal['provisional', 'confirmed', 'void']
    home: int | None = Field(default=None, ge=0, le=200, strict=True)
    away: int | None = Field(default=None, ge=0, le=200, strict=True)
    half_home: int | None = Field(default=None, ge=0, le=200, strict=True)
    half_away: int | None = Field(default=None, ge=0, le=200, strict=True)
    source_updated_at: AwareDatetime
    evidence: dict[str, str] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def validate_score(self):
        if self.status == 'confirmed' and (self.home is None or self.away is None):
            raise ValueError('确认赛果必须有双方比分')
        if (self.half_home is None) != (self.half_away is None):
            raise ValueError('半场比分必须成对')
        if self.home is not None and self.half_home is not None and self.half_home > self.home:
            raise ValueError('半场比分大于全场比分')
        if self.away is not None and self.half_away is not None and self.half_away > self.away:
            raise ValueError('半场比分大于全场比分')
        if not self.provider_reference.strip() or any(not k.strip() or not v.strip() or len(k) > 100 or len(v) > 4000 for k, v in self.evidence.items()):
            raise ValueError('赛果来源引用及证据不能为空或过长')
        return self


class ResultsInput(BaseModel):
    items: list[ResultInput] = Field(min_length=1, max_length=50)


@router.post('/results')
async def ingest_results(body: ResultsInput, request: Request):
    authorize(request, request.app.state.settings.collector_token)
    output = []
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            for item in body.items:
                output.append({'fixture_id': item.fixture_id, **await ingest_result(conn, item)})
    return {'items': output}


class RebuildInput(BaseModel):
    fixture_ids: list[str] = Field(min_length=1, max_length=50)

    @model_validator(mode='after')
    def validate_identifiers(self):
        if len(set(self.fixture_ids)) != len(self.fixture_ids) or any(not value.strip() or len(value) > 160 for value in self.fixture_ids):
            raise ValueError('比赛编号不能为空、重复或过长')
        return self


@router.post('/results/rebuild')
async def rebuild_results(body: RebuildInput, request: Request):
    authorize(request, request.app.state.settings.collector_token)
    output = []
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            for fixture_id in body.fixture_ids:
                output.append({'fixture_id': fixture_id, **await rebuild_settlements(conn, fixture_id)})
    return {'items': output}

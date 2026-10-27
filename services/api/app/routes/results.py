from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from .. import database
from ..result_ledger import ingest_result
from ..review_state import authorize

router = APIRouter(prefix='/internal/collector')


class ResultInput(BaseModel):
    fixture_id: str = Field(min_length=1, max_length=160)
    provider_reference: str = Field(min_length=1, max_length=240)
    status: Literal['provisional', 'confirmed', 'void']
    home: int | None = Field(default=None, ge=0, le=200)
    away: int | None = Field(default=None, ge=0, le=200)
    half_home: int | None = Field(default=None, ge=0, le=200)
    half_away: int | None = Field(default=None, ge=0, le=200)
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
        if any(len(k) > 100 or len(v) > 4000 for k, v in self.evidence.items()):
            raise ValueError('赛果证据过长')
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

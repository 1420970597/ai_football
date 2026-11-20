"""Obtain a fresh independent price decision for the exact native offer."""

import asyncio
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import httpx

from .handicap_lines import line_basis
from .score_rules import settlement_rule
from .team_totals import team_total_terms


def mode_for_order(settings, order):
    account = getattr(settings, "automatic_price_account", None)
    if account is not None and order["account_id"] == account and not order["manual"]:
        return getattr(settings, "automatic_price_mode", "enforce")
    return None


def offer_document(signal, price, checked_at, cutoff):
    fixture = {"id": signal["fixture_id"], "sport": signal["sport"]}
    contract = dict(signal, market_kind=signal["current_kind"])
    rule = settlement_rule(fixture, contract)
    basis = (rule or {}).get("score_basis", signal["score_basis"] or "unknown")
    if basis == "unknown" and signal["sport"] == "football" and signal["phase"] == "scheduled":
        basis = "full_period"
    return {
        "fixture_id": signal["fixture_id"], "market_id": signal["market_id"],
        "kind": signal["current_kind"], "sport": signal["sport"], "phase": signal["phase"],
        "period": signal["period"], "selection": signal["selection"],
        "line": str(signal["line"]) if signal["line"] is not None else None,
        "score_baseline": {"home": signal["home_score"], "away": signal["away_score"]},
        "effective_score_basis": basis, "settlement_rule": rule["reference"] if rule else None,
        "line_basis": line_basis(contract), "price": str(price),
        "captured_at": cutoff.isoformat(), "provider_quote_checked_at": checked_at.isoformat(),
    } | team_total_terms(contract, fixture["id"])


async def reference_price(socket, offer):
    transport = httpx.AsyncHTTPTransport(uds=socket)
    async with asyncio.timeout(2), httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.post("http://reference-price/evaluate", json=offer)
        response.raise_for_status()
        return response.json()


async def decision(settings, signal, price, checked_at, cutoff, *, mode="enforce"):
    budget = 0.2 if mode == "observe" else 2
    age = cutoff - checked_at
    if not timedelta(0) <= age <= timedelta(seconds=15):
        return {"status": "quote_expired"}
    if mode == "observe" and age > timedelta(seconds=14.5):
        return {"status": "observation_skipped_quote_deadline"}
    offer = offer_document(signal, price, checked_at, cutoff)
    try:
        async with asyncio.timeout(budget):
            result = await reference_price(settings.automatic_price_socket, offer)
        status = result["status"]
        if status == "ineligible":
            reasons = result.get("reasons", [])
            if not isinstance(reasons, list):
                return {"status": "reference_ineligible"}
            return {"status": "reference_ineligible", "reasons": [
                reason for reason in reasons[:12] if isinstance(reason, str) and reason.isascii()
                and len(reason) <= 70 and reason.replace("_", "").isalnum()
            ]}
        if status not in ("price_eligible", "below_minimum_odds"):
            return {"status": "reference_ineligible"}
        raw_floor = result["pricing"]["minimum_odds"]
        if raw_floor is None:
            return {"status": "reference_ineligible"}
        floor = Decimal(str(raw_floor))
        if not floor.is_finite() or floor <= 1:
            return {"status": "reference_ineligible"}
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, InvalidOperation):
        return {"status": "reference_unavailable"}
    status = "pass" if status == "price_eligible" and price >= floor else "below_minimum_odds"
    return {"status": status, "minimum_odds": str(floor)}

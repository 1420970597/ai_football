"""Native sports and markets supported by the account execution protocol.

Basketball identifiers come from the authorized native market captures E336,
E362 and E2781. Periods must be explicit: a quarter is never a full-time bet.
"""

SPORT_IDS = {"football": "1", "basketball": "2"}
BASKETBALL_MARKETS = {
    "37": ("winner", "full_time"),
    "38": ("total", "full_time"),
    "39": ("handicap", "full_time"),
}


def native_sport_id(sport: str) -> str | None:
    return SPORT_IDS.get(sport)


def native_match_type(phase: str) -> int | None:
    """Normal App single orders: prematch 1, in-play 2; other types excluded."""
    return {"scheduled": 1, "live": 2}.get(phase)


def execution_market_supported(sport: str, market_id: str, kind: str, period: str) -> bool:
    if sport == "basketball":
        return BASKETBALL_MARKETS.get(str(market_id)) == (kind, period)
    return sport == "football" and kind in {"winner", "handicap", "total"} and period in {
        "full_time", "first_half", "second_half",
    }


def native_execution_market_supported(sport_id: str, chpid: str, kind: str, period: str) -> bool:
    sport = next((name for name, value in SPORT_IDS.items() if value == sport_id), None)
    return execution_market_supported(sport, chpid, kind, period)

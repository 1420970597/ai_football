"""Verified native basketball score periods, without inferring overtime rules.

E505 and the 2026-11-10 native final reads contain S19..S22 quarters,
S2/S3 halves and S1 full time. Only their complete, equal totals establish
that an otherwise unknown overtime inclusion rule cannot affect this result.
"""
import re


SCORE_RULE_REFERENCE = "leyu-basketball-fulltime-no-overtime-20261110"
PERIODS = ("S1", "S2", "S3", "S19", "S20", "S21", "S22")


def score_pair(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,2}):(?:0|[1-9][0-9]{0,2})", value) is None:
        raise ValueError("basketball_score_evidence_incomplete")
    pair = tuple(map(int, value.split(":")))
    if max(pair) > 200:
        raise ValueError("basketball_score_evidence_incomplete")
    return pair


def _required_periods(scores) -> dict[str, tuple[int, int]]:
    if not isinstance(scores, list):
        raise ValueError("basketball_score_evidence_incomplete")
    periods = {}
    for period in PERIODS:
        rows = [value for value in scores if isinstance(value, str) and value.startswith(period + "|")]
        if len(rows) != 1:
            raise ValueError("basketball_score_evidence_incomplete")
        periods[period] = score_pair(rows[0].split("|", 1)[1])
    return periods


def _periods_agree(periods) -> bool:
    add = lambda *pairs: tuple(map(sum, zip(*pairs)))
    return (periods["S1"] == add(*(periods[p] for p in PERIODS[3:]))
            and periods["S2"] == add(periods["S19"], periods["S20"])
            and periods["S3"] == add(periods["S21"], periods["S22"])
            and periods["S1"] == add(periods["S2"], periods["S3"]))


def no_overtime_scores(scores) -> dict[str, tuple[int, int]]:
    periods = _required_periods(scores)
    if any(isinstance(value, str) and value.startswith("S7|") for value in scores):
        raise ValueError("basketball_overtime_rule_unverified")
    if not _periods_agree(periods):
        raise ValueError("basketball_overtime_or_period_scores_conflict")
    return periods


def provisional_conflict_scores(scores):
    """Require explicit, well-formed official facts before invalidating a result."""
    periods = _required_periods(scores)
    overtime = [value for value in scores if isinstance(value, str) and value.startswith('S7|')]
    if overtime:
        if len(overtime) != 1:
            raise ValueError('basketball_score_evidence_incomplete')
        periods['S7'] = score_pair(overtime[0].split('|', 1)[1])
        return periods, 'basketball_overtime_rule_unverified'
    if not _periods_agree(periods):
        return periods, 'basketball_overtime_or_period_scores_conflict'
    raise ValueError('basketball_conflict_not_proven')


def _validate_identity(evidence):
    if (not isinstance(evidence, dict) or evidence.get("source") != "leyu_native_detail"
            or evidence.get("path") != "/yewu11/v1/m/matchDetail/getMatchDetailPB"
            or re.fullmatch(r"[0-9]+", str(evidence.get("mid", ""))) is None
            or evidence.get("sport") != "2" or evidence.get("period") != "999"
            or evidence.get("state") not in {"3", "4"} or evidence.get("score_period") != "S1"
            or evidence.get("mft") not in {"0", "7"}
            or re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("decoded_response_sha256", ""))) is None):
        raise ValueError("basketball_final_period_evidence_missing")


def _evidence_scores(home, away, half_home, half_away, evidence):
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (home, away, half_home, half_away)):
        raise ValueError("basketball_score_evidence_incomplete")
    scores = [
        "S1|" + str(evidence.get("full_time", "")),
        "S2|" + str(evidence.get("first_half", "")),
        "S3|" + str(evidence.get("second_half", "")),
        *(period + "|" + str(evidence.get("quarter_" + str(index), ""))
          for index, period in enumerate(PERIODS[3:], 1)),
    ]
    periods = _required_periods(scores)
    if periods["S1"] != (home, away) or periods["S2"] != (half_home, half_away):
        raise ValueError("basketball_period_scores_do_not_match_result")
    return scores


def validate_no_overtime_evidence(home, away, half_home, half_away, evidence) -> None:
    """Validate persisted period evidence again before producing any return."""
    _validate_identity(evidence)
    if evidence.get('score_scope') != 'full_time_no_overtime' or evidence.get('overtime') != 'none_verified':
        raise ValueError('basketball_final_period_evidence_missing')
    no_overtime_scores(_evidence_scores(home, away, half_home, half_away, evidence))


def validate_provisional_evidence(home, away, half_home, half_away, evidence) -> None:
    _validate_identity(evidence)
    if evidence.get('score_scope') != 'full_time_unverified':
        raise ValueError('basketball_final_conflict_evidence_missing')
    scores = _evidence_scores(home, away, half_home, half_away, evidence)
    overtime = evidence.get('overtime', '')
    if overtime.startswith('observed:'):
        scores.append('S7|' + overtime.removeprefix('observed:'))
    elif overtime != 'unverified':
        raise ValueError('basketball_final_conflict_evidence_missing')
    _, reason = provisional_conflict_scores(scores)
    if reason != evidence.get('conflict_reason'):
        raise ValueError('basketball_final_conflict_evidence_missing')

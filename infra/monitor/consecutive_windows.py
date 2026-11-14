"""Strict, evidence-bound acceptance of three live half-hour windows."""
from __future__ import annotations

PERIOD = 1800


def evaluate(evidence: dict) -> tuple[bool, list[str]]:
    reasons = []
    if evidence.get('end', 0)-evidence.get('start', 0) != PERIOD:
        reasons.append('invalid_window')
    for key in ('upstream_complete', 'database_complete', 'source_health_ok', 'audit_complete'):
        if evidence.get(key) is not True:
            reasons.append(key)
    count = evidence.get('trusted_messages')
    if type(count) is not int or count <= 0:
        reasons.append('empty_window')
    # An ad-only window cannot demonstrate recommendation interpretation.
    if type(evidence.get('recommendation_messages')) is not int or evidence['recommendation_messages'] <= 0:
        reasons.append('no_recommendation_sample')
    for key in ('missing_messages', 'unreviewed_messages', 'confirmed_omissions',
                'confirmed_misbindings', 'duplicate_signals', 'deadline_violations',
                'unexplained_outcomes'):
        if type(evidence.get(key)) is not int or evidence[key] != 0:
            reasons.append(key)
    if not evidence.get('snapshot_sha256') or not evidence.get('audit_sha256'):
        reasons.append('evidence_not_bound')
    return not reasons, reasons


def record(state: dict, evidence: dict) -> dict:
    """Do not overwrite failed windows or accept out-of-order/repeated claims."""
    state = dict(state)
    windows = dict(state.get('windows', {}))
    start, end = evidence['start'], evidence['end']
    key = str(start)
    if key in windows:
        if windows[key]['snapshot_sha256'] != evidence.get('snapshot_sha256'):
            raise ValueError('Recorded evidence is immutable')
        return state
    previous = state.get('last_end')
    if previous is not None and start < previous:
        raise ValueError('Cannot import historical windows into the streak')
    passed, reasons = evaluate(evidence)
    streak = state.get('streak', 0) if previous == start else 0
    streak = streak+1 if passed else 0
    windows[key] = dict(evidence, passed=passed, reasons=reasons)
    state.update(windows=windows, last_end=end, streak=streak, complete=streak >= 3)
    return state


def repaired(state: dict, *, revision: str, at: int) -> dict:
    return dict(state, streak=0, complete=False, last_end=None,
                repair_revision=revision, repair_at=at)

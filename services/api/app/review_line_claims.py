"""Reject a final explanation contradicting its own explicit split handicap.

This only detects arithmetic inconsistencies; it never supplies a source intent,
team direction, native line, quote, or permission to record a recommendation.
"""
from decimal import Decimal
import re
import unicodedata

from .review_line_intent import evidence_line_intent
from .review_phase_claims import single_fixture_id

_NUMBER = r'(?:\d{1,3}(?:\.\d{1,3})?|\.\d{1,3})'
_SPLIT = re.compile(rf'(?:[+-]|受让|受讓|让|讓)\s*{_NUMBER}\s*/\s*[+-]?\s*{_NUMBER}')
_DERIVED = re.compile(rf'(?:对应|即|换算为|也就是)[^。；;\n]{{0,24}}?'
                      rf'(?:受让|受讓|让|讓)\s*[+-]?{_NUMBER}(?![\d./])')
_CORRECTION = re.compile(r'不是|并非|而非|不应|错误|误写|应为|纠正')


def split_line_claim_conflicts(content: str) -> list[dict[str, str]]:
    conflicts = []
    fenced = False
    for raw in content[:20000].splitlines():
        line = unicodedata.normalize('NFKC', raw).strip().replace('−', '-')
        if line.startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced or line.startswith(('>', '原文引用', '引用原文')):
            continue
        fixture = single_fixture_id(line)
        if fixture is None:
            continue
        for match in _DERIVED.finditer(line):
            start = max(line.rfind(mark, 0, match.start()) for mark in ('。', '；', ';')) + 1
            prefix = line[max(start, match.start()-250):match.start()]
            end = re.search(r'[。；;]', line[match.end():])
            statement = line[start:match.end()+(end.start() if end else len(line)-match.end())]
            if _CORRECTION.search(statement) or len(_SPLIT.findall(prefix)) != 1:
                continue
            source = evidence_line_intent(prefix, 'handicap')
            derived = evidence_line_intent(match[0], 'handicap')
            if not source or not derived:
                continue
            expected = abs(Decimal(source['selection_line']))
            claimed = abs(Decimal(derived['selection_line']))
            if expected != claimed:
                conflicts.append({'fixture_id': fixture, 'source_line': _SPLIT.search(prefix)[0],
                                  'normalized_magnitude': format(expected, 'f'),
                                  'claimed_magnitude': format(claimed, 'f')})
    return conflicts

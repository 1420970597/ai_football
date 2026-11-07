"""Conservative character folding, not a club alias or translation service."""
from __future__ import annotations

import hashlib
from pathlib import Path


_DATA = Path(__file__).parent / 'data' / 'opencc' / 'TSCharacters.txt'
_SHA256 = '6b5a0a799bea2bb22c001f635eaa3fc2904310f0c08addbff275477a80ecf09a'
_raw = _DATA.read_bytes()
if hashlib.sha256(_raw).hexdigest() != _SHA256:
    raise RuntimeError('Chinese name character dictionary checksum mismatch')

_characters: dict[str, str] = {}
for _line in _raw.decode('utf-8').splitlines():
    if not _line or _line.startswith('#'):
        continue
    _source, _targets = _line.split('\t')
    # Context-dependent or one-to-many conversions require other evidence.
    if len(_source) == len(_targets) == 1 and _source != _targets:
        _characters[_source] = _targets


def _canonical_character(value: str) -> str:
    seen = set()
    while value in _characters:
        if value in seen:
            raise RuntimeError('Chinese name character dictionary contains a cycle')
        seen.add(value)
        value = _characters[value]
    return value


# Some unambiguous entries form chains (薴 -> 苧 -> 苎). A canonical
# endpoint keeps repeated Python normalization and SQL folding consistent.
_characters = {source: _canonical_character(target) for source, target in _characters.items()}
_translation = str.maketrans(_characters)


def fold_chinese(value: str) -> str:
    return value.translate(_translation)


def sql_chinese_fold(value: str) -> tuple[str, str]:
    """Only send equivalents relevant to this query to PostgreSQL translate."""
    wanted = set(fold_chinese(value))
    relevant = sorted((source, target) for source, target in _characters.items() if target in wanted)
    return ''.join(source for source, _ in relevant), ''.join(target for _, target in relevant)

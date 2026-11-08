from __future__ import annotations

import re
import unicodedata

from .chinese_names import fold_chinese
from .verified_team_names import REGISTRY_VERSION, VERIFIED_TEAM_NAMES


_TOKEN_ALIASES = {
    "ath": "athletic",
    "st": "saint",
    "ste": "sainte",
    "utd": "united",
}
_PHRASE_ALIASES = {
    "atleti": "atletico madrid",
    "barca": "barcelona",
    "man city": "manchester city",
    "man united": "manchester united",
    "man utd": "manchester united",
    "psg": "paris saint germain",
    "spurs": "tottenham hotspur",
}
_CLUB_MARKERS = {"ac", "afc", "cf", "club", "fc", "fk", "sc", "sk", "ud"}
_GENERIC_PREFIXES = {"athletic", "city", "club", "inter", "real", "sporting", "united"}


def normalize_team_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", fold_chinese(unicodedata.normalize("NFKC", value).casefold()))
    chars = []
    for char in folded:
        if unicodedata.category(char).startswith("M"):
            continue
        chars.append(char if char.isalnum() else " ")
    return " ".join("".join(chars).split())


def _verified_group(value: str, sport: str | None):
    normalized = normalize_team_name(value)
    for group in VERIFIED_TEAM_NAMES:
        if sport == group['sport'] and normalized in {
                normalize_team_name(name) for name in group['names']}:
            return group
    return None


def with_verified_aliases(fixture: dict) -> dict:
    """Derive an auditable response without rewriting the immutable source row."""
    result = dict(fixture)
    sources = {}
    for side in ('home', 'away'):
        group = _verified_group(fixture.get(side + '_team', ''), fixture.get('sport'))
        if group:
            existing = list(fixture.get(side + '_aliases') or [])
            result[side + '_aliases'] = list(dict.fromkeys([*existing, *group['names']]))
            sources[side] = group['reference']
    if sources:
        result['verified_alias_sources'] = {'registry_version': REGISTRY_VERSION, **sources}
    return result


def team_search_variants(value: str, *, sport: str | None = None) -> tuple[str, ...]:
    normalized = normalize_team_name(value)
    if not normalized:
        return ()
    variants = {normalized}
    group = _verified_group(value, sport)
    if group:
        variants.update(normalize_team_name(name) for name in group['names'])
    phrase = _PHRASE_ALIASES.get(normalized)
    if phrase:
        variants.add(phrase)
    tokens = [_TOKEN_ALIASES.get(token, token) for token in normalized.split()]
    variants.add(" ".join(tokens))
    if tokens and tokens[0] == "man" and len(tokens) == 2 and tokens[1] in {"city", "united"}:
        variants.add("manchester " + tokens[1])
    return tuple(sorted(variants))


def verified_team_search_variants(value: str, *, sport: str | None = None) -> dict[str, tuple[str, ...]]:
    """Keep verified retrieval vocabulary tied to the candidate's actual sport."""
    normalized = normalize_team_name(value)
    variants = {}
    for group in VERIFIED_TEAM_NAMES:
        names = {normalize_team_name(name) for name in group['names']}
        if normalized in names and (sport is None or sport == group['sport']):
            variants.setdefault(group['sport'], set()).update(names)
    return {key: tuple(sorted(values)) for key, values in variants.items()}


def _club_marker_variants(value: str) -> set[str]:
    tokens = value.split()
    variants = {value}
    start, end = 0, len(tokens)
    while start < end and tokens[start] in _CLUB_MARKERS:
        start += 1
    while end > start and tokens[end - 1] in _CLUB_MARKERS:
        end -= 1
    if start < end and (start or end < len(tokens)):
        variants.add(" ".join(tokens[start:end]))
    return variants


def _name_forms(value: str, *, sport: str | None = None) -> set[str]:
    normalized = normalize_team_name(value)
    if not normalized:
        return set()
    forms = set(team_search_variants(normalized, sport=sport))
    forms.update(" ".join(_TOKEN_ALIASES.get(token, token) for token in form.split())
                 for form in tuple(forms))
    expanded = set(forms)
    for form in forms:
        expanded.update(_club_marker_variants(form))
    # Mixed-script source labels may insert layout spacing around Chinese
    # characters ("CD 印加" / "CD印加"). Compare the entire identity after
    # removing only those boundaries; keep Latin word and squad boundaries.
    expanded.update(re.sub(r'(?<=[\u3400-\u9fff]) +| +(?=[\u3400-\u9fff])', '', form)
                    for form in tuple(expanded))
    return expanded


def _prefix_match(query: str, candidate: str) -> bool:
    # Latin abbreviation matching must not treat a Chinese club name as a
    # prefix of its women's/youth/reserve team identity.
    if any('\u3400' <= char <= '\u9fff' for char in query + candidate):
        return False
    query_tokens = query.split()
    candidate_tokens = candidate.split()
    if not query_tokens or len(query_tokens) > len(candidate_tokens):
        return False
    if len(query_tokens) == 1:
        token = query_tokens[0]
        if len(token) < 6 or token in _GENERIC_PREFIXES:
            return False
    if any(len(token) < 3 for token in query_tokens):
        return False
    return all(candidate_token.startswith(query_token)
               for query_token, candidate_token in zip(query_tokens, candidate_tokens))


def team_name_matches(query: str, primary_name: str, aliases=(), *, sport: str | None = None) -> bool:
    query_forms = _name_forms(query, sport=sport)
    if not query_forms:
        return False
    candidate_forms: set[str] = set()
    for value in (primary_name, *aliases):
        if isinstance(value, str):
            candidate_forms.update(_name_forms(value, sport=sport))
    if query_forms & candidate_forms:
        return True
    return any(_prefix_match(query_form, candidate_form)
               for query_form in query_forms for candidate_form in candidate_forms)

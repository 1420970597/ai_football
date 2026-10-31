from __future__ import annotations

import unicodedata


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
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", value).casefold())
    chars = []
    for char in folded:
        if unicodedata.category(char).startswith("M"):
            continue
        chars.append(char if char.isalnum() else " ")
    return " ".join("".join(chars).split())


def team_search_variants(value: str) -> tuple[str, ...]:
    normalized = normalize_team_name(value)
    if not normalized:
        return ()
    variants = {normalized}
    phrase = _PHRASE_ALIASES.get(normalized)
    if phrase:
        variants.add(phrase)
    tokens = [_TOKEN_ALIASES.get(token, token) for token in normalized.split()]
    variants.add(" ".join(tokens))
    if tokens and tokens[0] == "man" and len(tokens) == 2 and tokens[1] in {"city", "united"}:
        variants.add("manchester " + tokens[1])
    return tuple(sorted(variants))


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


def _name_forms(value: str) -> set[str]:
    normalized = normalize_team_name(value)
    if not normalized:
        return set()
    forms = set(team_search_variants(normalized))
    forms.update(" ".join(_TOKEN_ALIASES.get(token, token) for token in form.split())
                 for form in tuple(forms))
    expanded = set(forms)
    for form in forms:
        expanded.update(_club_marker_variants(form))
    return expanded


def _prefix_match(query: str, candidate: str) -> bool:
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


def team_name_matches(query: str, primary_name: str, aliases=()) -> bool:
    query_forms = _name_forms(query)
    if not query_forms:
        return False
    candidate_forms: set[str] = set()
    for value in (primary_name, *aliases):
        if isinstance(value, str):
            candidate_forms.update(_name_forms(value))
    if query_forms & candidate_forms:
        return True
    return any(_prefix_match(query_form, candidate_form)
               for query_form in query_forms for candidate_form in candidate_forms)

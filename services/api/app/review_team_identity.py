"""Negative source identity guard; candidate vocabulary cannot add a squad."""
import re
import unicodedata


def squad_markers(value):
    value=unicodedata.normalize('NFKC',value).casefold()
    markers=set()
    if re.search(r'后备|後備|预备|預備|(?<![a-z])reserves?(?![a-z])',value):
        markers.add('reserve')
    if re.search(r"女足|女子|女队|女隊|(?<![a-z])women(?:s|'s)?(?![a-z])",value):
        markers.add('women')
    for age in re.findall(r'(?<![a-z0-9])u\s*[-]?\s*(\d{1,2})(?!\d)',value):
        markers.add('u'+age)
    return markers


def added_squad_markers(team_a,team_b,source_text):
    return (squad_markers(team_a)|squad_markers(team_b))-squad_markers(source_text or '')

"""Negative source identity guard; candidate vocabulary cannot add a squad."""
import re
import unicodedata
from .team_matching import compact_chinese_boundaries, normalize_team_name


def squad_markers(value):
    value=unicodedata.normalize('NFKC',value).casefold()
    markers=set()
    if re.search(r'后备|後備|预备|預備|(?<![a-z])reserves?(?![a-z])',value):
        markers.add('reserve')
    if re.search(r"女足|女子|女队|女隊|\(\s*女\s*\)|(?<![a-z])women(?:s|'s)?(?![a-z])",value):
        markers.add('women')
    for age in re.findall(r'(?<![a-z0-9])u\s*[-]?\s*(\d{1,2})(?!\d)',value):
        markers.add('u'+age)
    return markers


def added_squad_markers(team_a,team_b,source_text):
    source_text=re.sub(r'(?<![a-z])vs(?![a-z])',' ',source_text or '',flags=re.I)
    source_text=compact_chinese_boundaries(normalize_team_name(source_text))
    result=set()
    for team in (team_a,team_b):
        markers=squad_markers(team)
        if not markers:continue
        # A qualifier on the opposite team or in a league heading cannot
        # supply this team's identity. Only the actual full source name clears
        # this narrow guard; visual aliases remain the model's separate work.
        source_name=compact_chinese_boundaries(normalize_team_name(team))
        left=r'(?<![a-z0-9])' if re.match(r'[a-z0-9]',source_name) else ''
        right=r'(?![a-z0-9])' if re.search(r'[a-z0-9]$',source_name) else ''
        if not re.search(left+re.escape(source_name)+right,source_text):
            result.update(markers)
    return result

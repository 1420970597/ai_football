"""Negative source identity guard; candidate vocabulary cannot add a squad."""
import re
import unicodedata
from .team_matching import compact_chinese_boundaries, normalize_team_name, team_name_matches


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


def explicit_source_pairs(source_text):
    """Only standalone, fully written VS lines supply negative evidence."""
    pairs=[]
    for line in (source_text or '').splitlines():
        # Mixed selection/prose lines are not a complete negative identity
        # assertion. Do not guess where a team name ends in those formats.
        if re.search(r'全场|半场|全場|半場|推荐|推薦|让球|讓球|大小|[+-]\d|[大小]\s*\d|\b(?:over|under)\b',line,re.I):
            continue
        match=re.fullmatch(r'\s*([^\n:：]{1,160}?)\s+(?:VS|vs|Vs|vS)\s+([^\n:：]{1,160}?)\s*',line)
        if match is None:
            match=re.fullmatch(r'\s*([\u3400-\u9fffA-Za-z0-9()（）· .-]{1,100}?)(?:VS|vs)([\u3400-\u9fffA-Za-z0-9()（）· .-]{1,100}?)\s*',line)
        if match:pairs.append((match[1].strip(),match[2].strip()))
    return pairs


def source_pair_supports_claim(team_a,team_b,pairs,rows):
    """A candidate cannot replace an explicit source name without verified identity."""
    for row in rows:
        fixture=row['candidate_data']
        scope={'sport':fixture.get('sport'),'competition':fixture.get('competition_name')}
        for first,second in pairs:
            if (team_name_matches(first,team_a,**scope)and team_name_matches(second,team_b,**scope)
                    or team_name_matches(first,team_b,**scope)and team_name_matches(second,team_a,**scope)):
                return True
    return False

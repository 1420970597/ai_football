"""Negative caption/claim checks; never infer image contents or authorize a pick."""
import re
import unicodedata

from .review_line_intent import evidence_line_intent

_CAPTION = re.compile(r'\s*(?:[1-9]\d?\s*[xX×*]\s*[1-9]\d?\s*)?可[单單]可串\s*[。!]*\s*')
_TIME_HEADER = re.compile(r'[\w /·()（）-]{1,40}\s+(?:[01]?\d|2[0-3]):[0-5]\d')
_HISTORICAL_HEADER = re.compile(r'昨天|昨日|前天|之前|回顾|回顧|赛果|賽果|收米|反馈|反饋|历史|歷史|取消|延期')

_NUMBER = r'\d{1,3}(?:\.\d{1,3})?(?:\s*/\s*\d{1,3}(?:\.\d{1,3})?)?'
_FOLLOW = re.compile(
    rf'\s*(?:追|跟|上)\s*(?:{_NUMBER}\s*[大小](?:球)?|[大小](?:球)?\s*{_NUMBER})\s*[。!]*\s*')
_RECOMMEND = re.compile(r'^(?P<current>今日|今天|现在|現在|新增|最新)?推[荐薦]\s*[:：]\s*(?P<pick>.+)$')
_PERIOD_TOTAL = re.compile(
    rf'(?:上半场|上半場|下半场|下半場|全场|全場|上半|下半|半场|半場|半)\s*'
    rf'(?:{_NUMBER}\s*[大小](?:球)?|[大小]\s*{_NUMBER}(?:球)?)\s*[。!]*')
_PAST_SECTION = re.compile(
    r'^(?:昨天|昨日|前天|之前|历史|歷史|回顾|回顧|赛果|賽果|战绩|戰績|复盘|復盤|反馈|反饋)')
_NOT_CURRENT = re.compile(
    r'昨天|昨日|前天|之前|回顾|回顧|赛果|賽果|收米|反馈|反饋|取消|撤回|延期|已赢|已贏|已输|已輸|[✅❌]')
_QUESTION = re.compile(r'[?？]|可以[吗嗎]|怎[么麼]看|是否|要不要')


def explicit_text_pick(content: str) -> bool:
    """Request tool verification, never extract picks or infer match identity.

    Deliberately conservative: clear recommendation labels or a whole period
    total line count; historical sections require an explicit current label.
    The existing whole-message follow instruction remains supported.
    """
    if not isinstance(content, str) or len(content) > 20000:
        return False
    text = unicodedata.normalize('NFKC', content)
    if len(text) <= 100 and _FOLLOW.fullmatch(text):
        return True
    historical = fenced = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced or not line or len(line) > 500 or line.startswith(('>', '原文引用', '引用原文')):
            continue
        recommendation = _RECOMMEND.fullmatch(line)
        if recommendation and recommendation['current']:
            historical = False
        elif _PAST_SECTION.search(line):
            historical = True
        if historical or _NOT_CURRENT.search(line) or _QUESTION.search(line):
            continue
        if recommendation:
            pick = recommendation['pick']
            if evidence_line_intent(pick, 'handicap') or evidence_line_intent(pick, 'total'):
                return True
        elif _PERIOD_TOTAL.fullmatch(line) and evidence_line_intent(line, 'total'):
            return True
    return False


def explicit_visual_instruction(content: str, has_image: bool) -> bool:
    """Only a short, whole, current single/parlay instruction requests review."""
    if not has_image or not isinstance(content, str) or len(content) > 100:
        return False
    text = unicodedata.normalize('NFKC', content)
    if _CAPTION.fullmatch(text):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Time headings only permit checking the explicit final directive; they
    # provide no fixture, league, kickoff, image selection or market evidence.
    return (2 <= len(lines) <= 4 and bool(_CAPTION.fullmatch(lines[-1]))
            and all(_TIME_HEADER.fullmatch(line) and not _HISTORICAL_HEADER.search(line)
                    for line in lines[:-1]))


_PICK = r'(?:新增|新的)推[荐薦](?!信号|信號|记录|記錄|可录入|可錄入|可投注|已接受)'
_DENIAL = re.compile(
    rf'(?:没有|沒有|无|無|不构成|不構成|不属于|不屬於)\s*{_PICK}'
    rf'|不把[^。；;]{{0,16}}(?:当作|當作|视为|視為)\s*{_PICK}'
    r'|(?:没有|沒有|无|無)\s*新的跟[进進]指令')
_CORRECTION = re.compile(
    r'(?:不能|不应|不應|不该|不該)[^。；;]{0,40}(?:认定|認定|认为|認為|说|說|称|稱|判定)'
    r'|(?:说法|說法|判断|判斷)[^。；;]{0,8}(?:错误|錯誤|不对|不對)')
_NEGATED_DENIAL = re.compile(r'(?:并非|並非|不是|不等于|不等於|不代表)\s*$')


def source_intent_denial(content: str) -> bool:
    """A current explicit denial, excluding quotations and reported corrections."""
    fenced = False
    for raw in content[:20000].splitlines():
        line = unicodedata.normalize('NFKC', raw).strip()
        if line.startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced or line.startswith(('>', '原文引用', '引用原文', '上轮说明', '上輪說明')):
            continue
        line = re.sub(r'“[^”]*”|「[^」]*」|"[^"]*"|`[^`]*`', '', line)
        for clause in re.split(r'[。；;]', line):
            if not _CORRECTION.search(clause):
                for match in _DENIAL.finditer(clause):
                    if not _NEGATED_DENIAL.search(clause[:match.start()]):
                        return True
    return False

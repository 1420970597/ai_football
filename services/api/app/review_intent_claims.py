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
_CURRENT_TAIL = re.compile(r'\s*等水上[车車]\s*[。!！]*$')
_IMAGE_TOTAL = re.compile(rf'[大小]\s*{_NUMBER}(?:\s+{_NUMBER})?\s*[。!！]*')
_FORM_TOTAL = re.compile(
    rf'^(?:足球|篮球|籃球)\s*[（(](?:滚球|滾球|初盘|初盤|赛前|賽前)[）)]\s*'
    rf'大\s*/\s*小\s+(?:全场|全場|上半场|上半場|下半场|下半場)[大小]\s*{_NUMBER}\s*$')
_FORM_HANDICAP = re.compile(
    rf'^(?:足球|篮球|籃球)\s*[（(](?:滚球|滾球|初盘|初盤|赛前|賽前)[）)]\s*'
    rf'(?:让球|讓球)\s+[主客]\s*[+\-]\s*{_NUMBER}\s*$')


def bare_total_source_notice(content: str, *, has_image: bool = False) -> str:
    """Describe missing caption facts; never classify a picture or choose a pick."""
    if not has_image or not isinstance(content, str) or len(content) > 100:
        return ''
    text = unicodedata.normalize('NFKC', content).strip()
    if not _IMAGE_TOTAL.fullmatch(text) or not evidence_line_intent(text, 'total'):
        return ''
    return (
        '来源期间校验提示（仅负向约束，不证明任何图片内容）: '
        '本条正文仅有大小方向及数字，没有期间或完整赛前对阵。'
        '先核验全部原图和可读回复：若图片仅为带分钟、比分的滚球比分条，'
        '不能套用赛前完整推荐清单的整场约定；比赛正在上半场或下半场也不证明投注期间。'
        '只有原图本身确为未开赛的完整对阵推荐清单，才可按该清单语境确认整场。'
        '真实所选图片或可读回复明确期间时继续正常核验；'
        '否则source_periods=[]、record_source_intent=false，不默认全场或按库存补期间。'
        '多个数字的原始表达必须完整保留，不能删除或自行选其中一个；不明项保留未解决。\n'
    )


def _current_pick_lines(content: str):
    """Yield bounded current clauses, excluding history, quotes and questions."""
    if not isinstance(content, str) or len(content) > 20000:
        return
    text = unicodedata.normalize('NFKC', content)
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
        yield _CURRENT_TAIL.sub('', recommendation['pick'] if recommendation else line).strip(), bool(recommendation)


def explicit_text_pick(content: str, *, has_image: bool = False) -> bool:
    """Request tool verification, never infer identity or authorize a pick."""
    if not isinstance(content, str) or len(content) > 20000:
        return False
    text = unicodedata.normalize('NFKC', content)
    if len(text) <= 100 and _FOLLOW.fullmatch(text):
        return True
    for line, recommendation in _current_pick_lines(content):
        if _FORM_TOTAL.fullmatch(line) or _FORM_HANDICAP.fullmatch(line):
            # A strict form row is a negative verification obligation only.
            # Team identity, period, selection and valid quotes still come
            # exclusively from Hermes and the attempt-scoped native tools.
            return True
        if recommendation:
            if evidence_line_intent(line, 'handicap') or evidence_line_intent(line, 'total'):
                return True
        elif _PERIOD_TOTAL.fullmatch(line) and evidence_line_intent(line, 'total'):
            return True
        elif has_image and len(line) <= 100 and _IMAGE_TOTAL.fullmatch(line):
            # Only request verification. An additional number may be an
            # alternative line or price; this check never selects either.
            if evidence_line_intent(line, 'total'):
                return True
    return False


def explicit_current_total_items(content: str) -> list[dict]:
    """Negative review obligations, without fixture identity or recording rights.

    Only whole current period/total clauses count. Bare half, numeric
    alternatives and text mixed with team identities need model interpretation.
    """
    periods = {'全场': 'full_time', '全場': 'full_time',
               '上半场': 'first_half', '上半場': 'first_half', '上半': 'first_half',
               '下半场': 'second_half', '下半場': 'second_half', '下半': 'second_half'}
    items = []
    seen = set()
    for line, _ in _current_pick_lines(content):
        if not _PERIOD_TOTAL.fullmatch(line):
            continue
        period = next((value for prefix, value in periods.items() if line.startswith(prefix)), None)
        intent = evidence_line_intent(line, 'total')
        if not period or not intent or len(intent['lines']) != 1:
            continue
        key = (period, intent['lines'][0], intent['selection'])
        if key in seen:
            continue
        seen.add(key)
        if len(items) == 32:
            return items + [{'reason': 'source_total_item_budget_exceeded'}]
        items.append(dict(period=period, line=key[1], selection=key[2], source_text=line))
    return items


def image_access_unavailable(content: str) -> bool:
    """A current explicit failed-image-reading claim, never an OCR result."""
    if not isinstance(content, str) or len(content) > 20000:
        return False
    fenced = False
    for raw in content.splitlines():
        line = unicodedata.normalize('NFKC', raw).strip()
        if line.startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if (fenced or line.startswith(('>', '原文引用', '引用原文', '上轮说明', '上輪說明'))
                or _PAST_SECTION.search(line)):
            continue
        line = re.sub(r'“[^”]*”|「[^」]*」|"[^"]*"|`[^`]*`', '', line)
        for clause in re.split(r'[。；;]', line):
            if re.search(r'如果|假如|倘若|不能断言|不可断言|并非|並非|不是', clause):
                continue
            if (re.search(r'图片|圖片|图像|圖像|原图|原圖|截图|截圖', clause)
                    and re.search(r'看不清|无法看清|無法看清|无法读取|無法讀取|'
                                  r'无法辨认|無法辨認|无法看到|無法看到|不能读取|不能讀取', clause)):
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

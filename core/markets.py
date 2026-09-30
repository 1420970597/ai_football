"""多玩法市场目录与派生市场生成（零第三方依赖）。

## 为什么需要这一层

报告 §8.1 的核心结论是「去水分歧足以翻转 edge 符号」，而现实里
一场比赛远不止「胜平负」一种玩法。体彩官方单场可售 5 种玩法：

    HAD   胜平负       3 个结果
    HHAD  让球胜平负   3 个结果（含让球盘口）
    TTG   总进球       8 个结果（0/1/.../6/7+）
    CRS   比分         31 个结果（含「其他」桶）
    HAFU  半全场       9 个结果

此外标准赔率源还会提供 1X2 / AH / OU / BTTS / DC 等玩法。

本模块提供两件事：

1. **市场目录（MarketSpec）**：把每种玩法的结果空间、结果标签、
   是否构成完备划分（partition）显式描述出来，使去水、EV、校准
   等下游逻辑对玩法**完全通用**——它们只需要一个概率向量。

2. **派生市场（Poisson 比分模型）**：用两队期望进球 (λ_home, λ_away)
   建立比分分布 P(i, j)，再从中**解析地**导出任意玩法的模型概率。
   这是「模型概率 vs 市场隐含概率」比较的基础——即报告 §9 里
   判断是否存在真实优势的唯一途径。

## 关键概念：完备划分

「完备划分」指该玩法的结果集合互斥且穷尽样本空间，
因此隐含概率之和（booksum）应 ≥ 1，且去水可直接施加于整个向量。
本模块中绝大多数玩法都是完备划分；`ResultSpace.DERIVED_SUBSET`
用于描述「单腿」需求（如只想知道 P(大 2.5)），不直接去水。

## 诚实说明

本模块**不**包含任何采集乐鱼/开云等站点的代码。那些站点是无牌
离岸博彩，其赔率数据不可作为合法数据源；本层设计为**数据源无关**，
任何合法授权的赔率（含体彩官方 API）都可归一化后喂入。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ResultSpace",
    "MarketSpec",
    "MarketGroup",
    "score_matrix",
    "poisson_pmf",
    "market_probs",
    "subset_prob",
    "expected_goals_from_stats",
    "HAD",
    "HHAD",
    "TTG",
    "CRS",
    "HAFU",
    "BTTS",
    "OU",
    "DC",
    "OFFICIAL_SPECS",
    "STANDARD_SPECS",
    "spec_by_code",
    "parse_crs_key",
    "pool_odds_keys",
    "HAD_ODDS_KEYS",
    "TTG_ODDS_KEYS",
    "HAFU_ODDS_KEYS",
    "CRS_OTHER_KEYS",
    "catalog",
    "partition_check",
    "cross_market_marginals",
]

# --------------------------------------------------------------------------- #
# 结果空间类型
# --------------------------------------------------------------------------- #

class ResultSpace(str, Enum):
    """市场结果空间的性质。

    EXACT_PARTITION：结果互斥且穷尽 → 概率和为 1，可直接去水。
    OVERLAPPING    ：结果**互相重叠**（如双重机会 1X/12/X2 中主胜
                     同时属于 1X 与 12）→ 概率和为 2，
                     **不可归一化到 1**，也不可直接整套去水。
    DERIVED_SUBSET ：只是部分结果（单腿），不可直接去水。

    历史教训：曾把双重机会当作 EXACT_PARTITION 处理，
    归一化把所有概率砍了一半（0.705 变成 0.352），
    这类错误不会抛异常，只会静默产生错误的概率。
    """

    EXACT_PARTITION = "exact_partition"
    OVERLAPPING = "overlapping"
    DERIVED_SUBSET = "derived_subset"

    @property
    def is_partition(self) -> bool:
        return self is ResultSpace.EXACT_PARTITION

    @property
    def expected_sum(self) -> float:
        """该结果空间的概率之和应为多少。"""
        if self is ResultSpace.EXACT_PARTITION:
            return 1.0
        if self is ResultSpace.OVERLAPPING:
            return 2.0
        return math.nan        # 子集没有固定总和


# --------------------------------------------------------------------------- #
# 市场规格
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MarketSpec:
    """一种玩法的静态描述。

    Attributes:
        code: 内部规范代码，如 "HHAD(-1)" / "OU(2.5)" / "TTG"。
        name: 中文显示名。
        pool_code: 体彩官方 poolCode（若非官方玩法则为 None）。
        outcomes: 规范结果标签，顺序即概率向量顺序。
        space: 结果空间类型。
        line: 盘口线（让球 / 大小球），无则为 None。
        aliases: 上游可能使用的其它结果标签写法（用于归一化容错）。
        notes: 说明（如「含其他桶」）。
    """

    code: str
    name: str
    outcomes: Tuple[str, ...]
    space: ResultSpace = ResultSpace.EXACT_PARTITION
    pool_code: Optional[str] = None
    line: Optional[float] = None
    aliases: Mapping[str, str] = field(default_factory=dict)
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("MarketSpec.code 不能为空")
        if self.space.is_partition and len(self.outcomes) < 2:
            raise ValueError(
                "完备划分市场至少需要 2 个结果：%s 实际 %d"
                % (self.code, len(self.outcomes))
            )
        if len(set(self.outcomes)) != len(self.outcomes):
            raise ValueError("结果标签重复：%r" % (self.outcomes,))

    @property
    def n_outcomes(self) -> int:
        return len(self.outcomes)

    @property
    def is_partition(self) -> bool:
        return self.space.is_partition

    def index_of(self, outcome: str) -> int:
        """结果标签 → 下标，支持 aliases 容错。"""
        try:
            return self.outcomes.index(outcome)
        except ValueError:
            pass
        canon = self.aliases.get(outcome)
        if canon is not None and canon in self.outcomes:
            return self.outcomes.index(canon)
        raise KeyError(
            "%s 中不存在结果 %r（规范标签：%r）"
            % (self.code, outcome, self.outcomes)
        )

    def as_dict(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "name": self.name,
            "pool_code": self.pool_code,
            "outcomes": list(self.outcomes),
            "n_outcomes": self.n_outcomes,
            "line": self.line,
            "space": self.space.value,
            "is_partition": self.is_partition,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class MarketGroup:
    """一个盘口线下的成对市场（如 OU2.5 的 over/under）。

    之所以需要「组」的概念：大小球、让球这类玩法，
    单腿（只买大 2.5）不构成完备划分，必须成对才可去水；
    而组内各腿共享同一条概率分布，天然可做一致性校验。
    """

    name: str
    line: float
    members: Tuple[MarketSpec, ...]

    def __post_init__(self) -> None:
        if not self.members:
            raise ValueError("MarketGroup 至少需要一个成员")
        if any(m.line != self.line for m in self.members):
            raise ValueError("组内成员的 line 必须一致：%r" % (self.line,))

    @property
    def n_members(self) -> int:
        return len(self.members)


# --------------------------------------------------------------------------- #
# Poisson 比分模型
# --------------------------------------------------------------------------- #

_MAX_GOALS = 15   # 比分矩阵截断；λ 通常 < 4，尾部概率可忽略（< 1e-9）


def poisson_pmf(k: int, lam: float) -> float:
    """泊松概率质量函数 P(K = k)，用 lgamma 保证大 k 数值稳定。"""
    if k < 0:
        raise ValueError("k 不能为负：%r" % k)
    if lam < 0:
        raise ValueError("lam 不能为负：%r" % lam)
    if lam == 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def score_matrix(
    lam_home: float,
    lam_away: float,
    max_goals: int = _MAX_GOALS,
) -> Tuple[Tuple[float, ...], ...]:
    """独立泊松比分矩阵 M[i][j] = P(主队 i 球, 客队 j 球)。

    采用两队进球独立的经典假设（Dixon-Coles 的 ρ 修正未实现，
    见模块说明与技术债台账）。矩阵总量略小于 1（尾部截断），
    下游取用时应以**未归一化**的概率做相对比较，或显式归一化。
    """
    if lam_home < 0 or lam_away < 0:
        raise ValueError("期望进球不能为负：%r %r" % (lam_home, lam_away))
    if max_goals < 1:
        raise ValueError("max_goals 至少为 1")

    home = [poisson_pmf(i, lam_home) for i in range(max_goals + 1)]
    away = [poisson_pmf(j, lam_away) for j in range(max_goals + 1)]
    return tuple(
        tuple(home[i] * away[j] for j in range(max_goals + 1))
        for i in range(max_goals + 1)
    )


def _normalize(vec: Sequence[float]) -> Tuple[float, ...]:
    """归一化到和为 1；总和为 0 时抛错（不可静默返回假概率）。"""
    total = math.fsum(vec)
    if total <= 0.0:
        raise ValueError("概率向量总和为 0，无法归一化：%r" % (vec,))
    return tuple(v / total for v in vec)


# --------------------------------------------------------------------------- #
# 官方玩法定义（体彩）
# --------------------------------------------------------------------------- #

#: 胜平负
HAD = MarketSpec(
    code="HAD", name="胜平负", pool_code="HAD",
    outcomes=("home", "draw", "away"),
    aliases={"h": "home", "d": "draw", "a": "away",
             "主胜": "home", "平局": "draw", "客胜": "away"},
)

#: 总进球（7+ 为「7 球及以上」）
TTG = MarketSpec(
    code="TTG", name="总进球", pool_code="TTG",
    outcomes=("0", "1", "2", "3", "4", "5", "6", "7+"),
    notes="0–6 为精确球数，7+ 为「7 球及以上」聚合桶",
)

#: 半全场（9 种组合）
HAFU_OUTCOMES = (
    "H/H", "H/D", "H/A",
    "D/H", "D/D", "D/A",
    "A/H", "A/D", "A/A",
)
HAFU = MarketSpec(
    code="HAFU", name="半全场", pool_code="HAFU",
    outcomes=HAFU_OUTCOMES,
    notes="格式：上半场/全场；H 主队领先 D 平 A 客队领先",
)

#: 比分（体彩布局：具体比分 + 三个「其他」桶）
_CRS_HOME = ("1:0", "2:0", "2:1", "3:0", "3:1", "3:2",
             "4:0", "4:1", "4:2", "5:0", "5:1", "5:2")
_CRS_DRAW = ("0:0", "1:1", "2:2", "3:3")
_CRS_AWAY = ("0:1", "0:2", "1:2", "0:3", "1:3", "2:3",
             "0:4", "1:4", "2:4", "0:5", "1:5", "2:5")
_CRS_OTHER = ("胜其他", "平其他", "负其他")
CRS = MarketSpec(
    code="CRS", name="比分", pool_code="CRS",
    outcomes=_CRS_HOME + _CRS_DRAW + _CRS_AWAY + _CRS_OTHER,
    notes="共 31 个结果；「胜其他/平其他/负其他」聚合未列举比分",
)

#: 让球胜平负（带盘口）
def HHAD(line: float = -1.0) -> MarketSpec:  # noqa: N802 (与官方 poolCode 同名)
    """构造让球胜平负市场。

    line 为**主队让球数**（体彩惯例：-1 表示主队让 1 球）。
    判定：主队净胜球 + line > 0 → home；= 0 → draw；< 0 → away。
    """
    return MarketSpec(
        code="HHAD(%s)" % _fmt_line(line),
        name="让球胜平负",
        pool_code="HHAD",
        outcomes=("home", "draw", "away"),
        line=_as_float(line, "line"),
        aliases={"h": "home", "d": "draw", "a": "away"},
        notes="主队让球 %s" % _fmt_line(line),
    )


#: 标准玩法（非体彩，但常见于授权赔率源）
BTTS = MarketSpec(
    code="BTTS", name="双方进球",
    outcomes=("yes", "no"),
    aliases={"是": "yes", "否": "no", "y": "yes", "n": "no"},
)

DC = MarketSpec(
    code="DC", name="双重机会",
    outcomes=("1X", "12", "X2"),
    space=ResultSpace.OVERLAPPING,
    aliases={"home_or_draw": "1X", "home_or_away": "12",
             "draw_or_away": "X2"},
    notes="结果重叠：主胜同时属于 1X 与 12，概率和为 2",
)


def OU(line: float = 2.5) -> MarketSpec:  # noqa: N802
    """构造大小球市场（over/under）。"""
    return MarketSpec(
        code="OU(%s)" % _fmt_line(line),
        name="大小球",
        outcomes=("over", "under"),
        line=_as_float(line, "line"),
        aliases={"大": "over", "小": "under", "o": "over", "u": "under"},
        notes="盘口 %s" % _fmt_line(line),
    )


def AH(line: float = -0.5) -> MarketSpec:  # noqa: N802
    """构造亚洲让球（两结果，无平局）。"""
    return MarketSpec(
        code="AH(%s)" % _fmt_line(line),
        name="亚洲让球",
        outcomes=("home", "away"),
        line=_as_float(line, "line"),
        aliases={"h": "home", "a": "away"},
        notes="主队让球 %s（走盘按平局退还，本层不给概率）" % _fmt_line(line),
    )


def _as_float(value: object, name: str) -> float:
    """安全转 float：非数值或非有限值一律抛 ValueError。

    上游 JSON 里盘口/赔率常以字符串给出（"-1.00"、"2.5"），
    直接 float() 遇到 "N/A"、"" 或 None 会抛 TypeError，
    错误信息对排障毫无帮助。这里统一收敛为可读的 ValueError。

    注意必须同时捕获 OverflowError：float(10**400) 会抛
    OverflowError 而非 ValueError，漏掉它会让超大整数穿透校验层。
    """
    if value is None or isinstance(value, bool):
        raise ValueError("%s 必须是数值，实际 %r" % (name, value))
    if isinstance(value, str) and not value.strip():
        raise ValueError("%s 是空字符串" % name)
    try:
        out = float(value)            # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "%s 无法解析为数值：%r" % (name, value)) from exc
    if not math.isfinite(out):
        raise ValueError("%s 必须是有限数，实际 %r" % (name, value))
    return out


def _int_of(finite: float, name: str, original: object) -> int:
    """把**已知有限**的浮点转 int，并显式兜底以防静默截断。

    调用方须先经 _as_float 保证有限性；此处仍做防御，
    避免将来有人绕过校验直接调用时产生未捕获异常。
    """
    try:
        return int(finite)
    except (ValueError, OverflowError, TypeError) as exc:
        raise ValueError(
            "%s 无法转为整数：%r" % (name, original)) from exc


def _as_int(value: object, name: str) -> int:
    """安全转 int：接受 int 与整数值字符串/浮点，其余抛 ValueError。"""
    f = _as_float(value, name)
    if f != _int_of(f, name, value):
        raise ValueError("%s 必须是整数，实际 %r" % (name, value))
    return _int_of(f, name, value)


def _fmt_line(line: object) -> str:
    """盘口线的规范文本（去掉多余小数位：-1.0 → -1，2.5 → 2.5）。"""
    value = _as_float(line, "line")
    as_int = _int_of(value, "line", line)
    if value == as_int:
        return str(as_int)
    return repr(value)


OFFICIAL_SPECS: Tuple[MarketSpec, ...] = (HAD, HHAD(-1.0), TTG, CRS, HAFU)
STANDARD_SPECS: Tuple[MarketSpec, ...] = (HAD, AH(-0.5), OU(2.5), BTTS, DC)


# --------------------------------------------------------------------------- #
# 上游赔率键 → 规范结果标签
# --------------------------------------------------------------------------- #
#
# 体彩官方 API 的 oddsList 条目按 poolCode 使用**不同的键名**。
# 这一层把「上游编码」与「本项目的规范结果标签」解耦，
# 使采集器可以一次覆盖全部玩法，而不是像原先那样只认 HAD/HHAD。
#
# 说明：这些键名依据官方玩法布局定义。由于官方接口对非浏览器指纹
# 返回 WAF 拦截（HTTP 567），**CRS 的精确键布局无法在线复核**；
# 因此解析器对未知键一律记录告警而非静默丢弃（fail-loud），
# 保证一旦上游编码变化会立刻暴露，而不是产生错误的低水钱假象。

#: 胜平负 / 让球胜平负：官方用 h/d/a
HAD_ODDS_KEYS: Mapping[str, str] = {
    "h": "home", "d": "draw", "a": "away",
    "主胜": "home", "平局": "draw", "客胜": "away",
}

#: 总进球：s0..s6 为精确球数，s7 为「7 球及以上」
TTG_ODDS_KEYS: Mapping[str, str] = {
    "s0": "0", "s1": "1", "s2": "2", "s3": "3",
    "s4": "4", "s5": "5", "s6": "6", "s7": "7+",
}

#: 半全场：两个字母 = 半场/全场
HAFU_ODDS_KEYS: Mapping[str, str] = {
    "hh": "H/H", "hd": "H/D", "ha": "H/A",
    "dh": "D/H", "dd": "D/D", "da": "D/A",
    "ah": "A/H", "ad": "A/D", "aa": "A/A",
}

#: 比分「其他」桶的多种可能写法
CRS_OTHER_KEYS: Mapping[str, str] = {
    "otherh": "胜其他", "otherd": "平其他", "othera": "负其他",
    "winother": "胜其他", "drawother": "平其他", "loseother": "负其他",
    "胜其他": "胜其他", "平其他": "平其他", "负其他": "负其他",
}

#: 大小球 / 亚洲让球（标准赔率源的常见键名）
OU_ODDS_KEYS: Mapping[str, str] = {
    "o": "over", "over": "over", "u": "under", "under": "under",
    "大": "over", "小": "under",
}

#: 双方进球
BTTS_ODDS_KEYS: Mapping[str, str] = {
    "yes": "yes", "no": "no", "y": "yes", "n": "no",
    "是": "yes", "否": "no", "btts_yes": "yes", "btts_no": "no",
}

#: 双重机会
DC_ODDS_KEYS: Mapping[str, str] = {
    "1x": "1X", "12": "12", "x2": "X2",
    "home_or_draw": "1X", "home_or_away": "12", "draw_or_away": "X2",
}


def parse_crs_key(key: str) -> Optional[str]:
    """把上游比分键解析为 CRS 的规范结果标签。

    接受多种写法：
        "1:0" / "01:00" / "1-0" / "0100" / "0100|..." → "1:0"

    返回：
        CRS.outcomes 中的标签；若是「其他」桶则返回对应桶名；
        无法识别时返回 None（由调用方记录告警，**绝不静默丢弃**）。
    """
    if not isinstance(key, str):
        return None
    raw = key.strip()
    if not raw:
        return None

    lowered = raw.lower()
    if lowered in CRS_OTHER_KEYS:
        return CRS_OTHER_KEYS[lowered]
    if raw in CRS_OTHER_KEYS:
        return CRS_OTHER_KEYS[raw]

    # 去掉可能的后缀（部分接口会带 "|" 或 ";" 分隔的附加字段）
    head = re.split(r"[|;\s]", raw, maxsplit=1)[0]

    home_s = away_s = None
    if ":" in head:
        parts = head.split(":")
        if len(parts) == 2:
            home_s, away_s = parts
    elif "-" in head:
        parts = head.split("-")
        if len(parts) == 2:
            home_s, away_s = parts
    elif head.isdigit() and len(head) == 4:
        home_s, away_s = head[:2], head[2:]
    elif head.isdigit() and len(head) == 2:
        # 两位数字歧义（"10" 是 1:0 还是 10:0？）→ 明确拒绝
        return None

    if home_s is None or away_s is None:
        return None
    if not (home_s.isdigit() and away_s.isdigit()):
        return None

    label = "%d:%d" % (_as_int(home_s, "比分主队进球"),
                        _as_int(away_s, "比分客队进球"))
    if label in CRS.outcomes:
        return label
    # 未列举的比分属于「其他」桶，不应单独出现赔率
    return None


def pool_odds_keys(pool_code: str) -> Optional[Mapping[str, str]]:
    """返回某 poolCode 的上游键映射；HHAD 复用 HAD 的键。

    CRS 因键布局为「比分形式」而非固定键名，不在此表中，
    由 parse_crs_key() 单独处理，此时返回 None。
    """
    if not isinstance(pool_code, str) or not pool_code.strip():
        raise ValueError("pool_code 不能为空")
    code = pool_code.strip().upper()
    if code in ("HAD", "HHAD"):
        return HAD_ODDS_KEYS
    if code == "TTG":
        return TTG_ODDS_KEYS
    if code == "HAFU":
        return HAFU_ODDS_KEYS
    if code == "CRS":
        return None            # 用 parse_crs_key 逐键解析
    if code == "OU":
        return OU_ODDS_KEYS
    if code == "BTTS":
        return BTTS_ODDS_KEYS
    if code == "DC":
        return DC_ODDS_KEYS
    return None


# --------------------------------------------------------------------------- #
# 目录查询
# --------------------------------------------------------------------------- #

def catalog(
    include_standard: bool = True,
    hhad_line: float = -1.0,
    ou_line: float = 2.5,
    ah_line: float = -0.5,
) -> Tuple[MarketSpec, ...]:
    """返回可用市场目录（体彩官方 5 种 + 标准玩法）。"""
    specs: List[MarketSpec] = [HAD, HHAD(hhad_line), TTG, CRS, HAFU]
    if include_standard:
        specs.extend([AH(ah_line), OU(ou_line), BTTS, DC])
    return tuple(specs)


def spec_by_code(code: str) -> MarketSpec:
    """按 code 或 pool_code 查找市场规格。

    对带盘口的玩法（HHAD/OU/AH）支持省略盘口时返回默认线。
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code 不能为空")
    key = code.strip()

    # 1) 精确 code 匹配
    for spec in catalog():
        if spec.code == key:
            return spec

    # 2) pool_code 匹配（取默认盘口）
    for spec in catalog():
        if spec.pool_code == key.upper():
            return spec

    # 3) 无盘口的裸名（TTG/CRS/HAFU 等）
    for spec in catalog():
        if spec.pool_code is None and spec.code.upper() == key.upper():
            return spec

    raise KeyError(
        "未知市场代码 %r；可用：%s"
        % (code, ", ".join(s.code for s in catalog()))
    )


# --------------------------------------------------------------------------- #
# 派生概率：从比分矩阵导出任意玩法
# --------------------------------------------------------------------------- #

def _totals(matrix: Sequence[Sequence[float]]) -> Dict[int, float]:
    """总进球分布 P(total = k)（含截断尾部合并）。"""
    out: Dict[int, float] = {}
    n = len(matrix)
    for i in range(n):
        for j in range(n):
            out[i + j] = out.get(i + j, 0.0) + matrix[i][j]
    return out


def _crs_probabilities(matrix: Sequence[Sequence[float]]) -> Tuple[float, ...]:
    """比分玩法的 31 个结果概率（末尾三个为「其他」桶）。"""
    n = len(matrix)

    def cell(score: str) -> float:
        h, a = score.split(":")
        i = _as_int(h, "比分主队进球")
        j = _as_int(a, "比分客队进球")
        if 0 <= i < n and 0 <= j < n:
            return matrix[i][j]
        return 0.0

    probs: List[float] = [cell(s) for s in _CRS_HOME + _CRS_DRAW + _CRS_AWAY]

    # 「其他」桶 = 该结果类型总概率 − 已列举比分概率
    home_total = sum(
        matrix[i][j] for i in range(n) for j in range(n) if i > j
    )
    draw_total = sum(matrix[i][i] for i in range(n))
    away_total = sum(
        matrix[i][j] for i in range(n) for j in range(n) if i < j
    )
    listed_home = math.fsum(probs[0:len(_CRS_HOME)])
    listed_draw = math.fsum(
        probs[len(_CRS_HOME):len(_CRS_HOME) + len(_CRS_DRAW)]
    )
    listed_away = math.fsum(
        probs[len(_CRS_HOME) + len(_CRS_DRAW):]
    )
    probs.extend([
        max(0.0, home_total - listed_home),
        max(0.0, draw_total - listed_draw),
        max(0.0, away_total - listed_away),
    ])
    return tuple(probs)


def _hafu_probabilities_exact(
    lam_home: float,
    lam_away: float,
    ht_fraction: float = 0.45,
) -> Tuple[float, ...]:
    """半全场概率（直接联合枚举，最清晰可靠）。"""
    if not 0.0 < ht_fraction < 1.0:
        raise ValueError("ht_fraction 必须在 (0,1)：%r" % ht_fraction)
    n = 8
    ht = score_matrix(lam_home * ht_fraction, lam_away * ht_fraction, n)
    sh = score_matrix(lam_home * (1 - ht_fraction),
                      lam_away * (1 - ht_fraction), n)

    idx = {o: i for i, o in enumerate(HAFU_OUTCOMES)}
    out = [0.0] * len(HAFU_OUTCOMES)

    def sign(x: int, y: int) -> str:
        return "H" if x > y else ("A" if x < y else "D")

    for i in range(n + 1):
        for j in range(n + 1):
            p_ht = ht[i][j]
            if p_ht == 0.0:
                continue
            ht_res = sign(i, j)
            for k in range(n + 1):
                for l in range(n + 1):
                    p = p_ht * sh[k][l]
                    if p == 0.0:
                        continue
                    ft_res = sign(i + k, j + l)
                    out[idx["%s/%s" % (ht_res, ft_res)]] += p
    return tuple(out)


def market_probs(
    spec: MarketSpec,
    lam_home: float,
    lam_away: float,
    normalize: bool = True,
    max_goals: int = _MAX_GOALS,
) -> Tuple[float, ...]:
    """从两队期望进球导出某玩法的模型概率向量。

    Args:
        spec: 市场规格。
        lam_home: 主队期望进球。
        lam_away: 客队期望进球。
        normalize: 是否归一化到和为 1（完备划分建议 True）。
        max_goals: 比分矩阵截断上限。

    Returns:
        与 spec.outcomes 同序的概率元组。
    """
    if not spec.is_partition and spec.space is ResultSpace.DERIVED_SUBSET:
        raise ValueError(
            "%s 是单腿子集，请用 subset_prob()" % spec.code
        )

    matrix = score_matrix(lam_home, lam_away, max_goals)
    # 显式标注：各分支返回长度不同（2/3/8/9/31），
    # 若让类型从首个分支推断会把 vec 锁死为 3 元组。
    vec: Tuple[float, ...]

    if spec.code == "HAD":
        h = sum(matrix[i][j] for i in range(max_goals + 1)
                for j in range(max_goals + 1) if i > j)
        d = sum(matrix[i][i] for i in range(max_goals + 1))
        a = sum(matrix[i][j] for i in range(max_goals + 1)
                for j in range(max_goals + 1) if i < j)
        vec = (h, d, a)

    elif spec.pool_code == "HHAD":
        line = 0.0 if spec.line is None else spec.line
        h = d = a = 0.0
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                adj = (i - j) + line
                if adj > 0:
                    h += matrix[i][j]
                elif adj == 0:
                    d += matrix[i][j]
                else:
                    a += matrix[i][j]
        vec = (h, d, a)

    elif spec.code == "TTG":
        tot = _totals(matrix)
        vec = tuple(
            sum(v for k, v in tot.items() if k >= 7) if o == "7+"
            else tot.get(_as_int(o, "TTG 结果标签"), 0.0)
            for o in spec.outcomes
        )

    elif spec.code == "CRS":
        vec = _crs_probabilities(matrix)

    elif spec.code == "HAFU":
        vec = _hafu_probabilities_exact(lam_home, lam_away)

    elif spec.code == "BTTS":
        yes = sum(matrix[i][j] for i in range(1, max_goals + 1)
                  for j in range(1, max_goals + 1))
        vec = (yes, math.fsum(
            matrix[i][j] for i in range(max_goals + 1)
            for j in range(max_goals + 1)
        ) - yes)

    elif spec.code.startswith("OU("):
        line = 0.0 if spec.line is None else spec.line
        tot = _totals(matrix)
        over = sum(v for k, v in tot.items() if k > line)
        under = sum(v for k, v in tot.items() if k < line)
        push = sum(v for k, v in tot.items() if k == line)
        # 走盘概率按退还处理：不计入 over/under 的期望损失
        vec = (over, under) if push == 0.0 else (
            over + push / 2.0, under + push / 2.0)

    elif spec.code.startswith("AH("):
        line = 0.0 if spec.line is None else spec.line
        h = a = 0.0
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                adj = (i - j) + line
                if adj > 0:
                    h += matrix[i][j]
                elif adj < 0:
                    a += matrix[i][j]
                else:
                    h += matrix[i][j] / 2.0
                    a += matrix[i][j] / 2.0
        vec = (h, a)

    elif spec.code == "DC":
        h = sum(matrix[i][j] for i in range(max_goals + 1)
                for j in range(max_goals + 1) if i > j)
        d = sum(matrix[i][i] for i in range(max_goals + 1))
        a = sum(matrix[i][j] for i in range(max_goals + 1)
                for j in range(max_goals + 1) if i < j)
        vec = (h + d, h + a, d + a)

    else:
        raise KeyError("未实现的市场类型：%s" % spec.code)

    # 重叠结果空间（如双重机会）的概率和为 2，**绝不能归一化到 1**，
    # 否则每个概率都会被静默砍半。
    if spec.space is ResultSpace.OVERLAPPING:
        return tuple(vec)
    if normalize:
        return _normalize(vec)
    return tuple(vec)


def subset_prob(
    spec: MarketSpec,
    outcome: str,
    lam_home: float,
    lam_away: float,
) -> float:
    """单腿概率：P(某结果)（对非完备划分市场必需）。"""
    idx = spec.index_of(outcome)
    vec = market_probs(spec, lam_home, lam_away, normalize=True)
    return vec[idx]


# --------------------------------------------------------------------------- #
# 由球队数据估计期望进球
# --------------------------------------------------------------------------- #

def expected_goals_from_stats(
    home_avg_for: float,
    home_avg_against: float,
    away_avg_for: float,
    away_avg_against: float,
    league_avg_goals: float,
    shrink: float = 0.0,
) -> Tuple[float, float]:
    """用「攻击力 / 防守力」乘积模型估计期望进球。

    经典 Dagum/Dixon-Coles 简化式：

        attack_home  = home_avg_for  / league_avg
        defence_away = away_avg_against / league_avg
        λ_home = attack_home * defence_away * league_avg
               = home_avg_for * away_avg_against / league_avg

    同式可得 λ_away。该模型对小球会样本量小的情况会过拟合，
    故提供 shrink ∈ [0,1]：把估计值向 league_avg 收缩（经验贝叶斯）。

    Args:
        home_avg_for: 主队场均进球。
        home_avg_against: 主队场均失球。
        away_avg_for: 客队场均进球。
        away_avg_against: 客队场均失球。
        league_avg_goals: 联赛场均总进球（通常 2.5–2.8）。
        shrink: 收缩系数，0 为不收缩。

    Returns:
        (λ_home, λ_away)。
    """
    vals = {
        "home_avg_for": home_avg_for,
        "home_avg_against": home_avg_against,
        "away_avg_for": away_avg_for,
        "away_avg_against": away_avg_against,
    }
    for name, v in vals.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise TypeError("%s 必须是数值：%r" % (name, v))
        if v < 0 or not math.isfinite(v):
            raise ValueError("%s 必须是非负有限数：%r" % (name, v))
    if league_avg_goals <= 0 or not math.isfinite(league_avg_goals):
        raise ValueError("league_avg_goals 必须为正：%r" % league_avg_goals)
    if not 0.0 <= shrink <= 1.0:
        raise ValueError("shrink 必须在 [0,1]：%r" % shrink)

    lam_h = home_avg_for * away_avg_against / league_avg_goals
    lam_a = away_avg_for * home_avg_against / league_avg_goals

    if shrink > 0.0:
        # 单队半场期望（联赛均值的一半）作为收缩目标
        target = league_avg_goals / 2.0
        lam_h = lam_h * (1 - shrink) + target * shrink
        lam_a = lam_a * (1 - shrink) + target * shrink

    # 边界保护：极小值会让比分矩阵退化为 0:0 主导
    lam_h = min(max(lam_h, 1e-6), 12.0)
    lam_a = min(max(lam_a, 1e-6), 12.0)
    return (lam_h, lam_a)


# --------------------------------------------------------------------------- #
# 校验工具
# --------------------------------------------------------------------------- #

def partition_check(
    spec: MarketSpec,
    probabilities: Sequence[float],
    tol: float = 1e-6,
) -> Tuple[bool, float]:
    """校验概率向量的和是否等于该结果空间的期望值，且各项非负。

    完备划分期望和为 1；重叠空间（双重机会）期望和为 2。

    Returns:
        (是否通过, 实际总和)。
    """
    if len(probabilities) != spec.n_outcomes:
        raise ValueError(
            "概率个数 %d 与 %s 的结果数 %d 不匹配"
            % (len(probabilities), spec.code, spec.n_outcomes)
        )
    total = math.fsum(probabilities)
    expected = spec.space.expected_sum
    ok = (abs(total - expected) <= tol
          and all(p >= -tol for p in probabilities))
    return (ok, total)


def cross_market_marginals(
    per_market: Mapping[str, Sequence[float]],
    tol: float = 0.02,
) -> List[Dict[str, object]]:
    """跨玩法边际一致性校验。

    原理：同一场比赛的多个玩法由**同一个**比分分布生成，
    因此彼此之间存在可检验的约束关系。例如：

        P(全场主胜) = P(半全场 H/*) 之和
        P(总进球 ≥ 1) = 1 − P(比分 0:0)
        P(双方进球 yes) 与 P(比分含 0:x / x:0) 相容

    若市场（去水后）违反这些恒等式，说明：
      a) 数据采集有误，或
      b) 不同玩法之间存在真实套利机会（报告 §8.4）。

    两种结论都需要人工介入，故本函数只报差异不做自动裁定。

    Args:
        per_market: 市场 code → 去水后概率向量。
        tol: 判定为「不一致」的差异阈值。

    Returns:
        不一致项列表，每项含 check / expected / actual / diff。
    """
    findings: List[Dict[str, object]] = []

    def add(check: str, expected: float, actual: float) -> None:
        diff = abs(expected - actual)
        if diff > tol:
            findings.append({
                "check": check,
                "expected": round(expected, 6),
                "actual": round(actual, 6),
                "diff": round(diff, 6),
            })

    # --- HAD vs HAFU：全场主胜 = 半全场所有 H/* 之和 ---
    had = None
    for key in ("HAD", "1X2"):
        if key in per_market:
            had = per_market[key]
            break
    if had is not None and "HAFU" in per_market and len(had) == 3:
        hafu = per_market["HAFU"]
        if len(hafu) == 9:
            # 关键：全场结果由 HAFU 标签的**第二个**字母决定，
            # 第一个字母是半场结果。写错维度会得到假告警。
            add("HAD.home == Σ HAFU[*/*H]", had[0],
                hafu[0] + hafu[3] + hafu[6])
            add("HAD.draw == Σ HAFU[*/*D]", had[1],
                hafu[1] + hafu[4] + hafu[7])
            add("HAD.away == Σ HAFU[*/*A]", had[2],
                hafu[2] + hafu[5] + hafu[8])

    # --- TTG vs CRS：总进球分布应一致 ---
    if "TTG" in per_market and "CRS" in per_market:
        ttg = per_market["TTG"]
        crs = per_market["CRS"]
        if len(ttg) == 8 and len(crs) == 31:
            crs_totals: Dict[int, float] = {}
            for k, score in enumerate(_CRS_HOME + _CRS_DRAW + _CRS_AWAY):
                h, a = score.split(":")
                t = _as_int(h, "比分主队进球") + _as_int(a, "比分客队进球")
                crs_totals[t] = crs_totals.get(t, 0.0) + crs[k]
            # 「其他」桶无法细分到具体球数，故只比较 0–5 球
            for t in range(0, 6):
                expected = ttg[t]
                actual = crs_totals.get(t, 0.0)
                if abs(expected - actual) > tol:
                    findings.append({
                        "check": "TTG[%d] vs CRS 总进球 %d" % (t, t),
                        "expected": round(expected, 6),
                        "actual": round(actual, 6),
                        "diff": round(abs(expected - actual), 6),
                        "note": "CRS 的「其他」桶未细分，故仅比较已列举比分",
                    })

    # --- OU vs TTG：大小球应可由总进球分布聚合得到 ---
    for key, spec in ((k, spec_by_code(k)) for k in per_market
                      if k.startswith("OU(")):
        ttg_ou = per_market.get("TTG")
        if ttg_ou is None or len(ttg_ou) != 8:
            continue
        line = spec.line or 0.0
        vec = per_market[key]
        if len(vec) != 2:
            continue
        over = sum(ttg_ou[k] for k in range(8)
                   if (k if k < 7 else 7) > line)
        under = sum(ttg_ou[k] for k in range(8)
                    if (k if k < 7 else 7) < line)
        if over > 0 and under > 0:
            total = over + under
            add("OU(%s).over vs TTG 聚合" % _fmt_line(line),
                over / total, vec[0])

    return findings

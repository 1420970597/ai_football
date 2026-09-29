# -*- coding: utf-8 -*-
"""
「实时倍率 → LLM + JEV 混合决策 → 自动买入」体育博彩交易机器人
技术可行性与经济学可行性研究 —— 图表与量化数据生成脚本

范围: 仅技术分析与经济学分析（不含法律合规评估）
运行: python3 scripts/make_figures.py
输出: images/*.png  +  data/*.csv
"""
import os
import csv
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = os.path.join(ROOT, "images")
DATA = os.path.join(ROOT, "data")
def ensure_dir(path):
    """确保输出目录存在；失败时给出可读错误而非原始堆栈。"""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise SystemExit("无法创建输出目录 %s: %s" % (path, exc)) from exc
    return path


ensure_dir(IMG)
ensure_dir(DATA)

for cand in ["/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
             "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"]:
    if os.path.exists(cand):
        try:
            font_manager.fontManager.addfont(cand)
        except Exception:
            pass
plt.rcParams["font.sans-serif"] = ["WenQuanYi Zen Hei", "Noto Sans CJK JP", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 150
plt.rcParams["savefig.bbox"] = "tight"
plt.rcParams["axes.edgecolor"] = "#444444"
plt.rcParams["axes.linewidth"] = 0.8

C_RED, C_ORG, C_GRN = "#c0392b", "#e67e22", "#27ae60"
C_BLU, C_GRY, C_DGR = "#2c6fa8", "#7f8c8d", "#2c3e50"
C_PUR, C_TEA = "#7d5ba6", "#16847c"
R = {}


def safe_int(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return d


def save(fig, name):
    p = os.path.join(IMG, name)
    fig.savefig(p, facecolor="white")
    plt.close(fig)
    print("  [fig]", name)


def write_csv(name, header, rows):
    p = os.path.join(DATA, name)
    try:
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    except OSError as exc:
        print("  [csv-skip]", name, exc)
        return
    print("  [csv]", name)


def box(ax, x, y, w, h, text, fc, tc="white", fs=10.5, bold=True, ec=None):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle="round,pad=0.55,rounding_size=1.1",
                                fc=fc, ec=ec or fc, lw=1.4))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            color=tc, weight="bold" if bold else "normal", linespacing=1.5)


def arrow(ax, x1, y1, x2, y2, c="#555555", lw=1.6, ls="-", style="-|>"):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                                 mutation_scale=13, color=c, lw=lw,
                                 linestyle=ls, shrinkA=2, shrinkB=2))


# ==================================================== 图 1 平台技术形态
def fig1_platform_infra():
    fig, ax = plt.subplots(figsize=(12.6, 7.8))
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    ax.text(50, 97, "图 1  目标平台的技术形态与接口面（工程视角）",
            ha="center", fontsize=13.5, weight="bold", color=C_DGR)
    ax.text(50, 93.2,
            "第三方 DNS 安全研究已记录：数十个品牌共用同一套基础设施（域名池 + CNAME 流量分发）",
            ha="center", fontsize=9, color=C_GRY)

    box(ax, 30, 85, 40, 6.4, "品牌层：乐鱼 / 开云 / 同类（数十个）", C_DGR, fs=11)
    box(ax, 8, 75.5, 84, 7,
        "共享基础设施：域名池（10^5 量级）· DNS-CNAME 多层流量分发 · 多镜像站点 · App 壳 · 支付网关池",
        "#34495e", fs=10)
    arrow(ax, 50, 85, 50, 82.8)

    box(ax, 3, 60, 30, 12.5,
        "接口面特征\n· 无公开 API / 无文档\n· 无沙箱 / 无版本号 / 无变更日志\n· 无 SLA / 无状态页",
        C_RED, fs=9.4)
    box(ax, 35, 60, 30, 12.5,
        "前端特征\n· SPA + JS 动态渲染\n· 长连（WS/SSE）推送赔率\n· 会话/设备指纹绑定 · 估值与下注共用通道",
        C_ORG, fs=9.4)
    box(ax, 67, 60, 30, 12.5,
        "数据面特征\n· 更新频率随赛事阶段变化\n· 关键事件触发全市场 suspend\n· 端点域名持续轮换",
        C_BLU, fs=9.4)
    arrow(ax, 18, 75.5, 18, 72.9)
    arrow(ax, 50, 75.5, 50, 72.9)
    arrow(ax, 82, 75.5, 82, 72.9)

    ax.text(50, 56.0, "对采集系统的直接工程后果", ha="center", fontsize=11.5,
            weight="bold", color=C_DGR)

    box(ax, 4, 41, 28, 12,
        "端点发现与健康检查\n域名池轮换 → 需要持续探测\n可用端点、延迟分布、\n证书/链路变更",
        "#4a6f8a", fs=9.2)
    box(ax, 36, 41, 28, 12,
        "浏览器级执行\n前端为 JS 渲染 + 反自动化\n需要真实渲染与长连维持，\n而非 HTTP 轮询",
        "#4a6f8a", fs=9.2)
    box(ax, 68, 41, 28, 12,
        "状态机管理\n会话、设备指纹、下注通道\n均需维持一致性；\n任何异常即失去行情",
        "#4a6f8a", fs=9.2)

    box(ax, 8, 25.5, 84, 11,
        "核心工程矛盾：这是一个「无契约、无版本、无 SLA、随时变更的私有前端」。\n"
        "它不是一个可以稳定集成的服务端点 —— 采集层要承担对手方基础设施的\n"
        "全部不确定性，而这些不确定性无法通过代码消除。",
        "#8e6f2f", fs=10.2)

    box(ax, 8, 10, 40, 11.5,
        "可对冲的部分\n· 多源冗余（多平台/多镜像并行）\n· 端点池 + 自动故障转移\n· 本地时序库自建（不依赖对方历史）",
        C_GRN, fs=9.4)
    box(ax, 52, 10, 40, 11.5,
        "不可对冲的部分\n· 对方单方面停盘 / 改价 / 拒单\n· 对方随时变更协议与字段\n· 账户状态与被接受程度不可观测",
        C_RED, fs=9.4)
    arrow(ax, 50, 25.5, 28, 21.9)
    arrow(ax, 50, 25.5, 72, 21.9)
    save(fig, "fig1_platform_infra.png")


# ==================================================== 图 2 水钱与 EV
def fig2_margin_ev():
    mk = [("交易所（撮合，抽佣）", 2.0), ("欧冠 赛前 1X2", 4.0), ("网球 赛前", 4.5),
          ("英超 赛前 1X2", 5.0), ("滚球 足球在盘（均值）", 11.4),
          ("滚球 同场串关/道具", 13.0), ("四串一（5.2%/腿 复利）", 22.8),
          ("首个进球者", 27.0), ("正确比分", 32.0)]
    names = [m[0] for m in mk]
    mg = np.array([m[1] for m in mk]) / 100.0
    ev = -mg / (1 + mg) * 100

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14.4, 6.4),
                                 gridspec_kw={"width_ratios": [1.25, 1]})
    cols = [C_GRN if m < .06 else (C_ORG if m < .13 else C_RED) for m in mg]
    y = np.arange(len(names))
    a1.barh(y, mg * 100, color=cols, height=.62)
    a1.set_yticks(y); a1.set_yticklabels(names, fontsize=9.5); a1.invert_yaxis()
    for i, m in enumerate(mg * 100):
        a1.text(m + .5, i, "%.1f%%" % m, va="center", fontsize=9.5, color=C_DGR)
    a1.set_xlim(0, 37); a1.grid(axis="x", alpha=.25, ls="--")
    a1.axvline(5.06, color=C_BLU, ls="--", lw=1.4)
    a1.text(5.3, 7.85, "头部市场指数均值\n5.06%（68 家样本）", fontsize=8.5, color=C_BLU)
    a1.set_xlabel("庄家水钱 overround (%)", fontsize=11)
    a1.set_title("图 2a  各市场结构性抽水\n在盘市场的水钱约为赛前的 2 倍以上",
                 fontsize=12, weight="bold", color=C_DGR)

    m = np.linspace(0, .35, 300)
    a2.plot(m * 100, -m / (1 + m) * 100, color=C_RED, lw=2.6)
    for mm, lb in [(.05, "赛前 5%"), (.114, "滚球 11.4%"), (.228, "四串一 22.8%")]:
        a2.plot(mm * 100, -mm / (1 + mm) * 100, "o", color=C_DGR, ms=8)
        a2.annotate("%s\nEV %.1f%%" % (lb, -mm / (1 + mm) * 100),
                    (mm * 100, -mm / (1 + mm) * 100),
                    textcoords="offset points", xytext=(14, 12),
                    fontsize=9.5, color=C_DGR)
    a2.axhline(0, color=C_GRY, lw=1)
    a2.set_xlabel("水钱 m (%)", fontsize=11)
    a2.set_ylabel("单注期望收益率 EV (%)", fontsize=11)
    a2.set_title("图 2b  EV = −m / (1 + m)\n与选谁、模型多聪明完全无关",
                 fontsize=12, weight="bold", color=C_DGR)
    a2.grid(alpha=.25, ls="--")
    a2.text(.5, -24, "赔率不是价格，\n是含税的报价。",
            fontsize=10, color=C_RED, weight="bold",
            bbox=dict(fc="#fdecea", ec=C_RED, alpha=.9, boxstyle="round,pad=0.5"))
    save(fig, "fig2_margin_and_ev.png")
    write_csv("margin_ev.csv", ["市场", "水钱%", "单注EV%"],
              [[names[i].replace("\n", " "), round(mg[i] * 100, 2), round(ev[i], 3)]
               for i in range(len(names))])
    R["ev_inplay"] = -0.114 / 1.114


# ==================================================== 图 3 所需精度优势
def fig3_required_edge():
    mgs = np.array([.03, .05, .0807, .114, .15, .20])
    lbs = ["3%\n交易所", "5%\n头部赛前", "8.07%\n平均水钱",
           "11.4%\n滚球均值", "15%\n滚球高阶", "20%\n滚球上限"]
    ps = np.array([.50, .30, .10])
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14.4, 6.0))
    x = np.arange(len(mgs)); w = .26
    for i, p in enumerate(ps):
        pp = p * mgs * 100
        a1.bar(x + (i - 1) * w, pp, w, label="真实胜率 %d%% 的标的" % safe_int(p * 100),
               color=[C_GRN, C_BLU, C_ORG][i])
        for xi, v in zip(x + (i - 1) * w, pp):
            a1.text(xi, v + .08, "%.1f" % v, ha="center", fontsize=8)
    a1.set_xticks(x); a1.set_xticklabels(lbs, fontsize=9.5)
    a1.set_xlabel("水钱水平", fontsize=11)
    a1.set_ylabel("必须超过市场隐含概率的百分点 (pp)", fontsize=11)
    a1.set_title("图 3a  打平所需精度优势\np_model > p_fair × (1 + m)",
                 fontsize=12, weight="bold", color=C_DGR)
    a1.legend(fontsize=9.5); a1.grid(axis="y", alpha=.25, ls="--")

    a2.bar(x - .2, mgs * 100, .4, color=C_RED, label="需要的相对优势 = 水钱 m")
    a2.bar(x + .2, np.full_like(mgs, 5.06), .4, color=C_BLU,
           label="公开数据模型现实可得相对优势 ≈ 0–2%")
    a2.set_xticks(x); a2.set_xticklabels(lbs, fontsize=9.5)
    a2.set_ylabel("%", fontsize=11); a2.set_xlabel("水钱水平", fontsize=11)
    a2.set_title("图 3b  分母不对等：\n你要打败的是庄家自己的定价模型",
                 fontsize=12, weight="bold", color=C_DGR)
    a2.legend(fontsize=9.5); a2.grid(axis="y", alpha=.25, ls="--")
    a2.text(.02, 18, "必须在概率精度上相对优于\n庄家定价模型 11.4%，\n才能仅仅打平。",
            fontsize=9.5, color=C_RED, weight="bold",
            bbox=dict(fc="#fdecea", ec=C_RED, boxstyle="round,pad=0.5"))
    save(fig, "fig3_required_edge.png")
    write_csv("required_edge.csv",
              ["水钱档位", "水钱%", "p=50%需超出(pp)", "p=30%需超出(pp)", "p=10%需超出(pp)"],
              [[lbs[i].replace("\n", " "), round(mgs[i] * 100, 2)] +
               [round(p * mgs[i] * 100, 3) for p in ps] for i in range(len(mgs))])


# ==================================================== 图 4 延迟预算
def fig4_latency():
    st = [("① 场上事件发生", 0, 0, C_DGR, "起点"),
          ("② 场馆数据采集\n(数据中心现场采集员)", 2.0, 5.0, C_DGR, "人眼/终端录入"),
          ("③ 数据商 → 庄家交易系统行情推送", 1.0, 3.0, "#34495e", "专线低延迟"),
          ("④ 庄家模型重算 + 风控复核", 1.0, 4.0, "#4a6f8a", "关键球直接 suspend"),
          ("⑤ 新赔率抵达你的采集端", 0.5, 3.0, "#6d8ea8", "长连推送 0.5–3s"),
          ("⑥ 特征计算 + 经济学算法栈", 0.05, 0.5, C_TEA, "去水/Kelly：确定性计算"),
          ("⑦ JEV 决策调用", 0.3, 1.5, C_PUR, "小模型：高频低延迟"),
          ("⑧ LLM 决策调用（仅慢通路）", 1.5, 6.0, C_ORG, "大模型：低频，非关键路径"),
          ("⑨ 下单请求 → 庄家风控", 0.5, 2.0, C_ORG, "链路往返"),
          ("⑩ 庄家下注接受延迟", 5.0, 10.0, C_RED, "交易所足球在盘最短 5s"),
          ("⑪ 成交结果：旧价 / 改价 / 拒单", 0, 0, C_RED, "由对手方裁定")]
    fig, ax = plt.subplots(figsize=(13.6, 7.2))
    y = np.arange(len(st))[::-1]
    lo = np.array([s[1] for s in st]); hi = np.array([s[2] for s in st])
    start = np.concatenate([[0], np.cumsum(hi)[:-1]])
    for i, (nm, l, h, c, note) in enumerate(st):
        yy = y[i]
        if h > 0:
            ax.barh(yy, h, left=start[i], height=.55, color=c, alpha=.92)
            ax.barh(yy, l, left=start[i], height=.55, color="white", alpha=.32)
            ax.text(start[i] + h / 2, yy, "%.1f–%.1fs" % (l, h), ha="center", va="center",
                    fontsize=8.3, color="white" if h > 1.5 else C_DGR, weight="bold")
        ax.text(-0.6, yy, nm, ha="right", va="center", fontsize=9.3,
                color=C_DGR, linespacing=1.4)
        ax.text(start[i] + h + .7, yy, note, ha="left", va="center",
                fontsize=8.1, color=C_GRY)

    # 关键路径（不含 LLM 慢通路）
    fast = hi.copy(); fast[7] = 0.0
    t_fast = fast.sum()
    ax.axvline(t_fast, color=C_GRN, ls="--", lw=1.6)
    ax.axvline(hi.sum(), color=C_RED, ls="--", lw=1.6)
    ax.set_xlim(-15, max(hi.sum() * 1.34, 46))
    ax.set_ylim(-1.0, len(st) - .2); ax.set_yticks([])
    ax.set_xlabel("从「场上事件发生」起算的累计时间（秒）", fontsize=11)
    ax.set_title("图 4  延迟预算：LLM 只应存在于慢通路，"
                 "关键路径由 JEV + 经济学算法承担",
                 fontsize=12.5, weight="bold", color=C_DGR)
    ax.grid(axis="x", alpha=.25, ls="--")
    ax.text(t_fast - 0.6, len(st) - 1.3, "关键路径（快速通路，不含 LLM）≈ %.1fs" % t_fast,
            color=C_GRN, fontsize=9.5, weight="bold", ha="right")
    ax.text(hi.sum() + .5, len(st) - 2.6,
            "若 LLM 进入关键路径 ≈ %.1fs" % hi.sum(),
            color=C_RED, fontsize=9.5, weight="bold", ha="left")
    save(fig, "fig4_latency_budget.png")
    write_csv("latency_budget.csv", ["阶段", "乐观(秒)", "保守(秒)", "说明"],
              [[s[0].replace("\n", " "), s[1], s[2], s[4]] for s in st] +
              [["关键路径合计（不含LLM）", round(lo[7] * 0 + lo.sum() - lo[7], 2),
                round(t_fast, 2), "从场上事件到成交确认"],
               ["全路径合计（含LLM）", round(lo.sum(), 2), round(hi.sum(), 2), ""]])
    R["lat_fast"], R["lat_full"] = t_fast, hi.sum()


# ==================================================== 图 5 LLM+JEV 架构
def fig5_hybrid_architecture():
    fig, ax = plt.subplots(figsize=(13.6, 8.4))
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    ax.text(50, 97.5, "图 5  LLM + JEV 混合决策架构（分层与数据流）",
            ha="center", fontsize=13.5, weight="bold", color=C_DGR)

    # L1 数据层
    box(ax, 2, 84, 30, 9, "L1 采集层\n长连推送 + 多源冗余 + 端点池\n归一化 → 本地时序库",
        C_DGR, fs=9.5)
    box(ax, 35, 84, 30, 9, "L1′ 微结构提取\n跳动频率 · 恢复时间 · 漂移速率\n买卖价差 · 盘口深度代理",
        "#34495e", fs=9.5)
    box(ax, 68, 84, 30, 9, "L2 特征层\n去水隐含概率 · 无风险参考线\n跨市场/跨平台一致性",
        C_TEA, fs=9.5)
    arrow(ax, 32, 88.5, 35, 88.5); arrow(ax, 65, 88.5, 68, 88.5)

    # 决策分流
    ax.text(50, 79, "L3 决策层 —— 三路分流（按延迟预算与信息形态路由）",
            ha="center", fontsize=11.5, weight="bold", color=C_DGR)

    box(ax, 2, 62, 29, 14,
        "路径 A · 确定性规则\n\n· 阈值/漂移速率触发\n· 跨平台套利检测\n· 去水后价值筛选\n延迟 <1ms · 成本 ≈ 0",
        C_GRN, fs=9.4)
    box(ax, 35.5, 62, 29, 14,
        "路径 B · JEV 决策模型\n\n· 高频结构化判断\n· 二值/多分类 + 校准置信\n· 微结构模式识别\n延迟 0.3–1.5s · 高频",
        C_PUR, fs=9.4)
    box(ax, 69, 62, 29, 14,
        "路径 C · LLM 慢通路\n\n· 非结构化信息抽取\n· 异常归因 / 事件解释\n· 策略复盘与规则生成\n延迟 1.5–6s · 低频",
        C_ORG, fs=9.4)
    arrow(ax, 83, 84, 83.5, 76.2); arrow(ax, 50, 84, 50, 76.2); arrow(ax, 17, 84, 16.5, 76.2)

    # 经济学算法栈
    box(ax, 6, 46, 88, 11,
        "L4 经济学算法栈（决策合流后的统一约束层）\n"
        "去水/公平概率估计 · 期望值与优势估计 · Kelly/分数 Kelly 仓位 · 相关性约束与敞口上限 · 成本摊销 · 执行可行性过滤",
        "#2c3e50", fs=10)

    box(ax, 6, 32, 42, 10,
        "L5 执行层\n下注队列 · 幂等与去重 · 速率整形\n拒单/改价处理状态机 · 结果对账",
        C_BLU, fs=9.8)
    box(ax, 52, 32, 42, 10,
        "L6 反馈与校准层\n结算结果回归 · CLV 归因 · 模型校准评估\n(Brier / ECE) · 参数自动调优",
        "#4a6f8a", fs=9.8)

    box(ax, 20, 17, 60, 10,
        "核心不对称：L1–L4 全部属于你，L5 之后由对手方裁定。\n"
        "你的架构可以把前四层做到毫秒级，但决定成交的 5–20 秒\n"
        "窗口始终握在对手方手里。",
        "#8e6f2f", fs=10)

    arrow(ax, 16, 62, 30, 57.4); arrow(ax, 50, 62, 50, 57.4)
    arrow(ax, 83, 62, 70, 57.4)
    arrow(ax, 50, 46, 50, 42.2)
    arrow(ax, 27, 32, 27, 27.2); arrow(ax, 73, 32, 73, 27.2)
    arrow(ax, 73, 27, 73, 22.2, c=C_GRY, ls="--", style="-|>")
    ax.text(76, 24.5, "（反馈回路）", fontsize=8.5, color=C_GRY)
    save(fig, "fig5_hybrid_architecture.png")


# ==================================================== 图 6 模型路由矩阵
def fig6_routing():
    tasks = ["赔率跳动检测", "跨平台套利判定", "去水/公平概率", "价值下注筛选",
             "仓位计算", "微结构模式判断", "伤停/新闻抽取", "异常归因与复盘",
             "策略规则生成"]
    cols = ["延迟预算", "调用频率", "单次成本", "适配模型", "经济学贡献"]
    # (延迟分, 频率分, 成本分, 模型编号, 经济学贡献分) 0-5；成本分越高=越便宜
    M = np.array([
        [5, 5, 5, 0, 2],
        [5, 4, 5, 0, 4],
        [5, 5, 5, 0, 2],
        [4, 4, 4, 1, 3],
        [5, 4, 5, 0, 4],
        [3, 4, 2, 1, 2],
        [1, 1, 2, 2, 3],
        [1, 1, 2, 2, 1],
        [1, 1, 1, 2, 2],
    ], dtype=float)
    fig, ax = plt.subplots(figsize=(12.4, 6.6))
    score = M[:, :3].mean(axis=1)
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list(
        "r", ["#a52a2a", "#e67e22", "#f4d03f", "#7dcea0", "#27ae60"])
    ax.imshow(score.reshape(-1, 1), cmap=cmap, vmin=0, vmax=5, aspect="auto")
    ax.set_yticks(range(len(tasks))); ax.set_yticklabels(tasks, fontsize=10.5)
    ax.set_xticks([])
    mname = {0: "规则/算法", 1: "JEV", 2: "LLM"}
    mcol = {0: C_GRN, 1: C_PUR, 2: C_ORG}
    for i in range(len(tasks)):
        s = safe_int(M[i, 3])
        ax.text(0, i, "  %s  " % mname[s], va="center", ha="center", fontsize=10.5,
                weight="bold", color="white",
                bbox=dict(fc=mcol[s], ec="none", boxstyle="round,pad=0.35"))
    ax.set_title("图 6  决策路由矩阵：LLM 只应承担低频、非结构化、非关键路径的任务\n"
                 "（底色 = 该任务在延迟/频率/成本三维上的综合适配度）",
                 fontsize=12.2, weight="bold", color=C_DGR)
    ax.set_xticks([-.5, .5], minor=True)
    ax.grid(which="minor", color="white", lw=2.5)
    ax.tick_params(which="minor", length=0)
    save(fig, "fig6_routing_matrix.png")
    write_csv("routing_matrix.csv", ["任务"] + cols,
              [[tasks[i], safe_int(M[i, 0]), safe_int(M[i, 1]), safe_int(M[i, 2]),
                mname[safe_int(M[i, 3])], safe_int(M[i, 4])] for i in range(len(tasks))])


# ==================================================== 图 7 成本-收益平衡
def fig7_cost_breakeven():
    # 月度基础设施与模型调用成本
    cost_items = [("行情数据 API（多源冗余）", 250), ("JEV 调用（约 150k 次/月）", 150),
                  ("LLM 调用（约 6k 次/月）", 60), ("采集基础设施/代理/算力", 140),
                  ("存储与监控", 60), ("人力摊销（0.2 FTE）", 400)]
    total_cost = sum(c for _, c in cost_items)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14.6, 6.2),
                                 gridspec_kw={"width_ratios": [1, 1.15]})
    ax = a1
    y = np.arange(len(cost_items))
    vals = [c for _, c in cost_items]
    cmap = [C_ORG if "LLM" in n or "JEV" in n else C_BLU for n, _ in cost_items]
    ax.barh(y, vals, color=cmap, height=.6)
    ax.set_yticks(y); ax.set_yticklabels([n for n, _ in cost_items], fontsize=9.5)
    ax.invert_yaxis()
    for i, v in enumerate(vals):
        ax.text(v + 5, i, "$%d" % v, va="center", fontsize=9.5, color=C_DGR)
    ax.set_xlim(0, max(vals) * 1.28)
    ax.set_xlabel("月度成本 (USD)", fontsize=11)
    ax.set_title("图 7a  月度运行成本构成\n合计 ≈ $%d/月（约 $%.1f/天）"
                 % (total_cost, total_cost / 30),
                 fontsize=12, weight="bold", color=C_DGR)
    ax.grid(axis="x", alpha=.25, ls="--")

    # 摊销：不同 edge / 不同单注规模下，覆盖成本所需日均注数
    stakes = np.array([10, 20, 50, 100, 200])
    edges = [0.01, 0.02, 0.03]
    ax2 = a2
    for e, c in zip(edges, [C_BLU, C_GRN, C_TEA]):
        need = total_cost / 30 / (e * stakes)      # 日均注数
        ax2.plot(stakes, need, "o-", color=c, lw=2.2, ms=7,
                 label="真实优势 %d%%" % safe_int(e * 100))
        for s, n in zip(stakes, need):
            ax2.annotate("%.0f" % n, (s, n), textcoords="offset points",
                         xytext=(0, 9), ha="center", fontsize=8.5, color=c)
    ax2.set_yscale("log")
    ax2.set_xscale("log")
    ax2.set_xticks(stakes); ax2.set_xticklabels(["$%d" % s for s in stakes], fontsize=9.5)
    ax2.set_xlabel("单注规模 (USD)", fontsize=11)
    ax2.set_ylabel("覆盖成本所需日均注数（对数）", fontsize=11)
    ax2.set_title("图 7b  成本摊销：优势越小、注额越小，\n需要的投注量指数级上升",
                  fontsize=12, weight="bold", color=C_DGR)
    ax2.grid(alpha=.25, ls="--", which="both")
    ax2.axhline(30, color=C_RED, ls="--", lw=1.5)
    ax2.text(11, 33, "30 注/天：受限账户的典型上限量级", fontsize=8.8, color=C_RED)
    ax2.legend(fontsize=9.5, loc="upper right")
    ax2.text(0.03, 0.06,
             "注意：即使把优势假设为 +3%（业界乐观上界），\n"
             "小注额下仍需 20–70 注/天才能覆盖固定成本；\n"
             "而一旦被风控降额，单注规模下降会再次推高所需注数。",
             transform=ax2.transAxes, fontsize=9, color=C_RED, weight="bold",
             bbox=dict(fc="#fdecea", ec=C_RED, boxstyle="round,pad=0.45"))
    save(fig, "fig7_cost_breakeven.png")

    rows = [[n, c] for n, c in cost_items] + [["合计", total_cost]]
    rows.append(["日均成本", round(total_cost / 30, 1)])
    for e in edges:
        for s in stakes:
            rows.append(["需日均注数 edge=%d%% stake=$%d" % (safe_int(e * 100), s),
                         round(total_cost / 30 / (e * s), 1)])
    write_csv("cost_breakeven.csv", ["项目", "值(USD 或 注数)"], rows)
    R["cost_month"], R["cost_day"] = total_cost, total_cost / 30


# ==================================================== 图 8 经济算法栈
def fig8_econ_stack():
    fig, ax = plt.subplots(figsize=(13.4, 7.4))
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    ax.text(50, 97, "图 8  经济学算法栈：从原始赔率到最终仓位",
            ha="center", fontsize=13.5, weight="bold", color=C_DGR)
    steps = [
        ("S1 去水 / 公平概率", "1/o_i 归一化 → p_fair\n多源加权（按历史校准度赋权）",
         C_TEA, "确定性"),
        ("S2 优势估计", "edge = p_model×o − 1\n叠加微结构信号（漂移、恢复）",
         C_BLU, "统计"),
        ("S3 不确定性折扣", "对 p_model 用后验方差收缩\nShrinkage → 降低过度自信",
         C_PUR, "贝叶斯"),
        ("S4 仓位（分数 Kelly）", "f* = (bp−q)/b × λ\nλ ≈ 0.2–0.5 抵消估计误差",
         C_GRN, "最优化"),
        ("S5 组合约束", "相关性/同场敞口上限\n单注上限 · 账户暴露上限",
         "#4a6f8a", "约束"),
        ("S6 成本摊销与执行过滤", "扣除调用成本与失败率\n可成交性预测 → 放弃低概率单",
         C_ORG, "工程"),
    ]
    for i, (t, d, c, tag) in enumerate(steps):
        x = 2 + (i % 3) * 32.7
        y = 70 - (i // 3) * 24
        box(ax, x, y, 30, 17, "", c)
        ax.text(x + 15, y + 13.2, t, ha="center", fontsize=10.4, color="white",
                weight="bold")
        ax.text(x + 15, y + 7.2, d, ha="center", fontsize=8.9, color="white",
                linespacing=1.55)
        ax.text(x + 15, y + 2.0, tag, ha="center", fontsize=8.2, color="white",
                bbox=dict(fc="#00000033", ec="none", boxstyle="round,pad=0.28"))
        if i < len(steps) - 1:
            if i % 3 < 2:
                arrow(ax, x + 30, y + 8.5, x + 32.7, y + 8.5, c=C_GRY)
            elif i // 3 == 0:
                arrow(ax, 50, y, 50, y - 7, c=C_GRY)
    arrow(ax, 17, 46, 17, 39, c=C_GRY)
    arrow(ax, 49.7, 46, 49.7, 39, c=C_GRY)
    arrow(ax, 82.4, 46, 82.4, 39, c=C_GRY)
    box(ax, 12, 20, 76, 16,
        "关键的诚实结论：S1–S6 全部是「给定 p_model 之后」的最优处理。\n"
        "它们能让一个真实存在的优势实现最大几何增长率，\n"
        "但无法创造优势 —— 若 p_model 无信息，整条栈只是在更精细地\n"
        "分配一个负期望。",
        C_RED, fs=10)
    save(fig, "fig8_econ_algo_stack.png")


# ==================================================== 图 9 跳动时序
def fig9_sequence():
    fig, ax = plt.subplots(figsize=(13.4, 6.8))
    ax.set_xlim(-1, 1); ax.set_ylim(-.7, 9.5); ax.axis("off")
    actors = ["球场", "数据商", "庄家\n交易系统", "你的系统\n(L1–L4)", "你的账户"]
    xs = np.linspace(-.82, .82, len(actors))
    for x, a in zip(xs, actors):
        ax.add_patch(FancyBboxPatch((x - .12, 8.6), .24, .64,
                                    boxstyle="round,pad=0.02,rounding_size=0.05",
                                    fc=C_DGR, ec=C_DGR))
        ax.text(x, 8.92, a, ha="center", va="center", color="white",
                fontsize=9.4, weight="bold", linespacing=1.4)
        ax.plot([x, x], [-.45, 8.55], color="#bbbbbb", lw=1, ls="--", zorder=0)

    def msg(y, i, j, text, c, dy=-.38, fs=9):
        ax.annotate("", xy=(xs[j], y), xytext=(xs[i], y),
                    arrowprops=dict(arrowstyle="-|>", color=c, lw=1.8,
                                    mutation_scale=13, shrinkA=3, shrinkB=3))
        if text:
            ax.text((xs[i] + xs[j]) / 2, y + dy, text, ha="center", fontsize=fs,
                    color=c, weight="bold")

    msg(8.15, 0, 1, "① 进球 / 红牌 / 角球", C_DGR)
    msg(7.35, 1, 2, "② 事件数据上行（2–5s 已耗掉）", C_DGR)
    msg(6.70, 2, 3, "③ 新赔率下发（0.5–3s）", C_BLU)
    ax.plot([xs[2] - .02, xs[3]], [5.95, 5.95], color=C_RED, lw=1.6, ls=":")
    ax.text(xs[2] - .02, 5.62,
            "③′ 庄家停盘（suspend）：你的信号读到的是\n     冻结或被撤下的价格",
            fontsize=8.8, color=C_RED, weight="bold", va="center", ha="left")
    ax.text(xs[3] + .02, 4.90,
            "④ 微结构 + 经济学栈（<0.5s）\n⑤ JEV 判定（0.3–1.5s）",
            fontsize=9.2, color=C_PUR, weight="bold", va="center", ha="left")
    msg(4.15, 3, 4, "⑥ 提交下注（0.5–2s）", C_ORG)
    ax.annotate("", xy=(xs[2] + .02, 2.72), xytext=(xs[4], 2.72),
                arrowprops=dict(arrowstyle="-|>", color=C_RED, lw=1.8,
                                mutation_scale=13, shrinkA=3, shrinkB=3))
    ax.text((xs[4] + xs[2]) / 2, 3.32,
            "⑦ 进入庄家风控队列 → 接受延迟 5–20s：\n     庄家用最新行情复查你的单子",
            fontsize=9.3, color=C_RED, weight="bold", ha="center", va="center")
    msg(1.95, 2, 4, "⑧a 价格已变 → 改价（要求接受更差价）", C_RED)
    msg(1.20, 2, 4, "⑧b 判定为优势玩家 → 拒单 / 降限额 / 标记", C_RED)
    msg(.45, 2, 4, "⑧c 少数情况按旧价成交", C_GRN)
    ax.text(0, -.62,
            "结构结论：L1–L4 可以被优化到亚秒级，但成交裁定权在对手方 —— "
            "等价于「交易所可事后撤销成交」的市场结构。",
            ha="center", fontsize=10.2, color=C_RED, weight="bold")
    ax.set_title("图 9  赔率跳动瞬间的真实时序：可优化部分与不可控边界",
                 fontsize=12.5, weight="bold", color=C_DGR, pad=12)
    save(fig, "fig9_odds_tick_sequence.png")


# ==================================================== 图 10 JEV 定位
def fig10_jev_position():
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14.2, 5.9),
                                 gridspec_kw={"width_ratios": [1.05, 1]})
    legs = ["赔率数值预测", "校准概率(去水)", "微结构模式判断",
            "软信息(伤停/新闻)", "决策与仓位", "下单执行"]
    fit = [1, 2, 4, 5, 4, 1]
    a1.barh(np.arange(len(legs)), fit,
            color=[C_RED if v <= 2 else (C_ORG if v == 4 else C_GRN) for v in fit],
            height=.6)
    a1.set_yticks(np.arange(len(legs))); a1.set_yticklabels(legs, fontsize=10.5)
    a1.invert_yaxis(); a1.set_xlim(0, 5.7)
    a1.set_xlabel("JEV 适配度（0 = 不应使用，5 = 高度适配）", fontsize=10.5)
    a1.set_title("图 10a  JEV 不是定价模型\n"
                 "它是「非结构化状态 → 带校准的离散判断」",
                 fontsize=12, weight="bold", color=C_DGR)
    for i, v in enumerate(fit):
        a1.text(v + .1, i, ["", "不适用", "不适用", "可用", "适配", "高度适配"][v],
                va="center", fontsize=9, color=C_GRY)
    a1.grid(axis="x", alpha=.25, ls="--")

    labels = ["数据采集与推送", "经济学算法栈", "JEV 决策", "执行与接受延迟", "风控/改价/拒单"]
    vals = [6.5, 0.3, 1.0, 8.5, 4.0]
    cols = [C_BLU, C_TEA, C_PUR, C_RED, C_DGR]
    w, _, _ = a2.pie(vals, colors=cols, autopct="%1.1f%%", startangle=110,
                     textprops={"fontsize": 9.8},
                     wedgeprops={"width": .42, "edgecolor": "white"})
    a2.legend(w, ["%s（约 %.1fs）" % (l, v) for l, v in zip(labels, vals)],
              loc="center left", bbox_to_anchor=(.98, .5), fontsize=9.3)
    a2.set_title("图 10b  JEV + 经济学栈在延迟中占比 <12%\n"
                 "真正的瓶颈不在模型侧",
                 fontsize=12, weight="bold", color=C_DGR)
    a2.text(0, -1.5,
            "模型侧优化（更快的 JEV、更好的提示词）最多影响约 11% 的延迟；\n"
            "其余 89% 由数据链路与对手方行为决定。",
            ha="center", fontsize=9.3, color=C_GRY, linespacing=1.6)
    save(fig, "fig10_jev_position.png")


# ==================================================== 图 11 蒙特卡洛
def fig11_montecarlo():
    rng = np.random.default_rng(20260601)
    BANK0, BETS, DAYS, N, FLAT = 10000.0, 5, 90, 20000, 0.02
    COST_DAY = R["cost_day"]

    def sim(ev, run_risk, frac=FLAT, n=N, days=DAYS):
        """情形 A/B/C：不计固定成本（成本影响单独在情形 D 中考察）"""
        bank = np.full(n, BANK0)
        paths = np.zeros((n, days + 1)); paths[:, 0] = BANK0
        dday = np.where(rng.random(n) < run_risk,
                        rng.integers(1, days + 1, size=n), 10 ** 9)
        q = min(max((1 + ev) / 1.95, .001), .999)
        for d in range(1, days + 1):
            for _ in range(BETS):
                stake = bank * frac
                win = rng.random(n) < q
                bank = np.where(win, bank + stake * .95, bank - stake)
            hit = dday <= d
            bank = np.where(hit, bank * .30, bank)
            dday = np.where(hit, 10 ** 9, dday)
            paths[:, d] = bank
        return paths

    A = sim(-0.102, 0.0)
    B = sim(+0.020, 0.0)
    C = sim(+0.020, 0.25)

    # 情形 D：+2% 优势、无平台风险，但每日扣除模型与基础设施运行成本
    rng2 = np.random.default_rng(7)
    bank = np.full(N, BANK0)
    D = np.zeros((N, DAYS + 1)); D[:, 0] = BANK0
    q2 = (1 + .020) / 1.95
    for d in range(1, DAYS + 1):
        for _ in range(BETS):
            stake = bank * FLAT
            win = rng2.random(N) < q2
            bank = np.where(win, bank + stake * .95, bank - stake)
        bank = np.maximum(bank - COST_DAY, 0)
        D[:, d] = bank

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14.6, 6.1),
                                 gridspec_kw={"width_ratios": [1.55, 1]})

    def band(ax, p, c, lb):
        lo = np.percentile(p, 5, axis=0); md = np.percentile(p, 50, axis=0)
        hi = np.percentile(p, 95, axis=0); x = np.arange(p.shape[1])
        ax.fill_between(x, lo, hi, color=c, alpha=.13)
        ax.plot(x, md, color=c, lw=2.4, label=lb)

    band(a1, A, C_RED, "A. 无优势（滚球水钱 11.4%）")
    band(a1, B, C_GRN, "B. 假设 +2% 优势，无平台风险，不计成本")
    band(a1, D, C_TEA, "D. 假设 +2% 优势，扣除 " + str(safe_int(COST_DAY)) + " 美元/天 运行成本")
    band(a1, C, C_ORG, "C. 假设 +2% 优势，25% 概率平台跑路/拒付")
    a1.axhline(BANK0, color=C_GRY, lw=1, ls=":")
    a1.set_yscale("log")
    a1.set_xlabel("交易日（5 注/天，每注 2% 银行）", fontsize=11)
    a1.set_ylabel("账户余额（对数刻度）", fontsize=11)
    a1.set_title("图 11a  蒙特卡洛 2 万次：中位数路径与 5–95 分位",
                 fontsize=12, weight="bold", color=C_DGR)
    a1.legend(fontsize=8.8, loc="lower left"); a1.grid(alpha=.25, ls="--")

    defs = [("A 无优势", A, C_RED), ("B +2% 不计成本", B, C_GRN),
            ("D +2% 扣成本", D, C_TEA), ("C +2% +25% 跑路", C, C_ORG)]
    probs = [(p[:, -1] > BANK0).mean() * 100 for _, p, _ in defs]
    a2.bar([d[0] for d in defs], probs, color=[d[2] for d in defs], width=.6)
    for i, v in enumerate(probs):
        a2.text(i, v + 1.4, "%.1f%%" % v, ha="center", fontsize=10.5,
                weight="bold", color=C_DGR)
    a2.set_ylim(0, 100); a2.set_ylabel("90 天后仍盈利的概率 (%)", fontsize=11)
    a2.set_title("图 11b  盈利概率", fontsize=12, weight="bold", color=C_DGR)
    a2.grid(axis="y", alpha=.25, ls="--")
    a2.tick_params(axis="x", labelsize=8.6)
    a2.text(1.5, 84,
            "固定成本的破坏力：\n每日 " + str(safe_int(COST_DAY))
            + " 美元成本，相当于把 2% 的优势\n直接压回接近零期望。",
            fontsize=8.8, color=C_RED, weight="bold", ha="center", va="top",
            bbox=dict(fc="#fdecea", ec=C_RED, boxstyle="round,pad=0.45"))
    save(fig, "fig11_montecarlo.png")

    rows = []
    for nm, p, _ in defs:
        rows.append([nm, round(np.median(p[:, -1]), 0),
                     round(np.percentile(p[:, -1], 5), 0),
                     round(np.percentile(p[:, -1], 95), 0),
                     round((p[:, -1] > BANK0).mean() * 100, 2)])
    write_csv("montecarlo.csv",
              ["情形", "90日余额中位数", "5分位", "95分位", "盈利概率%"], rows)
    R["p_A"], R["p_B"], R["p_C"], R["p_D"] = (
        (p[:, -1] > BANK0).mean() for _, p, _ in defs)


# ==================================================== 图 12 可行性评分
def fig12_heatmap():
    layers = ["L1 数据采集\n(实时倍率)", "L2 特征/微结构\n(去水·漂移)",
              "L3 决策\n(LLM+JEV)", "L4 经济学算法栈\n(Kelly·约束)",
              "L5 下单执行\n(自动投注)"]
    dims = ["技术可实现性", "经济学有效性", "工程稳定性", "长期可维护性"]
    M = np.array([
        [4, 3, 2, 2],
        [5, 2, 4, 3],
        [4, 1, 4, 3],
        [5, 2, 4, 4],
        [2, 1, 1, 1],
    ])
    fig, ax = plt.subplots(figsize=(11.8, 6.4))
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list(
        "f", ["#a52a2a", "#e67e22", "#f4d03f", "#7dcea0", "#27ae60"])
    im = ax.imshow(M, cmap=cmap, vmin=0, vmax=5, aspect="auto")
    ax.set_xticks(range(len(dims))); ax.set_xticklabels(dims, fontsize=11)
    ax.set_yticks(range(len(layers))); ax.set_yticklabels(layers, fontsize=10.3)
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            ax.text(j, i, "%d" % safe_int(M[i, j]), ha="center", va="center",
                    fontsize=15, weight="bold",
                    color="white" if M[i, j] <= 2 else "#1a1a1a")
    ax.set_title("图 12  架构层 × 技术/经济学维度评分（0 = 不可行，5 = 可行）\n"
                 "「可实现」与「有效」是两条完全不同的曲线",
                 fontsize=12.4, weight="bold", color=C_DGR)
    fig.colorbar(im, ax=ax, shrink=.8, label="评分")
    ax.set_xticks(np.arange(-.5, len(dims), 1), minor=True)
    ax.set_yticks(np.arange(-.5, len(layers), 1), minor=True)
    ax.grid(which="minor", color="white", lw=2.2)
    ax.tick_params(which="minor", length=0)
    save(fig, "fig12_feasibility_heatmap.png")
    write_csv("feasibility_scores.csv", ["架构层"] + dims,
              [[layers[i].replace("\n", " ")] + [safe_int(v) for v in M[i]]
               for i in range(len(layers))])


def main():
    print("生成图表与数据 ...")
    fig1_platform_infra()
    fig2_margin_ev()
    fig3_required_edge()
    fig4_latency()
    fig7_cost_breakeven()          # 先算成本，供蒙特卡洛使用
    fig5_hybrid_architecture()
    fig6_routing()
    fig8_econ_stack()
    fig9_sequence()
    fig10_jev_position()
    fig11_montecarlo()
    fig12_heatmap()
    print("\n关键数字：")
    print("  在盘水钱 11.4%% → 单注 EV = %.2f%%" % (R["ev_inplay"] * 100))
    print("  月度成本 $%d（$%.1f/天）" % (R["cost_month"], R["cost_day"]))
    print("  关键路径（不含 LLM）≈ %.1fs / 全路径 ≈ %.1fs" % (R["lat_fast"], R["lat_full"]))
    print("  盈利概率 A=%.1f%% B=%.1f%% C=%.1f%% D=%.1f%%"
          % (R["p_A"] * 100, R["p_B"] * 100, R["p_C"] * 100, R["p_D"] * 100))


if __name__ == "__main__":
    main()

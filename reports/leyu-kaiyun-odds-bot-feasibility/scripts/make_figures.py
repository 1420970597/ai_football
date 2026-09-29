# -*- coding: utf-8 -*-
"""
乐鱼体育 / 开云体育「实时倍率 -> JEV 决策 -> 自动买入」可行性调查报告
图表与量化数据生成脚本

运行:  python3 reports/leyu-kaiyun-odds-bot-feasibility/scripts/make_figures.py
输出:  reports/leyu-kaiyun-odds-bot-feasibility/images/*.png
       reports/leyu-kaiyun-odds-bot-feasibility/data/*.csv

所有数字均为公开资料 + 透明数学推导, 不含任何博彩平台内部数据。
"""
import os
import csv
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

# ---------------------------------------------------------------- 基础设置
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = os.path.join(ROOT, "images")
DATA = os.path.join(ROOT, "data")
os.makedirs(IMG, exist_ok=True)
os.makedirs(DATA, exist_ok=True)   # IMG/DATA 位于本报告目录内

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

C_RED = "#c0392b"
C_ORG = "#e67e22"
C_GRN = "#27ae60"
C_BLU = "#2c6fa8"
C_GRY = "#7f8c8d"
C_DGR = "#2c3e50"

RESULT = {}

def safe_int(value, default=0):
    """防御性转换：绘图/导出失败不应中断整批报告生成。"""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def save(fig, name):
    p = os.path.join(IMG, name)
    fig.savefig(p, facecolor="white")
    plt.close(fig)
    print("  [fig]", os.path.relpath(p, ROOT))


def write_csv(name, header, rows):
    p = os.path.join(DATA, name)
    try:
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    except OSError as exc:  # 目录不可写时不应中断整批报告生成
        print("  [csv-skip]", name, exc)
        return
    print("  [csv]", os.path.relpath(p, ROOT))


# ================================================================ 图 1 生态链路
def fig1_ecosystem():
    fig, ax = plt.subplots(figsize=(11.5, 7.6))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")

    def box(x, y, w, h, text, fc, ec, fs=10.5, tc="white", bold=True):
        ax.add_patch(FancyBboxPatch((x, y), w, h,
                                    boxstyle="round,pad=0.6,rounding_size=1.2",
                                    fc=fc, ec=ec, lw=1.4))
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
                fontsize=fs, color=tc, weight="bold" if bold else "normal",
                linespacing=1.5)

    def arrow(x1, y1, x2, y2, color="#555555", style="-|>", lw=1.6, ls="-"):
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                                     mutation_scale=14, color=color, lw=lw,
                                     linestyle=ls, shrinkA=2, shrinkB=2))

    ax.text(50, 96.5, "图 1  乐鱼 / 开云等品牌背后的“同一套技术底座”（Vigorish Viper 模型）",
            ha="center", fontsize=13.5, weight="bold", color=C_DGR)
    ax.text(50, 91.5, "来源：Infoblox Threat Intel《Vigorish Viper: A Venomous Bet》(2024-07-22)",
            ha="center", fontsize=9, color=C_GRY)

    box(34, 78, 32, 9,
        "亚博集团 Yabo Group\n(2022 年对外宣称“解散”)",
        C_DGR, C_DGR, 11)

    box(20, 64, 60, 9,
        "技术套件：软件 + DNS-CNAME 流量分发(TDS) + 主机 + 支付通道 + 移动 App\n"
        "（全套网络犯罪供应链，非单一公司）",
        "#34495e", "#34495e", 10)

    box(6, 50, 38, 9,
        "17 万+ 活跃域名\n品牌间“像连锁加盟分店”",
        "#4a6f8a", "#4a6f8a", 10.5)
    box(56, 50, 38, 9,
        "数十个看似无关的博彩品牌\n乐鱼体育 / 开云体育 / 同类变种",
        "#4a6f8a", "#4a6f8a", 10.5)

    box(20, 36, 60, 8.5,
        "欧洲足球俱乐部赞助（球衣胸前 / 场边广告 / 演播室）\n"
        "常见名单：皇马、AC米兰、国际米兰、诺丁汉森林、门兴、勒沃库森、切尔西、水晶宫等",
        "#6d8ea8", "#6d8ea8", 9.5)

    box(30, 23, 40, 8,
        "大中华区赌客（博彩近乎全面非法）\n年投注额约 8500 亿美元",
        C_RED, C_RED, 10)

    box(3, 8, 44, 10,
        "洗钱 / 资金非法出境",
        "#8e6f2f", "#8e6f2f", 10.5)
    box(53, 8, 44, 10,
        "东南亚人口贩运与强迫劳动\n（受害者被迫支撑博彩服务）",
        "#8e6f2f", "#8e6f2f", 10.5)

    arrow(50, 78, 50, 73.3)
    arrow(35, 64, 24, 59.3)
    arrow(65, 64, 76, 59.3)
    arrow(25, 50, 42, 44.8)
    arrow(75, 50, 58, 44.8)
    arrow(50, 36, 50, 31.3)
    arrow(44, 23, 28, 18.3)
    arrow(56, 23, 72, 18.3)

    ax.text(50, 1.5,
            "结论：这不是“体育科技公司”，而是一条以博彩为变现端的跨境有组织犯罪供给链。\n"
            "要对接的“平台”，正是这条链条的收银台。",
            ha="center", fontsize=10.5, color=C_RED, weight="bold", linespacing=1.7)
    save(fig, "fig1_vigorish_viper_ecosystem.png")


# ================================================================ 图 2 水钱与 EV
def fig2_margin_ev():
    markets = [
        ("交易所 Betfair\n(抽佣模式)", 2.0),
        ("欧冠 赛前 1X2", 4.0),
        ("网球 赛前", 4.5),
        ("英超 赛前 1X2", 5.0),
        ("滚球 足球 在盘\n(8%–20% 区间)", 11.4),
        ("滚球 同场串关/道具", 13.0),
        ("四串一(每腿 5.2%\n复利)", 22.8),
        ("首个进球者", 27.0),
        ("正确比分", 32.0),
    ]
    names = [m[0] for m in markets]
    margins = np.array([m[1] for m in markets]) / 100.0
    ev = -margins / (1 + margins) * 100  # EV = -m/(1+m)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6.4),
                                   gridspec_kw={"width_ratios": [1.25, 1]})

    colors = [C_GRN if m < 0.06 else (C_ORG if m < 0.13 else C_RED) for m in margins]
    y = np.arange(len(names))
    ax1.barh(y, margins * 100, color=colors, height=0.62)
    ax1.set_yticks(y)
    ax1.set_yticklabels(names, fontsize=9.5)
    ax1.invert_yaxis()
    ax1.set_xlabel("庄家水钱 / 抽水率（overround, %）", fontsize=11)
    ax1.set_title("图 2a  各类市场的结构性抽水\n（在盘市场的水钱约为赛前的 2 倍以上）",
                  fontsize=12, weight="bold", color=C_DGR)
    for i, m in enumerate(margins * 100):
        ax1.text(m + 0.5, i, f"{m:.1f}%", va="center", fontsize=9.5, color=C_DGR)
    ax1.set_xlim(0, 36)
    ax1.grid(axis="x", alpha=0.25, ls="--")
    ax1.axvline(5.06, color=C_BLU, ls="--", lw=1.4)
    ax1.text(5.3, 7.9, "Bet Better 指数\n头部市场均值 5.06%",
             fontsize=8.5, color=C_BLU)

    m = np.linspace(0, 0.35, 300)
    ax2.plot(m * 100, -m / (1 + m) * 100, color=C_RED, lw=2.6)
    for mm, lbl in [(0.05, "赛前 5%"), (0.114, "滚球 11.4%"), (0.228, "四串一 22.8%")]:
        ax2.plot(mm * 100, -mm / (1 + mm) * 100, "o", color=C_DGR, ms=8)
        ax2.annotate(f"{lbl}\n每注期望 {(-mm/(1+mm)*100):.1f}%",
                     (mm * 100, -mm / (1 + mm) * 100),
                     textcoords="offset points", xytext=(14, 12),
                     fontsize=9.5, color=C_DGR)
    ax2.axhline(0, color=C_GRY, lw=1)
    ax2.set_xlabel("水钱 m (%)", fontsize=11)
    ax2.set_ylabel("单注期望收益率 EV (%)", fontsize=11)
    ax2.set_title("图 2b  水钱直接决定期望值\nEV = −m / (1 + m)，与你看好谁无关",
                  fontsize=12, weight="bold", color=C_DGR)
    ax2.grid(alpha=0.25, ls="--")
    ax2.text(0.5, -22,
             "关键：这个负期望与“模型多聪明”无关。\n"
             "赔率不是价格，是含税的报价。",
             fontsize=10, color=C_RED, weight="bold",
             bbox=dict(fc="#fdecea", ec=C_RED, alpha=0.9, boxstyle="round,pad=0.5"))
    save(fig, "fig2_margin_and_ev.png")

    write_csv("margin_ev.csv", ["市场", "平均水钱%", "单注期望EV%"],
              [[names[i].replace("\n", " "), round(margins[i] * 100, 2),
                round(ev[i], 3)] for i in range(len(names))])
    RESULT["ev_inplay"] = -0.114 / 1.114


# ================================================================ 图 3 需要的精度优势
def fig3_required_edge():
    margins = np.array([0.03, 0.05, 0.0807, 0.114, 0.15, 0.20])
    labels = ["3%\n交易所", "5%\n赛前头部", "8.07%\n平均水钱",
              "11.4%\n滚球均值", "15%\n滚球高阶", "20%\n滚球上限"]
    p_fair = np.array([0.50, 0.30, 0.10])

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6.0),
                                   gridspec_kw={"width_ratios": [1, 1]})

    x = np.arange(len(margins))
    w = 0.26
    for i, p in enumerate(p_fair):
        pp = p * margins * 100
        bars = ax1.bar(x + (i - 1) * w, pp, w,
                       label="真实胜率 %d%% 的标的" % safe_int(p * 100),
                       color=[C_GRN, C_BLU, C_ORG][i])
        del bars
        for xi, v in zip(x + (i - 1) * w, pp):
            ax1.text(xi, v + 0.08, f"{v:.1f}", ha="center", fontsize=8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontsize=9.5)
    ax1.set_ylabel("必须超过市场隐含概率的百分点 (pp)", fontsize=11)
    ax1.set_xlabel("水钱水平", fontsize=11)
    ax1.set_title("图 3a  要“打平”必须多准多少个百分点？\n（p_model > p_fair × (1+m)）",
                  fontsize=12, weight="bold", color=C_DGR)
    ax1.legend(fontsize=9.5)
    ax1.grid(axis="y", alpha=0.25, ls="--")

    ax2.bar(x - 0.2, margins * 100, 0.4, color=C_RED, label="需要的相对概率优势 = 水钱 m")
    ax2.bar(x + 0.2, np.full_like(margins, 5.06), 0.4, color=C_BLU,
            label="公开数据模型现实可得的相对优势 ≈ 0~2%")
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, fontsize=9.5)
    ax2.set_ylabel("%", fontsize=11)
    ax2.set_xlabel("水钱水平", fontsize=11)
    ax2.set_title("图 3b  分母不对等：你需要打败的是“庄家自己的定价模型”",
                  fontsize=12, weight="bold", color=C_DGR)
    ax2.legend(fontsize=9.5)
    ax2.grid(axis="y", alpha=0.25, ls="--")
    ax2.text(0.02, 18,
             "庄家的赔率不是“猜测”，\n是夏普市场共识 + 风控因子。\n"
             "你必须在概率精度上相对优于它 11.4%，\n才能仅仅打平。",
             fontsize=9.5, color=C_RED, weight="bold",
             bbox=dict(fc="#fdecea", ec=C_RED, boxstyle="round,pad=0.5"))
    save(fig, "fig3_required_edge.png")

    rows = []
    for ml, mm in zip(labels, margins):
        rows.append([ml.replace("\n", " "), round(mm * 100, 2)] +
                    [round(p * mm * 100, 3) for p in p_fair])
    write_csv("required_edge.csv",
              ["水钱档位", "水钱%", "p=50%需超出(pp)", "p=30%需超出(pp)", "p=10%需超出(pp)"],
              rows)


# ================================================================ 图 4 延迟预算
def fig4_latency():
    stages = [
        ("① 场上事件发生", 0.0, 0.0, C_DGR, "起点"),
        ("② 场馆数据采集\n(Sportradar/Genius 现场观察员)", 2.0, 5.0, C_DGR,
         "人眼/终端录入，普遍 2–5s"),
        ("③ 数据商 → 庄家交易系统的行情推送", 1.0, 3.0, "#34495e",
         "专线/低延迟，0.3–2s 真实值，此处取保守"),
        ("④ 庄家模型重算 + 风控审核（可能直接停盘）", 1.0, 4.0, "#4a6f8a",
         "关键球/进球瞬间直接 suspend"),
        ("⑤ 新赔率推到前端/你的采集端", 0.5, 3.0, "#6d8ea8",
         "The Odds API 在盘 40s 轮询；直连站点 0.5–3s"),
        ("⑥ 你的 JEV 决策调用", 0.3, 3.0, C_ORG,
         "LLM/决策模型单次往返，波动极大"),
        ("⑦ 下单请求发出 → 到达庄家风控", 0.5, 2.0, C_ORG, "越洋/代理链路"),
        ("⑧ 庄家“下注接受延迟”", 5.0, 10.0, C_RED,
         "交易所足球在盘最短 5s；软庄常见 7–20s"),
        ("⑨ 结果：按旧价成交 / 改价 / 直接拒单", 0.0, 0.0, C_RED,
         "大概率落在对你最不利的一侧"),
    ]
    fig, ax = plt.subplots(figsize=(13, 6.8))
    y = np.arange(len(stages))[::-1]
    lo = np.array([s[1] for s in stages])
    hi = np.array([s[2] for s in stages])
    start = np.concatenate([[0], np.cumsum(hi)[:-1]])

    for i, (name, l, h, c, note) in enumerate(stages):
        yy = y[i]
        if h > 0:
            ax.barh(yy, h, left=start[i], height=0.55, color=c, alpha=0.9)
            ax.barh(yy, l, left=start[i], height=0.55, color="white", alpha=0.35)
            ax.text(start[i] + h / 2, yy, f"{l:.1f}–{h:.1f}s", ha="center",
                    va="center", fontsize=8.5,
                    color="white" if h > 1.5 else C_DGR, weight="bold")
        ax.text(-0.6, yy, name, ha="right", va="center", fontsize=9.5,
                color=C_DGR, linespacing=1.4)
        ax.text(start[i] + h + 0.7, yy, note, ha="left", va="center",
                fontsize=8.2, color=C_GRY)

    total_lo = lo.sum()
    total_hi = hi.sum()
    ax.axvline(total_lo, color=C_GRN, ls="--", lw=1.6)
    ax.axvline(total_hi, color=C_RED, ls="--", lw=1.6)
    ax.set_xlim(-14, max(total_hi * 1.35, 45))
    ax.set_ylim(-1.0, len(stages) - 0.2)
    ax.set_yticks([])
    ax.set_xlabel("从“场上事件发生”起算的累计时间（秒）", fontsize=11)
    ax.set_title("图 4  赔率跳动瞬间的延迟预算：你能在 5–7 秒内完成决策，"
                 "但庄家握着最后一道闸门",
                 fontsize=12.5, weight="bold", color=C_DGR)
    ax.grid(axis="x", alpha=0.25, ls="--")
    ax.text(total_lo + 0.4, len(stages) - 0.75,
            f"乐观总延迟 ≈ {total_lo:.1f}s", color=C_GRN, fontsize=9.5,
            weight="bold")
    ax.text(total_hi + 0.4, len(stages) - 0.75,
            f"保守总延迟 ≈ {total_hi:.1f}s\n（远超任何“瞬时”假设）",
            color=C_RED, fontsize=9.5, weight="bold", linespacing=1.4)
    save(fig, "fig4_latency_budget.png")

    write_csv("latency_budget.csv",
              ["阶段", "乐观(秒)", "保守(秒)", "说明"],
              [[s[0].replace("\n", " "), s[1], s[2], s[4]] for s in stages] +
              [["合计", total_lo, total_hi, "从场上事件到成交确认"]])
    RESULT["latency_lo"], RESULT["latency_hi"] = total_lo, total_hi


# ================================================================ 图 5 蒙特卡洛
def fig5_montecarlo():
    rng = np.random.default_rng(20260601)
    BANK0 = 10000.0
    BETS_PER_DAY = 5
    DAYS = 90
    N = 20000
    FLAT = 0.02          # 每注 2% 银行

    def sim(ev, platform_risk, frac_flat=FLAT, n=N, days=DAYS):
        paths = np.zeros((n, days + 1))
        paths[:, 0] = BANK0
        bank = np.full(n, BANK0)
        default_day = np.full(n, 10 ** 9)
        if platform_risk > 0:
            default_day = rng.integers(1, days + 1, size=n)
        for d in range(1, days + 1):
            for _ in range(BETS_PER_DAY):
                stake = bank * frac_flat
                win = rng.random(n) < 0.5 * (1 + ev) / 1.0  # 近似: EV 体现在胜率
                # 用更稳健的方式: 按 EV 直接构造乘数
                r = rng.random(n)
                # 以赔率 1.95 计, 胜率 q 使 EV = q*0.95 - (1-q) = ev
                q = (1 + ev) / 1.95
                q = min(max(q, 0.001), 0.999)
                win = r < q
                bank = np.where(win, bank + stake * 0.95, bank - stake)
            hit = default_day <= d
            bank = np.where(hit & ~np.isnan(bank), bank * 0.30, bank)
            default_day = np.where(hit, 10 ** 9, default_day)
            paths[:, d] = bank
        return paths

    A = sim(-0.102, 0.0)                    # 无 edge, 滚球水钱 11.4%
    B = sim(+0.020, 0.0)                    # 有 2% edge, 无平台风险
    Cpath = sim(+0.020, 0.25)               # 有 2% edge, 25% 概率平台跑路

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14.5, 6.0),
                                   gridspec_kw={"width_ratios": [1.55, 1]})

    def band(ax, paths, color, label, ls="-"):
        p05 = np.percentile(paths, 5, axis=0)
        p50 = np.percentile(paths, 50, axis=0)
        p95 = np.percentile(paths, 95, axis=0)
        x = np.arange(paths.shape[1])
        ax.fill_between(x, p05, p95, color=color, alpha=0.14)
        ax.plot(x, p50, color=color, lw=2.4, ls=ls, label=label)

    band(ax1, A, C_RED, "A. 无 edge（滚球水钱 11.4%）— 中位数路径")
    band(ax1, B, C_GRN, "B. 假设真有 +2% edge，无平台风险")
    band(ax1, Cpath, C_ORG, "C. +2% edge，但 25% 概率平台跑路/拒付")
    ax1.axhline(BANK0, color=C_GRY, lw=1, ls=":")
    ax1.set_yscale("log")
    ax1.set_xlabel("交易日（每天 5 注，每注 2% 银行）", fontsize=11)
    ax1.set_ylabel("银行余额（对数刻度）", fontsize=11)
    ax1.set_title("图 5a  蒙特卡洛 2 万次：三条路径的中位数与 5–95 分位",
                  fontsize=12, weight="bold", color=C_DGR)
    ax1.legend(fontsize=9, loc="lower left")
    ax1.grid(alpha=0.25, ls="--")

    defs = [
        ("A 无 edge\n11.4% 水钱", (A[:, -1] > BANK0).mean() * 100, C_RED),
        ("B +2% edge\n无平台风险", (B[:, -1] > BANK0).mean() * 100, C_GRN),
        ("C +2% edge\n25% 跑路风险", (Cpath[:, -1] > BANK0).mean() * 100, C_ORG),
    ]
    ax2.bar([d[0] for d in defs], [d[1] for d in defs],
            color=[d[2] for d in defs], width=0.55)
    for i, d in enumerate(defs):
        ax2.text(i, d[1] + 1.2, f"{d[1]:.1f}%", ha="center", fontsize=11,
                 weight="bold", color=C_DGR)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("90 天后仍盈利的概率 (%)", fontsize=11)
    ax2.set_title("图 5b  盈利概率\n（平台风险不是“尾部”，而是主风险）",
                  fontsize=12, weight="bold", color=C_DGR)
    ax2.grid(axis="y", alpha=0.25, ls="--")

    ax2.text(1, 30,
             "即使模型真的有 2% edge，\n一次跑路就抹掉 70% 本金，\n"
             "而 A/B/C 三种情形的赔率、限额、\n拒单环境在实战中会同时出现。",
             fontsize=9, color=C_RED, weight="bold", ha="center",
             bbox=dict(fc="#fdecea", ec=C_RED, boxstyle="round,pad=0.45"))
    save(fig, "fig5_montecarlo.png")

    rows = []
    for nm, paths in [("A 无edge(11.4%水钱)", A), ("B +2%edge无平台风险", B),
                      ("C +2%edge+25%跑路", Cpath)]:
        med = np.median(paths[:, -1])
        rows.append([nm, round(med, 0),
                     round(np.percentile(paths[:, -1], 5), 0),
                     round(np.percentile(paths[:, -1], 95), 0),
                     round((paths[:, -1] > BANK0).mean() * 100, 2)])
    write_csv("montecarlo.csv",
              ["情形", "90日余额中位数", "5分位", "95分位", "盈利概率%"], rows)
    RESULT["p_profit_A"] = (A[:, -1] > BANK0).mean()
    RESULT["p_profit_B"] = (B[:, -1] > BANK0).mean()
    RESULT["p_profit_C"] = (Cpath[:, -1] > BANK0).mean()


# ================================================================ 图 6 可行性热力图
def fig6_heatmap():
    layers = ["数据采集\n(实时倍率)", "信号生成\n(去水/找 edge)",
              "AI 决策\n(JEV 调用)", "下单执行\n(自动投注)",
              "资金与合规\n(出入金/代理)"]
    dims = ["技术可行性", "经济学可行性", "法律可行性", "长期运营可行性"]
    # 0=完全不可行 5=完全可行 (作者基于本报告证据的主观评分)
    M = np.array([
        [4, 3, 2, 2],   # 数据采集: 抓取能做, 但违约/违法/封禁
        [3, 1, 3, 1],   # 信号生成: 技术上可做, 经济学上被水钱吃掉
        [4, 1, 4, 2],   # JEV 决策: 调用可行, 对定价无帮助
        [2, 1, 0, 0],   # 下单执行: 无 API + 延迟闸门 + 违法
        [1, 0, 0, 0],   # 资金: 跑分/代充 = 帮信罪
    ])
    fig, ax = plt.subplots(figsize=(11.5, 6.2))
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list(
        "feas", ["#a52a2a", "#e67e22", "#f4d03f", "#7dcea0", "#27ae60"])
    im = ax.imshow(M, cmap=cmap, vmin=0, vmax=5, aspect="auto")
    ax.set_xticks(range(len(dims)))
    ax.set_xticklabels(dims, fontsize=11)
    ax.set_yticks(range(len(layers)))
    ax.set_yticklabels(layers, fontsize=10.5)
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            ax.text(j, i, f"{M[i, j]}", ha="center", va="center",
                    fontsize=15, weight="bold",
                    color="white" if M[i, j] <= 2 else "#1a1a1a")
    ax.set_title("图 6  五层架构 × 四维可行性评分（0 = 不可行，5 = 可行）\n"
                 "越是靠近“钱”的层，法律可行性越先归零",
                 fontsize=12.5, weight="bold", color=C_DGR)
    fig.colorbar(im, ax=ax, shrink=0.8, label="可行性评分")
    ax.set_xticks(np.arange(-0.5, len(dims), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(layers), 1), minor=True)
    ax.grid(which="minor", color="white", lw=2)
    ax.tick_params(which="minor", length=0)
    save(fig, "fig6_feasibility_heatmap.png")

    rows = [[l.replace("\n", " ")] + [safe_int(v) for v in M[i]]
            for i, l in enumerate(layers)]
    write_csv("feasibility_scores.csv", ["架构层"] + dims, rows)


# ================================================================ 图 7 赔率跳动瞬时时序
def fig7_sequence():
    fig, ax = plt.subplots(figsize=(13, 6.6))
    ax.set_xlim(-1, 1)
    ax.set_ylim(-0.6, 9.4)
    ax.axis("off")
    actors = ["球场", "数据商\n(Sportradar)", "庄家\n交易系统", "你的爬虫\n+ 模型", "你的账户"]
    xs = np.linspace(-0.82, 0.82, len(actors))
    for x, a in zip(xs, actors):
        ax.add_patch(FancyBboxPatch((x - 0.115, 8.55), 0.23, 0.62,
                                    boxstyle="round,pad=0.02,rounding_size=0.05",
                                    fc=C_DGR, ec=C_DGR))
        ax.text(x, 8.86, a, ha="center", va="center", color="white",
                fontsize=9.5, weight="bold", linespacing=1.4)
        ax.plot([x, x], [-0.35, 8.5], color="#bbbbbb", lw=1, ls="--", zorder=0)

    def msg(y, i, j, text, color, ls="-", lw=1.8, dy=0.28, fs=9):
        ax.annotate("", xy=(xs[j], y), xytext=(xs[i], y),
                    arrowprops=dict(arrowstyle="-|>", color=color, lw=lw,
                                    linestyle=ls, mutation_scale=13,
                                    shrinkA=3, shrinkB=3))
        if text:
            ax.text((xs[i] + xs[j]) / 2, y + dy, text, ha="center", fontsize=fs,
                    color=color, weight="bold")

    msg(8.15, 0, 1, "① 进球 / 红牌 / 角球", C_DGR, dy=-0.36)
    msg(7.35, 1, 2, "② 事件数据上行（2–5s 已耗掉）", C_DGR)
    msg(6.70, 2, 3, "③ 新赔率下发（0.5–3s）", C_BLU)

    ax.plot([xs[2] - 0.02, xs[3]], [5.95, 5.95], color=C_RED, lw=1.6, ls=":")
    ax.text(xs[2] - 0.02, 5.63,
            "③′ 危险时刻：庄家直接“停盘”(suspend) —— 此时你的信号\n"
            "     读到的是冻结或被撤下的价格",
            fontsize=9, color=C_RED, weight="bold", va="center", ha="left")

    ax.text(xs[3] + 0.02, 4.95,
            "④ JEV / 模型决策（0.3–3s）\n"
            "     ← 整条链上唯一属于你的环节",
            fontsize=9.5, color=C_ORG, weight="bold", va="center", ha="left")
    msg(4.20, 3, 4, "⑤ 提交下注请求（0.5–2s）", C_ORG)

    ax.text((xs[4] + xs[2]) / 2, 3.30,
            "⑥/⑦ 请求进入庄家风控队列 → 接受延迟 5–20s：\n"
            "     庄家用最新行情复查你的单子",
            fontsize=9.5, color=C_RED, weight="bold", ha="center", va="center")
    ax.annotate("", xy=(xs[2] + 0.02, 2.72), xytext=(xs[4], 2.72),
                arrowprops=dict(arrowstyle="-|>", color=C_RED, lw=1.8,
                                mutation_scale=13, shrinkA=3, shrinkB=3))

    msg(1.95, 2, 4, "⑧a 价格已变 → 改价（要求你接受更差价）", C_RED, dy=-0.34)
    msg(1.20, 2, 4, "⑧b 判定为“优势玩家” → 拒单 / 限额 / 标记", C_RED, dy=-0.34)
    msg(0.45, 2, 4, "⑧c 少数情况按旧价成交（庄家认为对自己有利）", C_GRN, dy=-0.34)
    ax.text(0, -0.52,
            "结构性不对称：你能“看见”跳价，但成交与否由对手方单方面决定 —— "
            "这在币圈等同于“交易所可以事后撤销你的成交”。",
            ha="center", fontsize=10.5, color=C_RED, weight="bold")
    ax.set_title("图 7  “倍率跳动瞬间”的真实时序：你只拥有第 ④ 步",
                 fontsize=12.5, weight="bold", color=C_DGR, pad=14)
    save(fig, "fig7_odds_tick_sequence.png")


# ================================================================ 图 8 JEV 定位
def fig8_jev_position():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.8),
                                   gridspec_kw={"width_ratios": [1.05, 1]})

    legs = ["赔率/盘口\n数值预测", "校准概率\n(去水后)", "软信息\n(伤停/新闻)",
            "决策与仓位\n(Kelly)", "下单执行"]
    jev_fit = [1, 2, 5, 4, 1]
    ax1.barh(np.arange(len(legs)), jev_fit,
             color=[C_RED if v <= 2 else (C_ORG if v == 4 else C_GRN) for v in jev_fit],
             height=0.6)
    ax1.set_yticks(np.arange(len(legs)))
    ax1.set_yticklabels(legs, fontsize=10.5)
    ax1.invert_yaxis()
    ax1.set_xlim(0, 5.6)
    ax1.set_xlabel("JEV（决策模型）的适配度：0 = 不该用，5 = 很适合", fontsize=10.5)
    ax1.set_title("图 8a  JEV 不是一个定价模型\n"
                  "它是“对非结构化状态做带校准的离散判断”",
                  fontsize=12, weight="bold", color=C_DGR)
    for i, v in enumerate(jev_fit):
        ax1.text(v + 0.1, i, ["", "很不适合", "不适合", "一般", "适合", "很适合"][v],
                 va="center", fontsize=9, color=C_GRY)
    ax1.grid(axis="x", alpha=0.25, ls="--")

    # 延迟占比
    labels = ["数据采集/推送", "JEV 决策", "下单与接受延迟", "风控/改价/拒单"]
    vals = [6.5, 1.6, 8.5, 4.0]
    colors = [C_BLU, C_ORG, C_RED, C_DGR]
    wedges, _, autot = ax2.pie(vals, colors=colors, autopct="%1.1f%%",
                               startangle=110, textprops={"fontsize": 10},
                               wedgeprops={"width": 0.42, "edgecolor": "white"})
    ax2.legend(wedges, [f"{l}（约 {v}s）" for l, v in zip(labels, vals)],
               loc="center left", bbox_to_anchor=(0.98, 0.5), fontsize=9.5)
    ax2.set_title("图 8b  延迟预算中 JEV 只占约 8%\n"
                  "把它放在关键路径上并不带来优势",
                  fontsize=12, weight="bold", color=C_DGR)
    ax2.text(0, -1.45,
             "把 JEV 放在关键路径 = 用 8% 的延迟换 0 的定价能力；\n"
             "把 JEV 放在“事后解释/软信息过滤”位置 = 唯一有价值的用法。",
             ha="center", fontsize=9.5, color=C_GRY, linespacing=1.6)
    save(fig, "fig8_jev_position.png")


# ================================================================ 主流程
def main():
    print("生成图表与数据 ...")
    fig1_ecosystem()
    fig2_margin_ev()
    fig3_required_edge()
    fig4_latency()
    fig5_montecarlo()
    fig6_heatmap()
    fig7_sequence()
    fig8_jev_position()

    print("\n关键结论数字：")
    print(f"  在盘水钱 11.4% 时单注期望 EV = {RESULT['ev_inplay']*100:.2f}%")
    print(f"  乐观总延迟 {RESULT['latency_lo']:.1f}s / 保守总延迟 {RESULT['latency_hi']:.1f}s")
    print(f"  90 天盈利概率：无 edge {RESULT['p_profit_A']*100:.1f}% / "
          f"+2% edge 无风险 {RESULT['p_profit_B']*100:.1f}% / "
          f"+2% edge+25% 跑路 {RESULT['p_profit_C']*100:.1f}%")


if __name__ == "__main__":
    main()

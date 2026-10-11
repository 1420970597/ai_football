#!/usr/bin/env python3
"""Rebuild all audit CSV derivations and three measured/theoretical figures."""
import csv
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CHINESE_FONT = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'
FIGURE_DPI = 170
BAR_LABEL_OFFSET_CNY = 2
PLOT_MARGIN_CNY = 75
COLORS = ('#c44b3f','#c08035','#4575b4','#25876a','#213d58')
SCENARIO_LABELS = ('现有代码：全比分/旧负号','全比分/修正负号','剩余比分/旧负号',
                   '剩余比分/修正负号','场馆真实结算')


def read(name):
    with (ROOT/'data'/name).open(encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def main():
    for script in ('audit.py','reproduce.py'):
        subprocess.run([sys.executable,str(ROOT/'scripts'/script)],check=True)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties, fontManager
    if not Path(CHINESE_FONT).exists():
        raise RuntimeError('Required Chinese font is absent; refuse to overwrite figures')
    fontManager.addfont(CHINESE_FONT)
    font=FontProperties(fname=CHINESE_FONT)
    plt.rcParams['font.family']=font.get_name()
    plt.rcParams['axes.unicode_minus']=False
    rows=read('settlement_scenarios.csv')
    if len(rows)!=len(SCENARIO_LABELS): raise ValueError('settlement scenario count mismatch')
    fig,ax=plt.subplots(figsize=(10,5))
    values=[float(r['profit']) for r in rows]
    bars=ax.barh(SCENARIO_LABELS,values,color=COLORS)
    ax.axvline(0,color='#455a64',linewidth=1)
    ax.invert_yaxis()
    for bar,value in zip(bars,values):
        ax.text(value+(BAR_LABEL_OFFSET_CNY if value>=0 else -BAR_LABEL_OFFSET_CNY),
                bar.get_y()+bar.get_height()/2,
                '%+.3f 元'%value,va='center',ha='left' if value>=0 else 'right')
    ax.set_xlim(min(values)-PLOT_MARGIN_CNY,max(values)+PLOT_MARGIN_CNY)
    ax.set_xlabel('同一160笔自动订单，使用场馆成交赔率与金额重算净收益（元）')
    ax.set_title('结算口径制造了约%.3f元的虚假盈利差额' % (values[0]-values[-1]))
    ax.grid(axis='x',alpha=.18)
    fig.tight_layout()
    fig.savefig(ROOT/'images'/'receipt-attribution.png',dpi=FIGURE_DPI)
    plt.close(fig)
    rows=read('actual_by_date.csv')
    if not rows: raise ValueError('empty daily measurements')
    fig,ax=plt.subplots(figsize=(9,4))
    profits=[float(r['profit']) for r in rows]
    ax.bar([r['utc_bet_date'] for r in rows],profits,color=[COLORS[3] if v>=0 else COLORS[0] for v in profits])
    ax.axhline(0,color='#455a64',linewidth=1)
    for i,value in enumerate(profits):
        ax.text(i,value,'%+.2f'%value,ha='center',va='bottom' if value>=0 else 'top')
    ax.set_xlabel('UTC下注日期；仅统计已从场馆读取到结算的160笔匹配自动订单')
    ax.set_ylabel('实际净收益（元）')
    ax.set_title('真实亏损与波动；不混入5笔来源未匹配订单')
    ax.grid(axis='y',alpha=.18)
    fig.tight_layout()
    fig.savefig(ROOT/'images'/'actual-profit.png',dpi=FIGURE_DPI)
    plt.close(fig)
    rows=read('shrink_reproduction.csv')
    fig,ax=plt.subplots(figsize=(8,4))
    x=[float(r['input_model']) for r in rows]
    ax.plot(x,[float(r['production']) for r in rows],'o-',color=COLORS[0],label='现有单元素调用：全部变为100%')
    ax.plot(x,[float(r['scalar_expected']) for r in rows],'o-',color=COLORS[3],label='正确标量/完整二元向量收缩')
    ax.set_ylim(0,1.08)
    ax.set_xlabel('输入模型概率（理论诊断格点，不是实盘测量）')
    ax.set_ylabel('收缩输出概率')
    ax.set_title('经济学风控的归一化调用错误；先验50%，权重1/3')
    ax.legend(loc='lower right')
    ax.grid(alpha=.18)
    fig.tight_layout()
    fig.savefig(ROOT/'images'/'shrink-bug.png',dpi=FIGURE_DPI)
    plt.close(fig)
    print('generated 3 PNG figures; derived CSVs and diagnostics rebuilt')


if __name__=='__main__':
    main()

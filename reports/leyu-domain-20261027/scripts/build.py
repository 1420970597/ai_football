"""Reuse the established offline report renderer; compile Mermaid first."""
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT.parent / 'jiebao-score-reference/scripts/md_to_html.py'
render = runpy.run_path(str(RENDERER))['render']
render.__globals__['ROOT'] = ROOT
source = (ROOT / 'REPORT.md').read_text(encoding='utf-8')
output = render(source).replace('捷报比分网界面与系统能力调研', '乐鱼官方 APK、域名与采集恢复核验')
output = output.replace('</style>', 'p,li,td,th,code{overflow-wrap:anywhere}</style>')
(ROOT / 'REPORT.html').write_text(output, encoding='utf-8')

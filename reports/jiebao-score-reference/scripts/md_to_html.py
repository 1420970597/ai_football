#!/usr/bin/env python3
"""Render this report's Markdown subset to responsive HTML with Mermaid fallback."""

from __future__ import annotations

import html
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "REPORT.md"
TARGET = ROOT / "REPORT.html"


def inline(text: str) -> str:
    escaped = html.escape(text, quote=True)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)\)",
        r'<img alt="\1" src="\2">',
        escaped,
    )
    return re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" rel="noopener noreferrer">\1</a>',
        escaped,
    )


def table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def render(source: str) -> str:
    lines = source.splitlines()
    body: list[str] = []
    paragraph: list[str] = []
    index = 0
    diagram_index = 0

    def flush_paragraph() -> None:
        if paragraph:
            body.append(f"<p>{inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            flush_paragraph()
            language = line[3:].strip()
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            text = "\n".join(code)
            if language == "mermaid":
                diagram_index += 1
                svg = ROOT / "images" / f"flow-{diagram_index}.svg"
                if not svg.is_file():
                    raise FileNotFoundError("Compile the local Mermaid SVG before generating the report")
                body.append(f'<div class="diagram-scroll"><img src="images/flow-{diagram_index}.svg" alt="系统流程图，可横向滚动阅读"></div>')
                body.append(f'<details><summary>查看可审阅的 Mermaid 源码</summary><pre class="mermaid-source"><code>{html.escape(text)}</code></pre></details>')
            else:
                body.append(f'<pre><code>{html.escape(text)}</code></pre>')
        elif line.startswith("|"):
            flush_paragraph()
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].startswith("|"):
                rows.append(table_row(lines[index]))
                index += 1
            rows = [row for row in rows if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in row)]
            if rows:
                head, *rest = rows
                body.append("<div class=\"table-scroll\"><table><thead><tr>")
                body.extend(f"<th>{inline(cell)}</th>" for cell in head)
                body.append("</tr></thead><tbody>")
                for row in rest:
                    body.append("<tr>")
                    body.extend(f"<td>{inline(cell)}</td>" for cell in row)
                    body.append("</tr>")
                body.append("</tbody></table></div>")
            continue
        elif re.match(r"^#{1,6} ", line):
            flush_paragraph()
            depth = len(line) - len(line.lstrip("#"))
            body.append(f"<h{depth}>{inline(line[depth + 1:])}</h{depth}>")
        elif line.startswith("- "):
            flush_paragraph()
            body.append("<ul>")
            while index < len(lines) and lines[index].startswith("- "):
                body.append(f"<li>{inline(lines[index][2:])}</li>")
                index += 1
            body.append("</ul>")
            continue
        elif line.startswith("> "):
            flush_paragraph()
            body.append(f"<blockquote><p>{inline(line[2:])}</p></blockquote>")
        elif line.strip():
            paragraph.append(line.strip())
        else:
            flush_paragraph()
        index += 1

    flush_paragraph()
    content = "\n".join(body)
    return f'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>捷报比分网界面与系统能力调研</title>
<style>
body{{margin:0;color:#202a35;background:#f5f7f8;font:16px/1.65 system-ui,"Microsoft YaHei",sans-serif}}
main{{max-width:1120px;margin:auto;padding:32px 24px;background:#fff}}
h1,h2,h3{{line-height:1.25;color:#15283a}}h1{{font-size:32px}}h2{{margin-top:36px;border-bottom:1px solid #dce2e6;padding-bottom:8px}}
p,li{{max-width:84ch}}a{{color:#087b73}}img{{max-width:100%;height:auto;display:block;margin:20px auto}}
.table-scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;min-width:760px}}th,td{{text-align:left;padding:10px;border:1px solid #d9e0e4;vertical-align:top}}th{{background:#edf3f2}}
.diagram-scroll{{overflow:auto;max-width:100%;border:1px solid #dce2e6}}.diagram-scroll img{{max-width:none;width:auto;margin:8px 0}}.mermaid-source{{font-size:12px}}
pre{{padding:14px;background:#f2f4f5;overflow:auto;border-left:3px solid #13877d;white-space:pre-wrap}}code{{font-family:ui-monospace,monospace}}blockquote{{border-left:3px solid #e0a24a;margin-left:0;padding-left:16px;color:#53616c}}
@media(max-width:640px){{main{{padding:20px 14px}}h1{{font-size:26px}}}}
</style><main>{content}</main>
</html>'''


if __name__ == "__main__":
    TARGET.write_text(render(SOURCE.read_text(encoding="utf-8")), encoding="utf-8")

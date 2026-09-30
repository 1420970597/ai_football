# -*- coding: utf-8 -*-
"""
极简 Markdown -> 单文件 HTML 渲染器（无第三方依赖）

仅覆盖本报告实际用到的语法子集：
  标题 / 段落 / GFM 表格 / 围栏代码块 / 引用块 / 有序·无序列表 /
  图片 / 行内 code / 加粗 / 斜体 / 链接 / 分隔线 / $$ 公式块

用法:  python3 scripts/md_to_html.py REPORT.md REPORT.html
"""
import html
import os
import re
import sys

INLINE_CODE = re.compile(r"`([^`]+)`")
DISPLAY_MATH = re.compile(r"^\$\$(.+?)\$\$$")
INLINE_MATH = re.compile(r"\$(?!\$)([^$\n]+?)\$(?!\$)")
IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
ITAL = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def _stash_out(text, stash, regex):
    """把匹配内容换为占位符并放入 stash，避免后续标记干扰。"""
    return regex.sub(lambda m: stash.append(m.group(1)) or
                     "\x00%d\x00" % (len(stash) - 1), text)


def _stash_in(text, stash, cls=""):
    for i, code in enumerate(stash):
        body = html.escape(code, quote=False)
        if cls:
            out = '<span class="%s">%s</span>' % (cls, body)
        else:
            out = "<code>%s</code>" % body
        text = text.replace("\x00%d\x00" % i, out)
    return text


def inline(text):
    """行内元素。先抽出 code / 行内公式，避免其中的特殊字符被误解析。"""
    codes, maths = [], []
    text = _stash_out(text, codes, INLINE_CODE)
    text = _stash_out(text, maths, INLINE_MATH)
    text = html.escape(text, quote=False)
    text = IMG.sub(
        lambda m: '<img alt="%s" src="%s">' % (html.escape(m.group(1), True), m.group(2)),
        text,
    )
    text = LINK.sub(
        lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)), text
    )
    text = BOLD.sub(r"<strong>\1</strong>", text)
    text = ITAL.sub(r"<em>\1</em>", text)
    text = _stash_in(text, maths, cls="imath")
    return _stash_in(text, codes)


def split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def is_sep(line):
    s = line.strip()
    if not s.startswith("|"):
        return False
    cells = split_row(s)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def aligns_of(sep_line):
    out = []
    for c in split_row(sep_line):
        left, right = c.startswith(":"), c.endswith(":")
        if left and right:
            out.append("center")
        elif right:
            out.append("right")
        elif left:
            out.append("left")
        else:
            out.append("")
    return out


def convert(md):
    lines = md.replace("\r\n", "\n").split("\n")
    out, i, n = [], 0, len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # ---- 围栏代码块（mermaid 单独处理）
        if stripped.startswith("```"):
            lang = stripped[3:].strip().lower()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            body = "\n".join(buf)
            if lang in ("mermaid", "mmd"):
                # Mermaid 必须保留原始文本（含 <、>、&），\n由浏览器解析
                out.append('<div class="mmd"><pre class="mermaid">%s</pre></div>'
                           % html.escape(body, quote=False))
            else:
                cls = ' class="language-%s"' % lang if lang else ""
                out.append("<pre><code%s>%s</code></pre>"
                           % (cls, html.escape(body)))
            continue

        # ---- $$ 公式块（独占一行的 $$...$$，或 \n$$\n 围栏形式）
        m_disp = DISPLAY_MATH.match(stripped)
        if m_disp:
            out.append('<div class="math">%s</div>'
                       % html.escape(m_disp.group(1).replace("\\qquad", "   ")))
            i += 1
            continue
        if stripped == "$$":
            i += 1
            buf = []
            while i < n and lines[i].strip() != "$$":
                buf.append(lines[i])
                i += 1
            i += 1
            out.append('<div class="math">%s</div>' % html.escape("\n".join(buf)))
            continue

        # ---- 分隔线
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            out.append("<hr>")
            i += 1
            continue

        # ---- 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            lvl = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lvl, inline(m.group(2)), lvl))
            i += 1
            continue

        # ---- 表格
        if stripped.startswith("|") and i + 1 < n and is_sep(lines[i + 1]):
            header = split_row(stripped)
            aligns = aligns_of(lines[i + 1])
            i += 2
            body = []
            while i < n and lines[i].strip().startswith("|"):
                body.append(split_row(lines[i]))
                i += 1
            th = "".join(
                '<th%s>%s</th>'
                % (' class="%s"' % aligns[k] if k < len(aligns) and aligns[k] else "", inline(c))
                for k, c in enumerate(header)
            )
            trs = []
            for row in body:
                tds = "".join(
                    "<td%s>%s</td>"
                    % (' class="%s"' % aligns[k] if k < len(aligns) and aligns[k] else "",
                       inline(c))
                    for k, c in enumerate(row)
                )
                trs.append("<tr>%s</tr>" % tds)
            out.append(
                '<div class="tw"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
                % (th, "".join(trs))
            )
            continue

        # ---- 引用块
        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>" % convert("\n".join(buf)))
            continue

        # ---- 列表
        if re.match(r"^\s*([-*+]|\d+\.)\s+", line):
            ordered = bool(re.match(r"^\s*\d+\.\s+", line))
            items = []
            while i < n and re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i]):
                items.append(re.sub(r"^\s*([-*+]|\d+\.)\s+", "", lines[i]))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(
                "<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % inline(x) for x in items), tag)
            )
            continue

        # ---- 空行
        if not stripped:
            i += 1
            continue

        # ---- 段落
        buf = []
        while i < n:
            s = lines[i].strip()
            if (not s or s.startswith("```") or s == "$$"
                    or DISPLAY_MATH.match(s) or s.startswith(">")
                    or re.match(r"^#{1,6}\s", s) or re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s)
                    or re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i])
                    or (s.startswith("|") and i + 1 < n and is_sep(lines[i + 1]))):
                break
            buf.append(s)
            i += 1
        if buf:
            out.append("<p>%s</p>" % inline(" ".join(buf)))

    return "\n".join(out)


CSS = """
:root{--fg:#1c2733;--mut:#5d6b7a;--line:#d9e0e7;--bg:#fff;--acc:#2c6fa8;
      --red:#b3271e;--redbg:#fdf0ee;--amb:#8a5a00;--ambbg:#fdf6e6;
      --grn:#1e7b46;--grnbg:#eef9f2;--code:#f4f6f8}
*{box-sizing:border-box}
body{margin:0;background:#eef1f4;color:var(--fg);
     font:16px/1.85 -apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",
          "WenQuanYi Zen Hei","Noto Sans CJK SC",sans-serif;}
.wrap{max-width:980px;margin:0 auto;background:var(--bg);
      padding:56px 68px 90px;box-shadow:0 2px 18px rgba(0,0,0,.07)}
h1{font-size:1.95em;line-height:1.4;margin:.2em 0 .6em;padding-bottom:.5em;
   border-bottom:3px solid var(--acc);color:#12212e}
h2{font-size:1.42em;margin:2.4em 0 .8em;padding:.35em 0 .35em .7em;
   border-left:5px solid var(--acc);background:#f7fafc;color:#12212e}
h3{font-size:1.16em;margin:1.9em 0 .6em;color:#1d3a52}
h4{font-size:1.04em;margin:1.5em 0 .5em;color:#2b4a63}
p{margin:.85em 0}
a{color:var(--acc);text-decoration:none;border-bottom:1px solid #b9d3e6}
a:hover{border-bottom-color:var(--acc)}
hr{border:0;border-top:1px solid var(--line);margin:2.4em 0}
strong{color:#0f1e2b}
code{background:var(--code);padding:.14em .42em;border-radius:4px;
     font:0.88em/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
     color:#8a2b4a;border:1px solid #e6eaee}
pre{background:#1e2a36;color:#e6edf3;padding:18px 20px;border-radius:8px;
    overflow:auto;font-size:13.5px;line-height:1.7}
pre code{background:none;border:0;color:inherit;padding:0;font-size:1em}
.math{background:var(--code);border:1px solid var(--line);border-left:4px solid var(--acc);
      padding:14px 18px;margin:1.2em 0;white-space:pre-wrap;word-break:break-word;
      overflow-x:auto;border-radius:6px;
      font:14px/1.9 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#243342}
.imath{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
       font-size:.95em;color:#8a2b4a;background:#f4f6f8;padding:.05em .28em;border-radius:3px}
.mmd{margin:1.6em 0;padding:10px 6px;border:1px solid var(--line);border-radius:8px;
     background:#fcfdfe;overflow-x:auto}
pre.mermaid{background:none;border:0;color:#1c2733;padding:0;margin:0;
            font-size:13px;text-align:center;display:block}
pre.mermaid.mermaid-fallback{background:#f7f9fb;border:1px dashed #c3ccd6;border-radius:6px;
            padding:14px 16px;text-align:left;color:#33475b;font-size:12.5px;
            white-space:pre;overflow:auto}
pre.mermaid.mermaid-fallback::before{content:"⚠ Mermaid 未渲染（离线）—— 以下为图源文本";
            display:block;margin-bottom:8px;color:var(--amb);font-weight:600;font-size:12px}
pre.mermaid svg{max-width:100%;height:auto}
blockquote{margin:1.2em 0;padding:14px 20px;background:#f8f9fb;
           border-left:4px solid var(--acc);border-radius:0 6px 6px 0;color:#33475b}
blockquote p{margin:.4em 0}
blockquote blockquote{margin:.6em 0;background:#fff}
.tw{overflow-x:auto;margin:1.3em 0;
    border:1px solid var(--line);border-radius:8px;background:#fff}
table{border-collapse:collapse;width:100%;font-size:14.5px}
th,td{padding:9px 13px;border-bottom:1px solid var(--line);text-align:left;
      vertical-align:top}
th{background:#f1f5f9;font-weight:600;color:#1d3a52;white-space:nowrap}
tbody tr:nth-child(even){background:#fafcfd}
tbody tr:last-child td{border-bottom:0}
.center{text-align:center}.right{text-align:right}
img{max-width:100%;height:auto;display:block;margin:1.6em auto;
    border:1px solid var(--line);border-radius:8px;background:#fff}
ul,ol{margin:.8em 0;padding-left:1.7em}
li{margin:.35em 0}
.tag{display:inline-block;padding:.1em .55em;border-radius:4px;font-size:.86em;
     font-weight:600;white-space:nowrap}
@media print{body{background:#fff}.wrap{box-shadow:none;max-width:none;padding:0}
  h2{page-break-after:avoid}table,img,pre,.math{page-break-inside:avoid}}
@media(max-width:760px){.wrap{padding:26px 18px 60px}h1{font-size:1.5em}}
"""


def _safe_path(raw, must_exist, what):
    """把用户传入的路径限定在当前工作目录内，并检查存在性。"""
    base = os.path.realpath(os.getcwd())
    full = os.path.realpath(os.path.abspath(raw))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("%s 路径必须位于当前工作目录内: %s" % (what, raw))
    if must_exist and not os.path.isfile(full):
        raise ValueError("%s 文件不存在: %s" % (what, raw))
    return full


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise ValueError("无法读取 %s: %s" % (path, exc)) from exc


def _write_text(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise ValueError("无法创建输出目录 %s: %s" % (parent, exc)) from exc
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        raise ValueError("无法写入 %s: %s" % (path, exc)) from exc


def render_mermaid(md, doc_head_extra):
    """判断是否有 mermaid，并返回需注入 <head> 的运行时片段。"""
    if 'class="mermaid"' not in md:
        return ""
    return MERMAID_RUNTIME


MERMAID_RUNTIME = """<script type="module">
/* Mermaid 运行时：CDN 可用则渲染；离线/失败则优雅降级为可读代码块。
   关键：渲染成功必须清除降级标记，避免出现「已渲染但仍显示未渲染警告」。 */
const CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
const SEL = "pre.mermaid";

function degrade(reason) {
  var n = 0;
  document.querySelectorAll(SEL).forEach(function (el) {
    if (el.querySelector("svg")) return;   // 已成功渲染的不动
    el.classList.add("mermaid-fallback");
    n++;
  });
  if (n) console.warn("Mermaid fallback:", reason, "(" + n + " blocks)");
  return n;
}

function clearFallback() {
  document.querySelectorAll(SEL).forEach(function (el) {
    el.classList.remove("mermaid-fallback");
    el.removeAttribute("title");
  });
}

(function () {
  var settled = false;
  var timer = setTimeout(function () {
    if (!settled) degrade("加载超时（CDN 不可达）");
  }, 15000);

  import(CDN).then(function (mod) {
    var mermaid = mod.default;
    mermaid.initialize({
      startOnLoad: false,
      theme: "neutral",
      securityLevel: "strict",
      fontFamily: '-apple-system,"PingFang SC","Microsoft YaHei","WenQuanYi Zen Hei",sans-serif',
      flowchart: { useMaxWidth: true, htmlLabels: false },
      sequence: { useMaxWidth: true },
      state: { useMaxWidth: true }
    });
    return mermaid.run({ querySelector: SEL });
  }).then(function () {
    settled = true;
    clearTimeout(timer);
    clearFallback();                       // 成功后必须清除降级标记
    window.__mermaidStatus = { ok: true, blocks: document.querySelectorAll(SEL + " svg").length };
  }).catch(function (e) {
    settled = true;
    clearTimeout(timer);
    var msg = String((e && e.message) || e);
    degrade(msg);
    window.__mermaidStatus = { ok: false, error: msg };
  });
})();
</script>
"""


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    try:
        src = _safe_path(sys.argv[1], True, "输入")
        dst = _safe_path(sys.argv[2], False, "输出")
        md = _read_text(src)
    except ValueError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 2

    title = "体育博彩交易机器人可行性调查报告"
    m = re.search(r"^#\s+(.+)$", md, re.M)
    if m:
        title = re.sub(r"[`*]", "", m.group(1)).strip()

    body = convert(md)
    runtime = render_mermaid(body, None)
    doc = (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        "<title>%s</title>\n<style>%s</style>\n%s</head>\n<body>\n"
        "<main class=\"wrap\">\n%s\n</main>\n</body>\n</html>\n"
        % (html.escape(title), CSS, runtime, body)
    )
    try:
        _write_text(dst, doc)
    except ValueError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 3
    print("HTML ->", os.path.relpath(dst), "(%.1f KB)" % (len(doc.encode()) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())

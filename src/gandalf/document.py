"""spec.md as a self-contained HTML page, and as a PDF when Typst is installed.

GANDALF writes its own Markdown, so this reads only what GANDALF writes: headings, paragraphs,
bullet and numbered lists (one level of nesting), tables, code blocks, quotes, and inline code,
bold, italics, links and images. Every piece of text is escaped: spec.md quotes model output and
the target repo, and none of it may become markup. Links keep only http(s), mailto and relative
targets. The diagrams are inlined into the HTML, so the page works on its own.

The PDF is typeset by Typst (``pip install typst``, one package, no other
dependencies). Text reaches Typst as string literals, never as markup. Without the extra the
render writes spec.md and spec.html and says how to get the PDF.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from html import escape
from pathlib import Path

INSTALL_HINT = "pip install typst"
LOGO = Path(__file__).parent / "assets" / "gandalf.png"


class MissingTool(Exception):
    """The optional PDF typesetter isn't installed."""


# --- parsing ----------------------------------------------------------------------------------

INLINE = re.compile(r"`([^`]+)`|!\[([^\]]*)\]\(([^)\s]+)\)|\[([^\]]+)\]\(([^)\s]+)\)|\*\*(.+?)\*\*"
                    r"|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")


def inline(text: str) -> list:
    """Inline nodes: ("text", s), ("code", s), ("img", alt, src), ("link", href, nodes),
    ("strong", nodes), ("em", nodes)."""
    out, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos:m.start()]))
        code, alt, src, label, href, strong, em = m.groups()
        if code is not None:
            out.append(("code", code))
        elif src is not None:
            out.append(("img", alt, src))
        elif href is not None:
            out.append(("link", href, inline(label)))
        elif strong is not None:
            out.append(("strong", inline(strong)))
        else:
            out.append(("em", inline(em)))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out


LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", line)]


def blocks(md: str) -> list:
    """Block nodes: ("h", level, nodes), ("p", nodes), ("list", ordered, [(nodes, children)]),
    ("table", header, rows), ("code", lang, text), ("quote", blocks), ("hr",)."""
    lines = md.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            lang, body, i = line[3:].strip(), [], i + 1
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            out.append(("code", lang, "\n".join(body)))
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            out.append(("h", len(m.group(1)), inline(m.group(2).strip())))
            i += 1
            continue
        if re.match(r"^\s*(---|\*\*\*)\s*$", line):
            out.append(("hr",))
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and TABLE_RULE.match(lines[i + 1]):
            header, rows, i = _cells(line), [], i + 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append(("table", [inline(c) for c in header], [[inline(c) for c in r] for r in rows]))
            continue
        if line.startswith(">"):
            body = []
            while i < len(lines) and lines[i].startswith(">"):
                body.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            out.append(("quote", blocks("\n".join(body))))
            continue
        m = LIST_ITEM.match(line)
        if m and len(m.group(1)) == 0:
            ordered = m.group(2)[0].isdigit()
            items, i = [], i
            while i < len(lines):
                m = LIST_ITEM.match(lines[i])
                if m and len(m.group(1)) == 0:
                    items.append([m.group(3), []])
                    i += 1
                elif m and items:                          # nested item
                    items[-1][1].append(lines[i].strip())
                    i += 1
                elif lines[i].startswith("  ") and lines[i].strip() and items:   # continuation line
                    if items[-1][1]:
                        items[-1][1][-1] += " " + lines[i].strip()
                    else:
                        items[-1][0] += " " + lines[i].strip()
                    i += 1
                else:
                    break
            out.append(("list", ordered, [(inline(text), blocks("\n".join(sub)) if sub else []) for text, sub in items]))
            continue
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,6}\s|```|>|\s*([-*+]|\d+[.)])\s|\s*\|)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(("p", inline(" ".join(para))))
    return out


def anchor_id(heading: str) -> str:
    """A GitHub-style heading id: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", text)


def _safe_href(href: str) -> str | None:
    if re.match(r"^(https?:|mailto:)", href, re.I):
        return href
    if ":" in href.split("/")[0] or href.startswith("//"):
        return None                                   # javascript:, data:, protocol-relative ...
    return href


# --- HTML ---------------------------------------------------------------------------------------

def _html_inline(nodes: list, svgs: dict[str, str]) -> str:
    out = []
    for n in nodes:
        kind = n[0]
        if kind == "text":
            out.append(escape(n[1]))
        elif kind == "code":
            out.append(f"<code>{escape(n[1])}</code>")
        elif kind == "strong":
            out.append(f"<strong>{_html_inline(n[1], svgs)}</strong>")
        elif kind == "em":
            out.append(f"<em>{_html_inline(n[1], svgs)}</em>")
        elif kind == "link":
            href = _safe_href(n[1])
            label = _html_inline(n[2], svgs)
            out.append(f'<a href="{escape(href)}">{label}</a>' if href else label)
        elif kind == "img":
            alt, src = n[1], n[2]
            if src in svgs:
                out.append(f'<figure class="diagram">{svgs[src]}</figure>')
            else:
                out.append(escape(alt))
    return "".join(out)


def _html_blocks(items: list, svgs: dict[str, str]) -> str:
    out = []
    for b in items:
        kind = b[0]
        if kind == "h":
            plain = "".join(n[1] if n[0] in ("text", "code") else "" for n in b[2])
            out.append(f'<h{b[1]} id="{escape(anchor_id(plain))}">{_html_inline(b[2], svgs)}</h{b[1]}>')
        elif kind == "p":
            out.append(f"<p>{_html_inline(b[1], svgs)}</p>")
        elif kind == "list":
            tag = "ol" if b[1] else "ul"
            lis = "".join(f"<li>{_html_inline(text, svgs)}{_html_blocks(sub, svgs)}</li>" for text, sub in b[2])
            out.append(f"<{tag}>{lis}</{tag}>")
        elif kind == "table":
            head = "".join(f"<th>{_html_inline(c, svgs)}</th>" for c in b[1])
            rows = "".join("<tr>" + "".join(f"<td>{_html_inline(c, svgs)}</td>" for c in r) + "</tr>" for r in b[2])
            out.append(f'<div class="table"><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>')
        elif kind == "code":
            out.append(f"<pre><code>{escape(b[2])}</code></pre>")
        elif kind == "quote":
            out.append(f"<blockquote>{_html_blocks(b[1], svgs)}</blockquote>")
        elif kind == "hr":
            out.append("<hr>")
    return "\n".join(out)


CSS = """
:root { --paper:#fbfbfa; --ink:#1c2430; --muted:#5b6676; --rule:#dde2e8; --wash:#f2f4f7; --accent:#2f7a5b;
  --display:"Avenir Next","Segoe UI","Helvetica Neue",Arial,sans-serif;
  --body:Charter,"Iowan Old Style",Georgia,serif; --mono:Menlo,Consolas,"DejaVu Sans Mono",monospace; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --paper:#12161c; --ink:#e3e7ed; --muted:#9aa5b4; --rule:#2b323d; --wash:#1a2029; --accent:#62c196; color-scheme: dark; } }
:root[data-theme="dark"] { --paper:#12161c; --ink:#e3e7ed; --muted:#9aa5b4; --rule:#2b323d; --wash:#1a2029; --accent:#62c196; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.55 var(--body); }
main { max-width: 56rem; margin: 0 auto; padding: 2.5rem 16px 4rem; }
header.brand { display: flex; align-items: center; gap: 12px; }
header.brand img { width: 44px; height: 44px; }
h1, h2, h3, h4, table, .diagram figcaption { font-family: var(--display); }
h1 { font-size: 2em; line-height: 1.15; margin: 0 0 .3em; text-wrap: balance; }
h1 + p { color: var(--muted); font-size: .85em; padding-bottom: 1em; border-bottom: 2px solid var(--ink); }
h2 { font-size: 1.25em; margin: 2.2em 0 .6em; padding-top: .7em; border-top: 1px solid var(--rule); }
h3 { font-size: 1.02em; margin: 1.6em 0 .4em; }
p, li { margin: 0 0 .5em; }
ul, ol { padding-left: 1.3em; }
a { color: var(--accent); }
em { color: var(--muted); }
code { font-family: var(--mono); font-size: .86em; background: var(--wash); padding: .05em .3em; border-radius: 3px; overflow-wrap: anywhere; }
pre { background: var(--wash); padding: .8em 1em; border-radius: 6px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid var(--accent); background: var(--wash); margin: 1em 0; padding: .6em 1em; }
.table { overflow-x: auto; margin: .4em 0 1.1em; }
table { border-collapse: collapse; width: 100%; font-size: .86em; font-variant-numeric: tabular-nums; }
th { text-align: left; font-size: .82em; text-transform: uppercase; letter-spacing: .05em; color: var(--muted);
     border-bottom: 1.5px solid var(--ink); padding: .4em .6em; }
td { border-bottom: 1px solid var(--rule); padding: .45em .6em; vertical-align: top; }
figure.diagram { margin: 1em 0 1.4em; overflow-x: auto; background: #fff; border: 1px solid var(--rule); border-radius: 8px; padding: 8px; }
figure.diagram svg { display: block; max-width: 100%; height: auto; }
@media print { body { background: #fff; font-size: 10.5pt; } main { max-width: none; padding: 0; }
  h2, h3 { break-after: avoid; } tr, figure { break-inside: avoid; } }
"""


def page_html(md: str, title: str, svgs: dict[str, str], logo_uri: str | None = None) -> str:
    """A complete, self-contained page: the diagrams inline, no external requests."""
    brand = (f'<header class="brand"><img src="{logo_uri}" alt=""></header>' if logo_uri else "")
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{escape(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n<main>\n{brand}\n"
            f"{_html_blocks(blocks(md), svgs)}\n</main>\n</body>\n</html>\n")


# --- Typst --------------------------------------------------------------------------------------

def _ts(s: str) -> str:
    """A Typst string literal: the only way text reaches Typst, so nothing in it is markup."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _typ_inline(nodes: list, images: dict[str, str]) -> str:
    out = []
    for n in nodes:
        kind = n[0]
        if kind == "text":
            out.append(f"#{_ts(n[1])}")
        elif kind == "code":
            out.append(f"#raw({_ts(n[1])})")
        elif kind == "strong":
            out.append(f"#strong[{_typ_inline(n[1], images)}]")
        elif kind == "em":
            out.append(f"#emph[{_typ_inline(n[1], images)}]")
        elif kind == "link":
            href = _safe_href(n[1])
            label = _typ_inline(n[2], images)
            out.append(f"#link({_ts(href)})[{label}]" if href and href.startswith(("http", "mailto")) else label)
        elif kind == "img":
            alt, src = n[1], n[2]
            if src in images:
                path, size = images[src] if isinstance(images[src], tuple) else (images[src], "width: 100%")
                out.append(f"#image({_ts(path)}, {size}, alt: {_ts(alt)})")
            else:
                out.append(f"#{_ts(alt)}")
    return "".join(out)


def _typ_blocks(items: list, images: dict[str, str]) -> str:
    out = []
    for b in items:
        kind = b[0]
        if kind == "h":
            out.append(f"#heading(level: {b[1]})[{_typ_inline(b[2], images)}]")
        elif kind == "p":
            out.append(_typ_inline(b[1], images))
        elif kind == "list":
            fn = "enum" if b[1] else "list"
            parts = ", ".join(f"[{_typ_inline(text, images)}{(' ' + _typ_blocks(sub, images)) if sub else ''}]"
                              for text, sub in b[2])
            out.append(f"#{fn}({parts})")
        elif kind == "table":
            n = max([len(b[1])] + [len(r) for r in b[2]])
            head = ", ".join(f"[{_typ_inline(c, images)}]" for c in b[1] + [[]] * (n - len(b[1])))
            cells = ", ".join(f"[{_typ_inline(c, images)}]" for r in b[2] for c in (r + [[]] * (n - len(r)))[:n])
            out.append(f"#table(columns: {n}, table.header({head}){', ' + cells if cells else ''})")
        elif kind == "code":
            literal = '"' + b[2].replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
            out.append(f"#raw({literal}, block: true)")
        elif kind == "quote":
            out.append(f"#quote(block: true)[{_typ_blocks(b[1], images)}]")
        elif kind == "hr":
            out.append("#line(length: 100%, stroke: 0.5pt + luma(200))")
    return "\n\n".join(out)


# A4 less the margins below, in mm; an image is never taller than the page (a tall landing
# map would otherwise be clipped top and bottom)
TEXT_WIDTH_MM, MAX_IMAGE_HEIGHT_MM = 176, 236
SVG_SIZE = re.compile(r'<svg[^>]*\swidth="(\d+(?:\.\d+)?)"[^>]*\sheight="(\d+(?:\.\d+)?)"')


def image_size(path: Path) -> str:
    """The Typst size for an image: full width, or a capped height when full width would run
    past the page."""
    if path.suffix == ".svg":
        m = SVG_SIZE.search(path.read_text(errors="replace")[:600])
        if m and float(m.group(1)) > 0:
            scaled = float(m.group(2)) * TEXT_WIDTH_MM / float(m.group(1))
            if scaled > MAX_IMAGE_HEIGHT_MM:
                return f"height: {MAX_IMAGE_HEIGHT_MM}mm"
    return "width: 100%"


TYPST_PREAMBLE = """#set document(title: {title})
#set page(paper: "a4", margin: (x: 1.7cm, top: 1.9cm, bottom: 1.8cm), numbering: "1 of 1",
  number-align: right, header: context if counter(page).get().first() > 1 [#align(right, text(8pt, fill: luma(110), {title}))])
#set text(size: 9.6pt, lang: "en")
#set par(justify: false, leading: 0.6em)
#show heading.where(level: 1): set text(size: 19pt)
#show heading.where(level: 2): it => [#v(0.9em) #line(length: 100%, stroke: 0.5pt + luma(200)) #v(0.2em) #text(size: 12.5pt, it)]
#show heading.where(level: 3): set text(size: 10.5pt)
#show raw: set text(size: 8.4pt)
#set table(stroke: (x, y) => (bottom: 0.5pt + luma(210)), inset: (x: 5pt, y: 4pt))
#show table: set text(size: 8.2pt)
#show table.cell.where(y: 0): set text(weight: "bold")
#show link: set text(fill: rgb("#2f7a5b"))
"""


def typst_available() -> bool:
    try:
        import typst  # noqa: F401
    except ImportError:
        return False
    return True


def write_pdf(md: str, title: str, images: dict[str, Path], out: Path) -> None:
    """Typeset spec.md as ``out``. ``images`` maps each image path in the Markdown to its file.
    Raises MissingTool when the ``report`` extra isn't installed."""
    try:
        import typst
    except ImportError as exc:
        raise MissingTool(f"the PDF needs Typst ({INSTALL_HINT})") from exc
    with tempfile.TemporaryDirectory(prefix="gandalf-pdf-") as tmp:
        root = Path(tmp)
        local = {}
        for n, (src, path) in enumerate(sorted(images.items())):
            if path.is_file():
                name = f"img{n}{path.suffix}"
                shutil.copyfile(path, root / name)
                local[src] = ("/" + name, image_size(path))
        logo = ""
        if LOGO.is_file():
            shutil.copyfile(LOGO, root / "logo.png")
            logo = '#place(top + right, dy: -0.4cm, image("/logo.png", width: 1.3cm))\n'
        source = TYPST_PREAMBLE.replace("{title}", _ts(title)) + logo + "\n" + _typ_blocks(blocks(md), local) + "\n"
        (root / "plan.typ").write_text(source)
        typst.compile(str(root / "plan.typ"), output=str(out), root=str(root))

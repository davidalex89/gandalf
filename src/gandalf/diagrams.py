"""The plan's diagrams as SVG, drawn here with the standard library.

Every diagram is self-contained: a white background, explicit colours and a system font, so it
reads the same in a browser, on GitHub, in an editor preview and inside the PDF. All text is
escaped: PR titles, paths and function names come from model output and the target repo.
"""

from __future__ import annotations

from html import escape

FONT = "Helvetica, Arial, sans-serif"
INK, MUTED, RULE, WASH = "#0f172a", "#64748b", "#e2e8f0", "#f8fafc"
INERT, ENABLING, PRECURSOR = "#f1f5f9", "#dcfce7", "#fef3c7"
FLAG = "#dc2626"
CITED = "#4338ca"                 # cited by a review, no pattern category
CHANGED, REACHED = "#1e40af", "#bfdbfe"
CATEGORY_COLOUR = {"guard": "#b45309", "sink": "#b91c1c", "secret": "#7c3aed", "exposure": "#0369a1",
                   "schema": "#047857", "config": "#475569", "admin": "#be185d"}


def _e(text) -> str:
    return escape(str(text), quote=True)


def _clip(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _svg(width: float, height: float, body: list[str], title: str) -> str:
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" '
        f'height="{height:.0f}" font-family="{FONT}" role="img" aria-label="{_e(title)}">',
        f"<title>{_e(title)}</title>",
        f'<rect width="{width:.0f}" height="{height:.0f}" fill="#ffffff"/>',
        '<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
        f'<path d="M0,0 L8,4 L0,8 z" fill="{MUTED}"/></marker></defs>',
        *body, "</svg>", ""])


# --- build order ------------------------------------------------------------------------------

def build_order(order: list[str], prs: dict[str, dict], waves: list[list[str]], lanes: list[list[str]],
                flagged: dict[str, list[str]], unit: str = "PR") -> str:
    """PRs by wave (columns) and lane (rows), direct dependencies only. ``flagged`` maps a PR to
    the security-relevant labels shown on it (fixes, risk tags, sensitive code it reaches)."""
    W, H, GX, GY, PAD = 158, 60, 72, 26, 20
    lane_of = {p: i for i, lane in enumerate(lanes) for p in lane}
    col = {p: i for i, wave in enumerate(waves) for p in wave}
    pos = {p: (PAD + col[p] * (W + GX), PAD + lane_of.get(p, 0) * (H + GY)) for p in order}
    deps = {p: {d for d in prs[p].get("depends_on", []) if d in prs} for p in order}

    def ancestors(p, seen=None):
        seen = set() if seen is None else seen
        for d in deps[p]:
            if d not in seen:
                seen.add(d)
                ancestors(d, seen)
        return seen

    direct = {p: {d for d in deps[p] if not any(d in ancestors(o) for o in deps[p] - {d})} for p in order}
    hidden = sum(len(deps[p]) - len(direct[p]) for p in order)
    width = PAD * 2 + max(len(waves), 1) * (W + GX) - GX
    rows = max(lane_of.values(), default=0) + 1
    height = PAD * 2 + rows * (H + GY) - GY + 34
    body, k = [], 0
    for pid in order:
        x2, y2 = pos[pid]
        for dep in sorted(direct[pid], key=order.index):
            x1, y1 = pos[dep]
            k += 1
            off = (k % 5 - 2) * 3
            sy, ty = y1 + H / 2, y2 + H / 2
            gx1, gx2 = x1 + W + GX / 2 + off, x2 - GX / 2 + off
            if col[pid] - col[dep] <= 1:
                d = f"M{x1 + W},{sy} H{gx1} V{ty} H{x2 - 2}"
            else:                      # run along the gap between rows, never through a box
                lane = max(lane_of.get(dep, 0), lane_of.get(pid, 0))
                cy = PAD + lane * (H + GY) - GY / 2 + off if lane else PAD + H + GY / 2 + off
                d = f"M{x1 + W},{sy} H{gx1} V{cy} H{gx2} V{ty} H{x2 - 2}"
            body.append(f'<path d="{d}" fill="none" stroke="#94a3b8" stroke-width="1.5" stroke-linejoin="round" '
                        'marker-end="url(#arr)"/>')
    for pid in order:
        x, y = pos[pid]
        pr = prs[pid]
        fill = {"enabling": ENABLING, "precursor": PRECURSOR}.get(pr.get("type"), INERT)
        tags = flagged.get(pid, [])
        stroke, sw = (FLAG, 2) if tags else ("#64748b", 1.2)
        body.append(f'<rect x="{x}" y="{y}" width="{W}" height="{H}" rx="7" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
        body.append(f'<text x="{x + 10}" y="{y + 19}" font-size="13" font-weight="bold" fill="{INK}">{_e(pid)}</text>')
        files = len(pr.get("changes", []))
        body.append(f'<text x="{x + 10}" y="{y + 36}" font-size="10.5" fill="#334155">'
                    f'{_e(pr.get("type", ""))} · {files} file{"s" if files != 1 else ""}</text>')
        if tags:
            body.append(f'<text x="{x + 10}" y="{y + 51}" font-size="10" fill="#b91c1c">{_e(_clip(", ".join(tags), 26))}</text>')
    legend_y = height - 22
    body.append(f'<text x="{PAD}" y="{legend_y}" font-size="10" fill="{MUTED}">Columns are waves: {unit}s in one column '
                'can be built at the same time. Rows are lanes.</text>')
    body.append(f'<text x="{PAD}" y="{legend_y + 14}" font-size="10" fill="{MUTED}">Green switches something on. '
                'A red outline marks security-relevant work.'
                + (f" {hidden} implied dependenc{'y' if hidden == 1 else 'ies'} not drawn." if hidden else "") + "</text>")
    return _svg(max(width, 560), height, body, "Build order")


# --- where each PR lands --------------------------------------------------------------------

def landing(order: list[str], report: dict) -> str:
    """Folders by PR: files each PR changes, and with the code map the code that uses or is
    called by what it changes. A red dot marks security-relevant code in that folder."""
    rows: dict[str, dict] = {}
    for pid in order:
        e = report["prs"][pid]
        for a, n in e["areas"].items():
            rows.setdefault(a, {}).setdefault(pid, {})["changed"] = n
        if e.get("reach"):
            for counts in (e["reach"]["used_by_areas"], e["reach"]["calls_areas"]):
                for a, n in counts.items():
                    cell = rows.setdefault(a, {}).setdefault(pid, {})
                    cell["reached"] = cell.get("reached", 0) + n
        hot = set()
        for path, hints in e["hints"].items():
            hot.add(_area_of(path, e))
        for f in (e.get("reach") or {}).get("flows", []):
            hot.add(_area_from_short(f["target"]))
        for a in hot:
            rows.setdefault(a, {}).setdefault(pid, {})["sensitive"] = True
    rows = _fold_rows(rows)
    areas = sorted(rows, key=lambda a: (a == "tests", a.startswith("\u2026"), a))
    CW, RH, TOP = 64, 24, 40
    LW = max(160, min(320, 8 + 6.4 * max((len(a) for a in areas), default=10)))
    width = LW + CW * len(order) + 12
    height = TOP + RH * len(areas) + 44
    body = []
    for ci, pid in enumerate(order):
        body.append(f'<text x="{LW + ci * CW + CW / 2}" y="{TOP - 12}" font-size="11" font-weight="bold" '
                    f'text-anchor="middle" fill="{INK}">{_e(pid)}</text>')
    for ri, a in enumerate(areas):
        y = TOP + ri * RH
        body.append(f'<text x="{LW - 8}" y="{y + 16}" font-size="11" text-anchor="end" fill="{INK}">{_e(_clip(a, 48))}</text>')
        for ci, pid in enumerate(order):
            cell = rows[a].get(pid, {})
            x = LW + ci * CW
            if cell.get("changed"):
                fill, label, colour = CHANGED, str(cell["changed"]), "#ffffff"
            elif cell.get("reached"):
                fill, label, colour = REACHED, f"·{cell['reached']}", "#1e3a8a"
            else:
                fill, label, colour = WASH, "", INK
            body.append(f'<rect x="{x + 3}" y="{y + 2}" width="{CW - 6}" height="{RH - 4}" rx="3" fill="{fill}" stroke="{RULE}"/>')
            if label:
                body.append(f'<text x="{x + CW / 2}" y="{y + 16}" font-size="11" text-anchor="middle" fill="{colour}">{label}</text>')
            if cell.get("sensitive"):
                body.append(f'<circle cx="{x + CW - 10}" cy="{y + 8}" r="4" fill="{FLAG}"/>')
    note = ("Dark: files the PR changes. Light ·n: code within two steps that calls or is called by what it changes."
            if report.get("map") else "Dark: files the PR changes. Install the code map to see the code it reaches.")
    body.append(f'<text x="10" y="{height - 24}" font-size="10" fill="{MUTED}">{_e(note)}</text>')
    body.append(f'<text x="10" y="{height - 10}" font-size="10" fill="{MUTED}">Red dot: security-relevant code in that folder.</text>')
    return _svg(max(width, 520), height, body, "Where each PR lands")


MAX_LANDING_ROWS = 24


def _fold_rows(rows: dict[str, dict]) -> dict[str, dict]:
    """Keep the map to a page: a folder no PR changes and no review or pattern marks is only
    reached, and three or more such siblings fold into one row for their parent; past
    MAX_LANDING_ROWS the least interesting rows fold into a last "and n more" row. A map
    of a few dozen folders, most of them one integration each, would otherwise be clipped."""
    def plain(cells: dict) -> bool:
        return not any(c.get("changed") or c.get("sensitive") for c in cells.values())

    def merge(into: dict, cells: dict) -> None:
        for pid, c in cells.items():
            t = into.setdefault(pid, {})
            t["reached"] = t.get("reached", 0) + c.get("reached", 0)
            t["changed"] = t.get("changed", 0) + c.get("changed", 0)
            t["sensitive"] = t.get("sensitive") or c.get("sensitive", False)

    by_parent: dict[str, list[str]] = {}
    for a, cells in rows.items():
        if plain(cells) and "/" in a.rstrip("/"):
            by_parent.setdefault(a.rstrip("/").rsplit("/", 1)[0] + "/", []).append(a)
    out = dict(rows)
    for parent, kids in by_parent.items():
        if len(kids) >= 3:
            folded: dict = {}
            for k in kids:
                merge(folded, out.pop(k))
            merge(out.setdefault(f"{parent}\u2026 ({len(kids)} folders)", {}), folded)
    if len(out) > MAX_LANDING_ROWS:
        def weight(item):
            a, cells = item
            return (any(c.get("changed") for c in cells.values()), any(c.get("sensitive") for c in cells.values()),
                    sum(c.get("reached", 0) for c in cells.values()))
        keep = sorted(out.items(), key=weight, reverse=True)
        rest = keep[MAX_LANDING_ROWS - 1:]
        out = dict(keep[:MAX_LANDING_ROWS - 1])
        folded = {}
        for _, cells in rest:
            merge(folded, cells)
        out[f"\u2026 and {len(rest)} more folders"] = folded
    for cells in out.values():
        for c in cells.values():
            if not c.get("changed"):
                c.pop("changed", None)
    return out


def _area_of(path: str, entry: dict) -> str:
    for a in entry["areas"]:
        if a == "tests":
            continue
        if path.startswith(a.rstrip("/")):
            return a
    return next(iter(entry["areas"]), "(top level)")


def _area_from_short(target: str) -> str:
    from gandalf.reach import TEST
    path = target.split(":", 1)[0]
    if TEST.search(path):
        return "tests"
    parts = path.split("/")[:-1][:3]
    return "/".join(parts) + "/" if parts else "(top level)"


# --- one PR's flows into sensitive code ------------------------------------------------------

def pr_flow(pid: str, entry: dict, sensitive: dict[str, dict], limit: int = 9) -> str | None:
    """Left to right: the PR, the functions it changes or its new code calls, and the security-relevant
    code they reach. A starting function that is itself security-relevant is outlined in its colour.
    None when the PR meets no security-relevant code."""
    flows = (entry.get("reach") or {}).get("flows", [])
    if not flows:
        return None
    hot_starts = {f["target"]: f for f in flows if f["how"] == "changes"}
    targets, seen = [], set()
    for f in flows:
        if f["how"] != "changes" and f["target"] not in seen:
            seen.add(f["target"])
            targets.append(f)
    shown, more = targets[:limit], max(0, len(targets) - limit)
    starts = []
    for name in list(hot_starts) + [f["path"][0] for f in shown]:
        if name not in starts:
            starts.append(name)

    def colour_of(target: str) -> str:
        cats = sensitive.get(target, {}).get("categories") or []
        return CATEGORY_COLOUR.get(cats[0], CITED) if cats else CITED

    def tag_of(target: str) -> str:
        info = sensitive.get(target, {})
        return ", ".join((info.get("categories") or []) + (info.get("cited") or []))

    BW, BH, GAP, X0, X1, X2, TW = 230, 34, 10, 16, 176, 520, 300
    rows = max(len(starts), len(shown), 1)
    top = 18
    height = top + rows * (BH + GAP) + (18 if more else 0) + 36
    width = X2 + TW + 16
    body = []
    py = top + (rows * (BH + GAP) - GAP) / 2 - 22
    body.append(f'<rect x="{X0}" y="{py}" width="128" height="44" rx="7" fill="{INERT}" stroke="#64748b"/>')
    body.append(f'<text x="{X0 + 12}" y="{py + 27}" font-size="13" font-weight="bold" fill="{INK}">{_e(pid)}</text>')
    spos = {}
    for i, s in enumerate(starts):
        y = top + i * (BH + GAP)
        spos[s] = y + BH / 2
        new = s.startswith("(")
        label = "new code" if new else s.split("/")[-1]
        hot = s in hot_starts
        stroke, sw = (colour_of(s), 1.6) if hot else ("#94a3b8", 1)
        body.append(f'<path d="M{X0 + 128},{py + 22} C{X0 + 148},{py + 22} {X1 - 20},{y + BH / 2} {X1},{y + BH / 2}" '
                    'fill="none" stroke="#cbd5e1" stroke-width="1.2"/>')
        body.append(f'<rect x="{X1}" y="{y}" width="{BW}" height="{BH}" rx="5" fill="{WASH}" stroke="{stroke}" stroke-width="{sw}"/>')
        body.append(f'<text x="{X1 + 8}" y="{y + (14 if hot or new else 21)}" font-size="10.5" fill="{INK}">{_e(_clip(label, 38))}</text>')
        if hot:
            body.append(f'<text x="{X1 + 8}" y="{y + 27}" font-size="9" fill="{colour_of(s)}">changed · {_e(_clip(tag_of(s), 34))}</text>')
        elif new:
            body.append(f'<text x="{X1 + 8}" y="{y + 27}" font-size="9" fill="{MUTED}">{_e(_clip(s.strip("()"), 40))}</text>')
    for i, f in enumerate(shown):
        y = top + i * (BH + GAP)
        colour = colour_of(f["target"])
        sy = spos[f["path"][0]]
        dash = ' stroke-dasharray="4 3"' if f["how"] == "used by" else ""
        body.append(f'<path d="M{X1 + BW},{sy} C{X1 + BW + 50},{sy} {X2 - 50},{y + BH / 2} {X2 - 2},{y + BH / 2}" '
                    f'fill="none" stroke="{colour}" stroke-width="1.4"{dash} marker-end="url(#arr)"/>')
        steps = "" if f["distance"] <= 1 else " · {} steps".format(f["distance"])
        body.append(f'<rect x="{X2}" y="{y}" width="{TW}" height="{BH}" rx="5" fill="#ffffff" stroke="{colour}" stroke-width="1.5"/>')
        body.append(f'<text x="{X2 + 8}" y="{y + 14}" font-size="10.5" fill="{INK}">{_e(_clip(f["target"].split("/")[-1], 44))}</text>')
        body.append(f'<text x="{X2 + 8}" y="{y + 27}" font-size="9" fill="{colour}">'
                    f'{_e(_clip(f["how"] + steps + " · " + tag_of(f["target"]), 54))}</text>')
    if more:
        body.append(f'<text x="{X2}" y="{top + rows * (BH + GAP) + 6}" font-size="10" fill="{MUTED}">and {more} more, '
                    'listed in the table</text>')
    body.append(f'<text x="{X0}" y="{height - 10}" font-size="10" fill="{MUTED}">Solid: what the code calls. '
                'Dashed: what calls it. Guard amber, sink red, secret purple, exposure blue, cited by a review indigo.</text>')
    return _svg(width, height, body, f"{pid}: where it meets security-relevant code")

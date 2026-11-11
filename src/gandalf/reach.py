"""How far each PR reaches into the code, and where it meets security-relevant code.

Three layers, each optional above the first:

1. **Always:** each PR's files by folder, the plan's own security data (fixes it builds in,
   risk tags, the security inventory through its contracts) and pattern hints in the files
   it changes (``PATTERNS``: hints, not verdicts).
2. **With the code map** (``codemap.py``, the ``map`` extra): a function-level graph. Edges
   come from imports, ``module.name`` and ``self.method`` uses and classes to their methods;
   a name from another file counts only when it's imported, because Python and JavaScript
   can't use it otherwise. Each PR starts from the functions its changes name, plus the
   existing functions its new code says it will call ("planned"), and the graph is followed
   both ways: what that code calls, and what calls it.
3. **Sensitive code** is anything the security review or a boundary check cited (by
   ``path:line``) and anything a pattern matches inside a function. ``compute`` also takes
   labels from elsewhere (``labels``), for a later fast-tier labelling step; nothing passes
   them yet. Each label keeps its source.

A cross-check lists sensitive code a PR reaches that no review cited: where a reviewer
should look first. Nothing here reads anything but the repo's files, and nothing changes them.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath

MAX_DEPTH = 3
TEST = re.compile(r"(^|/)(tests?|spec|specs|__tests__|tests_e2e|e2e|fixtures)/|(_test|_spec|\.test|\.spec)\.\w+$"
                  r"|(^|/)test_[^/]+$")
DOCS = {".md", ".rst", ".txt", ".adoc"}
CODE = {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".go", ".rs", ".rb", ".java",
        ".php", ".ex", ".exs", ".html", ".htm", ".jinja", ".j2", ".erb", ".ejs", ".hbs"}
CATEGORIES = ("guard", "sink", "secret", "exposure", "schema", "config", "admin")
CATEGORY_TEXT = {
    "guard": "decides who may do or see something",
    "sink": "passes data to something dangerous (SQL, shell, raw HTML, deserialisation)",
    "secret": "handles passwords, tokens or keys",
    "exposure": "sends stored data outward",
    "schema": "changes stored data's shape",
    "config": "settings and environment",
    "admin": "administration surface",
}

# Pattern hints. Path rules name a file's role; code rules match single lines. A match is a
# hint for a reviewer, never a verdict: the report says which source each label came from.
PATH_RULES = [
    ("schema", re.compile(r"(^|/)migrations?(/|$)|(^|/)schema\.(sql|prisma|rb)$|(^|/)prisma/schema")),
    ("config", re.compile(r"(^|/)settings(/|\.py$)|(^|/)\.env(\.|$)|(^|/)config/[^/]+\.(py|ts|js)$")),
    ("admin", re.compile(r"(^|/)admin(\.py$|/)")),
    ("guard", re.compile(r"(^|/)(auth|authentication|authorization|permissions?|middleware)(/|\.\w+$)", re.I)),
]
CODE_RULES = [
    ("guard", re.compile(r"@login_required|@permission_required|user_passes_test|permission_classes|IsAuthenticated"
                         r"|PermissionDenied|raise Http404|get_object_or_404\([^)]*\b(owner|user)\s*="
                         r"|\.filter\([^)]*\b(owner|user)\s*=|check_password|isAuthenticated\(|requireAuth"
                         r"|getServerSession|passport\.authenticate|\bauthorize\(")),
    # raw SQL only when it looks like SQL: a socket's conn.raw("PING") is not a query
    ("sink", re.compile(r"RawSQL\(|objects\.raw\(|\.raw\(\s*f?[\"'`]\s*(?i:select|insert|update|delete|with)\b"
                        r"|cursor\.execute|\.extra\(\s*(select|where|tables)\s*=|\$queryRawUnsafe|\$executeRawUnsafe|mark_safe\("
                        r"|\|\s*safe\b|innerHTML\s*=|outerHTML\s*=|v-html|dangerouslySetInnerHTML|\{@html"
                        r"|subprocess\.|os\.system\(|child_process|\beval\(|new Function\(|pickle\.loads"
                        r"|yaml\.load\((?![^)]*SafeLoader)|marshal\.loads")),
    ("secret", re.compile(r"SECRET_KEY|\bapi_?key\b|ApiToken|access_token|refresh_token|auth_token|private_key"
                          r"|hmac\.|bcrypt|argon2|set_password\(|\bjwt\.(sign|verify|encode|decode)\b", re.I)),
]


def _cite_re():
    return re.compile(r"([\w./@-]+\.[A-Za-z]{1,6}):(\d+)(?:-(\d+))?")


CITE = _cite_re()


def area(path: str, repo: Path) -> str:
    """The folder a changed path sits in (a folder change is its own area), at most three deep."""
    if TEST.search(path):
        return "tests"
    pp = PurePosixPath(path)
    folder = pp if (repo / path).is_dir() else pp.parent
    parts = folder.parts[:3]
    return "/".join(parts) + "/" if parts else "(top level)"


def _lines(repo: Path, path: str) -> list[str]:
    f = repo / path
    try:
        if f.is_file() and f.stat().st_size < 600_000:
            return f.read_text(errors="replace").splitlines()
    except OSError:
        # unreadable (permissions, a broken link): no lines, so no hints for this file
        pass
    return []


def file_hints(repo: Path, path: str) -> list[dict]:
    """Pattern hints for one file: its role from the path, and the first line matching each code rule."""
    hints = [{"category": cat, "line": None, "text": "path"} for cat, rx in PATH_RULES if rx.search(path)]
    if PurePosixPath(path).suffix.lower() in DOCS or PurePosixPath(path).suffix.lower() not in CODE:
        return hints
    seen = {h["category"] for h in hints}
    for n, line in enumerate(_lines(repo, path), 1):
        for cat, rx in CODE_RULES:
            if cat not in seen and rx.search(line):
                seen.add(cat)
                hints.append({"category": cat, "line": n, "text": line.strip()[:160]})
    return hints


# --- the function-level graph ---------------------------------------------------------------

class Graph:
    """Definitions and the edges between them, from the code map and the repo's import lines."""

    PY_FROM = re.compile(r"^\s*from\s+([.\w]+)\s+import\s+\(?([^#\n]+)")
    PY_IMPORT = re.compile(r"^\s*import\s+([.\w]+)(?:\s+as\s+(\w+))?")
    JS_IMPORT = re.compile(r"""import\s+(?:type\s+)?(?:\{([^}]*)\}|(\w+))(?:\s*,\s*\{([^}]*)\})?\s+from\s+['"]([^'"]+)['"]""")
    GO_IMPORT_BLOCK = re.compile(r"^import\s*\(([^)]*)\)", re.M)
    GO_IMPORT_LINE = re.compile(r'^\s*(?:import\s+)?(\w+\s+|_\s+|\.\s+)?"([^"]+)"\s*$', re.M)
    ATTR = re.compile(r"\b(\w+)\.(\w+)\b")

    def __init__(self, cmap: dict, repo: Path):
        self.repo = repo
        self.files = cmap["files"]
        self.defs: dict[str, dict] = {}
        self.by_file: dict[str, list[str]] = defaultdict(list)
        for path, info in self.files.items():
            classes = [d for d in info["defs"] if d[0] == "class"]
            for kind, name, start, end in info["defs"]:
                owner = [c for c in classes if c[2] < start and end <= c[3] and c[1] != name]
                cls = min(owner, key=lambda c: c[2])[1] if owner else None
                qual = f"{cls}.{name}" if cls else name
                did = f"{path}::{qual}"
                if did not in self.defs:
                    self.defs[did] = {"file": path, "name": name, "qual": qual, "kind": kind,
                                      "start": start, "end": end, "cls": cls}
                    self.by_file[path].append(did)
        self.definers: dict[str, set[str]] = defaultdict(set)     # module-level names only
        self.methods: dict[tuple[str, str], set[str]] = defaultdict(set)
        for did, d in self.defs.items():
            if d["cls"]:
                self.methods[(d["file"], d["cls"])].add(did)
            else:
                self.definers[d["name"]].add(did)
        self._imports()
        self._edges()

    def enclosing(self, path: str, line: int) -> str | None:
        best = None
        for did in self.by_file.get(path, ()):
            d = self.defs[did]
            if d["start"] <= line <= d["end"] and (
                    best is None or d["end"] - d["start"] < self.defs[best]["end"] - self.defs[best]["start"]):
                best = did
        return best

    def _module_files(self, mod: str, here: str) -> set[str]:
        if mod.startswith("."):
            base = PurePosixPath(here).parent
            for _ in range(len(mod) - len(mod.lstrip(".")) - 1):
                base = base.parent
            rest = mod.lstrip(".")
            stem = str(base / rest.replace(".", "/")) if rest else str(base)
        else:
            stem = mod.replace(".", "/")
        return {c for c in (f"{stem}.py", f"{stem}/__init__.py") if c in self.files}

    def _js_files(self, spec: str, here: str) -> set[str]:
        if not spec.startswith("."):
            return set()
        stem = posixpath.normpath(str(PurePosixPath(here).parent / spec))
        cands = [stem] + [f"{stem}{ext}" for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".vue", ".svelte", ".d.ts")]
        cands += [f"{stem}/index{ext}" for ext in (".ts", ".tsx", ".js", ".jsx")]
        return {c for c in cands if c in self.files}

    def _go_module(self) -> str:
        for line in _lines(self.repo, "go.mod"):
            if line.startswith("module "):
                return line.split()[1]
        return ""

    def _imports(self) -> None:
        self.imports: dict[str, dict[str, set[str]]] = defaultdict(dict)
        self.modalias: dict[str, dict[str, set[str]]] = defaultdict(dict)
        go_module = self._go_module()
        self.go_packages: dict[str, set[str]] = defaultdict(set)       # folder -> its .go files
        for path in self.files:
            if path.endswith(".go") and not path.endswith("_test.go"):
                self.go_packages[posixpath.dirname(path)].add(path)
        for path in self.files:
            text = "\n".join(_lines(self.repo, path))
            if path.endswith(".go"):
                # Go imports a package by path; its files sit in one folder
                specs = []
                for block in self.GO_IMPORT_BLOCK.findall(text):
                    specs += self.GO_IMPORT_LINE.findall(block)
                specs += [m for m in self.GO_IMPORT_LINE.findall("\n".join(l for l in text.splitlines()
                                                                         if l.startswith("import \"")
                                                                         or re.match(r"^import \w+ \"", l)))]
                for alias, ipath in specs:
                    folder = ipath[len(go_module) + 1:] if go_module and ipath.startswith(go_module + "/") else None
                    if folder is None or folder not in self.go_packages:
                        continue
                    name = alias.strip() or ipath.rsplit("/", 1)[-1]
                    if name not in ("_", "."):
                        self.modalias[path][name] = self.go_packages[folder]
                continue
            if path.endswith(".py"):
                for line in text.splitlines():
                    m = self.PY_FROM.match(line)
                    if m:
                        mod = m.group(1)
                        src = self._module_files(mod, path)
                        for part in m.group(2).replace(")", "").split(","):
                            bits = part.split(" as ")
                            name, local = bits[0].strip(), bits[-1].strip()
                            if not name:
                                continue
                            sub = self._module_files(mod + name if mod.endswith(".") else f"{mod}.{name}", path)
                            if sub:              # "from notes.views import access" brings a module
                                self.modalias[path][local] = sub
                            elif src:
                                self.imports[path][local] = src
                        continue
                    m = self.PY_IMPORT.match(line)
                    if m:
                        src = self._module_files(m.group(1), path)
                        if src:
                            self.modalias[path][m.group(2) or m.group(1).split(".")[-1]] = src
            else:
                for m in self.JS_IMPORT.finditer(text):
                    src = self._js_files(m.group(4), path)
                    if not src:
                        continue
                    for group in (m.group(1), m.group(3)):
                        for part in (group or "").split(","):
                            local = part.split(" as ")[-1].strip()
                            if local:
                                self.imports[path][local] = src
                    if m.group(2):
                        self.imports[path][m.group(2)] = src

    def _edges(self) -> None:
        self.edges: dict[str, set[str]] = defaultdict(set)
        # Go has no overloading: a method name defined once in the repo is that method, whatever
        # the receiver (h.store.NewEntryQueryBuilder). Other languages don't get this rule.
        go_methods = defaultdict(set)
        for did, d in self.defs.items():
            if d["file"].endswith(".go") and d["kind"] == "method":
                go_methods[d["name"]].add(did)
        for path, info in self.files.items():
            local_names = defaultdict(set)
            # a Go package's names are visible in all its files without an import
            same = self.go_packages.get(posixpath.dirname(path), {path}) if path.endswith(".go") else {path}
            for f in same:
                for did in self.by_file.get(f, ()):
                    if not self.defs[did]["cls"]:
                        local_names[self.defs[did]["name"]].add(did)
            # what the Go packages this file imports define, by name: request.UserID and
            # h.store.NewEntryQueryBuilder both resolve to the imported package's definition
            pkg_defs = defaultdict(set)              # methods only: functions are called pkg.Func
            if path.endswith(".go"):
                for files in self.modalias[path].values():
                    for f in files:
                        for d in self.by_file.get(f, ()):
                            if self.defs[d]["kind"] == "method":
                                pkg_defs[self.defs[d]["name"]].add(d)
            go_lines = _lines(self.repo, path) if path.endswith(".go") else []
            for name, used_at in info["refs"].items():
                if name in self.imports[path]:
                    target = {t for t in self.definers.get(name, ()) if self.defs[t]["file"] in self.imports[path][name]}
                elif name in local_names:
                    target = local_names[name]
                elif path.endswith(".go") and (name in pkg_defs or len(go_methods.get(name, ())) == 1):
                    target = pkg_defs[name] if 0 < len(pkg_defs.get(name, ())) <= 2 else (
                        go_methods[name] if len(go_methods.get(name, ())) == 1 else set())
                    # an occurrence written pkg.Name belongs to the package pass below, not to a method
                    # (a package name starts the chain: h.store.X is a field, store.X the package)
                    qualified = re.compile(rf"(?<![\w.])(\w+)\.{re.escape(name)}\b")
                    used_at = [ln for ln in used_at if ln <= len(go_lines) and not any(
                        q in self.modalias[path] for q in qualified.findall(go_lines[ln - 1]))]
                else:                    # another file's name is only reachable through an import
                    continue
                for ln in used_at:
                    src = self.enclosing(path, ln)
                    if src:
                        self.edges[src] |= target - {src}
            for ln, text in enumerate(_lines(self.repo, path), 1):
                for mod, attr in self.ATTR.findall(text):
                    if mod in self.modalias[path]:
                        tgt = {t for t in self.definers.get(attr, ()) if self.defs[t]["file"] in self.modalias[path][mod]}
                    elif mod in ("self", "this"):
                        src = self.enclosing(path, ln)
                        cls = self.defs[src]["cls"] if src else None
                        tgt = {t for t in self.methods.get((path, cls), ()) if self.defs[t]["name"] == attr} if cls else set()
                    else:
                        continue
                    src = self.enclosing(path, ln)
                    if src and tgt:
                        self.edges[src] |= tgt - {src}
        for (path, cls), ms in self.methods.items():      # reaching a class reaches what its methods do
            cid = f"{path}::{cls}"
            if cid in self.defs:
                self.edges[cid] |= ms
        self.callers: dict[str, set[str]] = defaultdict(set)
        for s, ts in self.edges.items():
            for t in ts:
                self.callers[t].add(s)

    def short(self, did: str) -> str:
        d = self.defs[did]
        return f"{d['file']}:{d['qual']}"

    # --- what a PR touches ------------------------------------------------------------------
    def reused(self, pr: dict) -> set[str]:
        """Existing functions the spec's reuse list says the new code calls rather than changes: a
        citation covering the whole function, or a name in ``what`` that the change text doesn't
        also name. A citation of a few lines inside a function is a pattern to copy, not a call."""
        changed_text = " ".join(c.get("what", "") for c in pr.get("changes", []))
        out = set()
        for item in pr.get("reuse", []):
            cites = CITE.findall(item.get("where", ""))
            for path, a, b in cites:
                first, last = int(a), int(b or a)
                for did in self.by_file.get(path, ()):
                    d = self.defs[did]
                    # the whole function, or its first line: the usual way to cite a function
                    if first == d["start"] or (first <= d["start"] + 1 and last >= d["end"] - 1):
                        out.add(did)
            if cites:
                continue
            path = item.get("where", "").split(":")[0]
            for did in self.by_file.get(path, ()):
                name = self.defs[did]["name"]
                if (re.search(rf"\b{re.escape(name)}\b", item.get("what", ""))
                        and not re.search(rf"\b{re.escape(name)}\b", changed_text)):
                    out.add(did)
        return out

    def seeds(self, pr: dict) -> set[str]:
        """Functions in the PR's changed files that its change text names as code, less the ones its
        reuse list says it only calls. A method counts when its class is named too, or its name is
        defined once in that file. A plain English word never counts: on an app whose views are
        named after everyday words (list, details, export), prose would seed them all."""
        out = set()
        reused = self.reused(pr)
        for ch in pr.get("changes", []):
            text = " ".join([ch.get("what", ""), pr.get("goal", "")])
            ids = self.by_file.get(ch.get("path", ""), [])
            for did in ids:
                d = self.defs[did]
                if not self.names_as_code(text, d["name"]):
                    continue
                same = sum(1 for o in ids if self.defs[o]["name"] == d["name"])
                if d["cls"] and same > 1 and not re.search(rf"\b{re.escape(d['cls'])}\b", text):
                    continue
                if did in reused and self.called(text, d["name"]):
                    continue              # reused whole and named as something the new code calls
                out.add(did)
        return out

    @staticmethod
    def names_as_code(text: str, name: str) -> bool:
        """The text names ``name`` as code rather than as a word: a name shaped like code (snake_case,
        CamelCase) anywhere, or any name in backticks, called (``name(``) or qualified (``Class.name``,
        ``name.attr``)."""
        n = re.escape(name)
        if "_" in name or re.search(r"[a-z][A-Z]", name):
            return bool(re.search(rf"\b{n}\b", text))
        return bool(re.search(rf"`[^`]*\b{n}\b[^`]*`|\b{n}\s*\(|\.{n}\b|\b{n}\.\w", text))

    @staticmethod
    def called(text: str, name: str) -> bool:
        """The text names ``name`` as something called ("with X", "via X", "using X", "calls X")."""
        return bool(re.search(rf"\b(with|via|using|uses?|calls?|calling|through|reus(?:e|es|ing))\s+(the\s+)?`?"
                              rf"(\w+\.)?{re.escape(name)}\b", text))

    QUAL = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b|\b([A-Za-z_]\w{3,})\b")

    def planned(self, pr: dict, seeds: set[str]) -> dict[str, str]:
        """Existing functions the spec says its new code will call: ``module.name`` or
        ``Class.method``, or a bare name that looks like code (snake_case, CamelCase, ``name(``)
        and is defined once in the repo. Plain English words never count."""
        out = {d: "" for d in self.reused(pr) - seeds if not TEST.search(self.defs[d]["file"])}
        for ch in pr.get("changes", []):
            text = ch.get("what", "")
            for m in self.QUAL.finditer(text):
                mod, attr, bare = m.groups()
                if bare:
                    codeish = ("_" in bare.strip("_") or re.search(r"[a-z][A-Z]", bare)
                               or text[m.end():m.end() + 1] == "(")
                    found = self.definers.get(bare, set())
                    cands = set(found) if codeish and len(found) == 1 else set()
                else:
                    cands = {t for t in self.definers.get(attr, ()) if PurePosixPath(self.defs[t]["file"]).stem == mod}
                    cands |= {t for t in self.methods_named(mod, attr)}
                for t in cands - seeds:
                    if not TEST.search(self.defs[t]["file"]):
                        out.setdefault(t, ch.get("path", ""))
        return out

    def methods_named(self, cls: str, name: str) -> set[str]:
        return {t for (path, c), ms in self.methods.items() if c == cls for t in ms if self.defs[t]["name"] == name}

    def walk(self, start: set[str], graph: dict[str, set[str]], depth: int = MAX_DEPTH) -> dict[str, list[str]]:
        seen = {s: [s] for s in start}
        queue = deque((s, 0) for s in start)
        while queue:
            node, k = queue.popleft()
            if k == depth:
                continue
            for nxt in sorted(graph.get(node, ())):
                if nxt not in seen and not TEST.search(self.defs[nxt]["file"]):
                    seen[nxt] = seen[node] + [nxt]
                    queue.append((nxt, k + 1))
        return seen


# --- sensitive code ---------------------------------------------------------------------------

def review_citations(texts: list[tuple[str, object]]) -> list[tuple[str, int, int, str]]:
    """(path, first line, last line, label) for every ``path:line`` cited in the given stage data."""
    out = []
    for label, data in texts:
        for path, a, b in CITE.findall(json.dumps(data, ensure_ascii=False)):
            first, last = int(a), int(b or a)
            if last - first <= 400:
                out.append((path, first, last, label))
    return out


def anchors_for(graph: Graph, citations, labels: dict[str, dict] | None = None) -> dict[str, dict]:
    """Sensitive functions: cited by a review, matched by a pattern, or labelled (fast tier)."""
    found: dict[str, dict] = defaultdict(lambda: {"categories": set(), "sources": set(), "cited": set()})
    for path, first, last, label in citations:
        hit = set()
        for ln in range(first, last + 1):
            d = graph.enclosing(path, ln)
            if d:
                hit.add(d)
        for d in hit:
            found[d]["sources"].add("review")
            found[d]["cited"].add(label)
    for did, d in graph.defs.items():
        if d["kind"] == "class" or TEST.search(d["file"]):
            continue
        body = _lines(graph.repo, d["file"])[d["start"] - 1:d["end"]]
        for cat, rx in CODE_RULES:
            if any(rx.search(l) for l in body):
                found[did]["categories"].add(cat)
                found[did]["sources"].add("pattern")
    for did, lab in (labels or {}).items():
        if did in graph.defs and lab.get("label") in CATEGORIES:
            found[did]["categories"].add(lab["label"])
            found[did]["sources"].add("labelled")
    return {k: v for k, v in found.items() if v["sources"]}


# --- the report ---------------------------------------------------------------------------------

WIDE_USE = 12   # a changed class used in more places than this is reported once and its users aren't followed


def compute(*, prs: list[dict], order: list[str], repo: Path, cmap: dict | None,
            inventory: list[dict], reviews: list[tuple[str, object]], applied: dict[str, list[str]],
            risk: dict[str, list[str]] | None = None, labels: dict[str, dict] | None = None) -> dict:
    """The reach report for a plan. ``reviews`` is [(label, data)]: each security finding, boundary
    gap and inventory item under its own id, and the rest of each review under its name.
    ``applied`` maps each PR to the security fixes it builds in, ``risk`` to its risk tags."""
    by_id = {p["id"]: p for p in prs}
    out = {"map": cmap is not None, "prs": {}, "unreviewed": [], "sensitive": {}}
    graph = Graph(cmap, repo) if cmap else None
    anchors = anchors_for(graph, review_citations(reviews), labels) if graph else {}
    for pid in order:
        pr = by_id[pid]
        files = [c["path"] for c in pr.get("changes", [])]
        inv = [n["id"] for n in inventory if n.get("contract") and n["contract"] in pr.get("contracts_exposed", [])]
        entry = {
            "files": files,
            "areas": {},
            "plan": {"fixes": applied.get(pid, []),
                     "risk": [r for r in (risk or {}).get(pid, pr.get("risk", [])) if r != "enabling"],
                     "inventory": inv},
            "hints": {p: h for p in files if not TEST.search(p) for h in [file_hints(repo, p)] if h},
            "reach": None,
        }
        for p in files:
            a = area(p, repo)
            entry["areas"][a] = entry["areas"].get(a, 0) + 1
        if graph:
            seeds = graph.seeds(pr)
            planned = graph.planned(pr, seeds)
            # a changed class doesn't call its own methods: follow only what changed functions call
            down = graph.walk({d for d in seeds if graph.defs[d]["kind"] != "class"} | set(planned), graph.edges)
            # a model class is used by most of a Django app: following its users reaches everything
            wide = {d for d in seeds if graph.defs[d]["kind"] == "class" and len(graph.callers.get(d, ())) > WIDE_USE}
            up = graph.walk(seeds - wide, graph.callers)
            flows = []
            for did in sorted(seeds):
                if did in anchors:
                    flows.append({"how": "changes", "distance": 0, "path": [did]})
            for did, where in sorted(planned.items()):
                if did in anchors:
                    start = f"(new code in {where})" if where else "(new code, reusing it)"
                    flows.append({"how": "new code calls", "distance": 1, "path": [start, did]})
            for how, reached in (("calls into", down), ("used by", up)):
                for did, path in reached.items():
                    if did in anchors and did not in seeds and did not in planned:
                        flows.append({"how": how, "distance": len(path) - 1, "path": path})
            flows.sort(key=lambda f: (f["distance"], f["how"], f["path"][-1]))
            entry["reach"] = {
                "changes": sorted(graph.short(d) for d in seeds),
                "planned": sorted(graph.short(d) for d in planned),
                "widely_used": {graph.short(d): len(graph.callers[d]) for d in sorted(wide)},
                "used_by_areas": {},
                "calls_areas": {},
                "flows": [{"how": f["how"], "distance": f["distance"],
                           "target": graph.short(f["path"][-1]),
                           "path": [p if p.startswith("(") else graph.short(p) for p in f["path"]]}
                          for f in flows],
            }
            for counts, reached in ((entry["reach"]["used_by_areas"], up), (entry["reach"]["calls_areas"], down)):
                for did, path in reached.items():          # folder counts stop at two steps
                    if did not in seeds and len(path) - 1 <= 2:
                        a = area(graph.defs[did]["file"], repo)
                        counts[a] = counts.get(a, 0) + 1
            for f in flows:
                target = f["path"][-1]
                if "review" not in anchors[target]["sources"] and f["distance"] <= 2:
                    out["unreviewed"].append({"pr": pid, "target": graph.short(target), "how": f["how"],
                                              "distance": f["distance"],
                                              "categories": sorted(anchors[target]["categories"])})
        out["prs"][pid] = entry
    if graph:
        reached = {f["target"] for e in out["prs"].values() for f in (e["reach"] or {}).get("flows", [])}
        for did, a in anchors.items():
            if graph.short(did) in reached:
                out["sensitive"][graph.short(did)] = {"categories": sorted(a["categories"]),
                                                      "sources": sorted(a["sources"]), "cited": sorted(a["cited"])}
    return out

"""The optional code map: every definition in the repo and every place its name is used,
read by tree-sitter with each grammar's own ``tags`` query.

GANDALF needs none of this. With tree-sitter and its language grammars installed (the README lists them)
the render builds the map and the report shows how far each PR reaches into the code: what
its changes call, what calls them, and which of those is security-relevant (``reach.py``).
Without it the report shows each PR's files and the plan's own security data, and says how
to get the rest. No stage prompt uses the map: measured on a real plan it saved nothing
there.

The map knows names, not types: two functions with the same name are not told apart, and
code that reaches things dynamically (string-keyed filters, getattr, reflection, routes built
from strings) never shows up as a use. The report says so.
"""

import importlib
import json
import re
from collections import defaultdict
from pathlib import Path

from gandalf.repo import iter_files

INSTALL_HINT = "pip install tree-sitter and the tree-sitter-<language> grammars the README lists"
MAP_VERSION = 2               # 2: Vue and Svelte script blocks
MAX_FILE_BYTES = 512_000
# generated bundles and fixtures beyond this aren't code to plan around
NON_SOURCE_PARENTS = {"vendor", "node_modules", "third_party", "contrib", "packaging"}

# extension -> (language, grammar module, language function, module whose tags query comes first)
# TypeScript's tags query only adds to JavaScript's, so both run for .ts and .tsx.
# C# is left out: its wheel names a tags query it doesn't ship.
GRAMMARS = {
    ".py": ("python", "tree_sitter_python", "language", None),
    ".js": ("javascript", "tree_sitter_javascript", "language", None),
    ".mjs": ("javascript", "tree_sitter_javascript", "language", None),
    ".cjs": ("javascript", "tree_sitter_javascript", "language", None),
    ".jsx": ("javascript", "tree_sitter_javascript", "language", None),
    ".ts": ("typescript", "tree_sitter_typescript", "language_typescript", "tree_sitter_javascript"),
    ".tsx": ("tsx", "tree_sitter_typescript", "language_tsx", "tree_sitter_javascript"),
    ".go": ("go", "tree_sitter_go", "language", None),
    ".rs": ("rust", "tree_sitter_rust", "language", None),
    ".rb": ("ruby", "tree_sitter_ruby", "language", None),
    ".java": ("java", "tree_sitter_java", "language", None),
    ".php": ("php", "tree_sitter_php", "language_php", None),
    ".vue": ("typescript", "tree_sitter_typescript", "language_typescript", "tree_sitter_javascript"),
    ".svelte": ("typescript", "tree_sitter_typescript", "language_typescript", "tree_sitter_javascript"),
    ".ex": ("elixir", "tree_sitter_elixir", "language", None),
    ".exs": ("elixir", "tree_sitter_elixir", "language", None),
}
# Identifier node types per language. The tags queries record calls only, so `Note.objects.filter()`
# would count as a use of `filter` and never of `Note`; a second pass records every identifier
# whose name the repo defines somewhere.
IDENTIFIERS = {
    "python": ("identifier",),
    "javascript": ("identifier", "property_identifier", "shorthand_property_identifier"),
    "typescript": ("identifier", "property_identifier", "type_identifier", "shorthand_property_identifier"),
    "tsx": ("identifier", "property_identifier", "type_identifier", "shorthand_property_identifier"),
    "go": ("identifier", "field_identifier", "type_identifier"),
    "rust": ("identifier", "field_identifier", "type_identifier"),
    "ruby": ("identifier", "constant"),
    "java": ("identifier", "type_identifier"),
    "php": ("name",),
    "elixir": ("identifier", "alias"),
}
# macro calls a grammar's tags query reports as references, which say nothing about the code
NOISE = {"elixir": {"def", "defp", "defmodule", "defmacro", "defmacrop", "defstruct", "defimpl",
                    "defprotocol", "defdelegate", "defguard", "defexception", "use", "import", "alias",
                    "require"}}



# --- availability ------------------------------------------------------------------------

def _tree_sitter():
    try:
        import tree_sitter
    except ImportError:
        return None
    return tree_sitter


def _tags_query(module) -> str | None:
    try:
        text = getattr(module, "TAGS_QUERY", None)
    except Exception:            # a wheel that names the query lazily and lacks the file
        text = None
    if text:
        return text
    path = Path(module.__file__).parent / "queries" / "tags.scm"
    return path.read_text() if path.is_file() else None


class _Grammars:
    """Loads each grammar once; a grammar that's missing or broken is simply not used."""

    def __init__(self, ts):
        self.ts = ts
        self.loaded: dict[str, tuple | None] = {}
        self.problems: dict[str, str] = {}

    def get(self, ext: str):
        if ext not in GRAMMARS:
            return None
        name, modname, fn, base = GRAMMARS[ext]
        key = f"{modname}.{fn}"
        if key not in self.loaded:
            self.loaded[key] = None
            try:
                # Module names come from the GRAMMARS table above, never from input.
                module = importlib.import_module(modname)  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
                language = self.ts.Language(getattr(module, fn)())
                query = _tags_query(module)
                if base:
                    query = (_tags_query(importlib.import_module(base)) or "") + (query or "")  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
                if not query:
                    raise LookupError("no tags query in the installed grammar")
                kinds = []
                for kind in IDENTIFIERS.get(name, ()):
                    try:
                        self.ts.Query(language, f"({kind}) @id")
                        kinds.append(kind)
                    except Exception:  # noqa: BLE001 - a node type this grammar version lacks
                        pass
                ids = self.ts.Query(language, "[" + " ".join(f"({k})" for k in kinds) + "] @id") if kinds else None
                self.loaded[key] = (name, self.ts.Parser(language), self.ts.Query(language, query), ids)
            except ImportError:
                self.problems[name] = "grammar not installed"
            except Exception as exc:  # noqa: BLE001 - one broken grammar must not stop the map
                self.problems[name] = f"grammar unusable: {exc}"
        return self.loaded[key]


COMPONENTS = {".vue", ".svelte"}
SCRIPT = re.compile(rb"(<script\b[^>]*>)(.*?)(</script\b[^>]*>)", re.S | re.I)


def script_blocks(source: bytes) -> bytes:
    """A Vue or Svelte file with everything outside its <script> blocks blanked, line breaks
    kept, so line numbers still match the file."""
    out, last = [], 0
    for m in SCRIPT.finditer(source):
        out.append(re.sub(rb"[^\n]", b"", source[last:m.start(2)]))
        out.append(m.group(2))
        last = m.end(2)
    out.append(re.sub(rb"[^\n]", b"", source[last:]))
    return b"".join(out)


def available() -> bool:
    """True when tree-sitter and at least one grammar can be loaded."""
    ts = _tree_sitter()
    if ts is None:
        return False
    grammars = _Grammars(ts)
    return any(grammars.get(ext) for ext in (".py", ".js", ".go", ".rs", ".ts"))


# --- building ----------------------------------------------------------------------------

def _source_files(root: Path):
    for rel in iter_files(root):
        if rel.parts and rel.parts[0] in NON_SOURCE_PARENTS:
            continue
        if rel.name.endswith((".min.js", ".bundle.js")):
            continue
        yield rel


def _read(ts, parser, query, ids, source: bytes, lang: str) -> tuple[list, dict, dict]:
    tree = parser.parse(source)
    defs, refs = [], defaultdict(set)
    def_lines = set()
    for _, caps in ts.QueryCursor(query).matches(tree.root_node):
        kind = next((k for k in caps if k.startswith(("definition.", "reference."))), None)
        if not kind or "name" not in caps:
            continue
        name = caps["name"][0].text.decode(errors="replace")
        node = caps[kind][0]
        if kind.startswith("definition."):
            line = node.start_point[0] + 1
            defs.append([kind.split(".", 1)[1], name, line, node.end_point[0] + 1])
            def_lines.add((name, line))
        elif name not in NOISE.get(lang, ()):
            refs[name].add(caps["name"][0].start_point[0] + 1)
    mentions: dict[str, set] = defaultdict(set)
    if ids is not None:
        for node in ts.QueryCursor(ids).captures(tree.root_node).get("id", []):
            name = node.text.decode(errors="replace")
            if len(name) >= 3 and name not in NOISE.get(lang, ()):
                mentions[name].add(node.start_point[0] + 1)
    for name, line in def_lines:                 # a definition's own line isn't a use of it
        refs.get(name, set()).discard(line)
        mentions.get(name, set()).discard(line)
    defs.sort(key=lambda d: (d[2], d[1]))
    return defs, refs, mentions


def build(root: Path) -> dict | None:
    """The map of ``root``, or None when the ``map`` extra isn't installed."""
    ts = _tree_sitter()
    if ts is None:
        return None
    grammars = _Grammars(ts)
    read: dict[str, tuple] = {}
    languages: dict[str, int] = defaultdict(int)
    no_grammar: dict[str, int] = defaultdict(int)
    too_large, errors = [], []
    for rel in _source_files(root):
        ext = rel.suffix.lower()
        loaded = grammars.get(ext)
        if loaded is None:
            if ext in GRAMMARS:
                no_grammar[ext] += 1
            continue
        name, parser, query, ids = loaded
        path = root / rel
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                too_large.append(rel.as_posix())
                continue
            source = path.read_bytes()
            if ext in COMPONENTS:
                source = script_blocks(source)
            read[rel.as_posix()] = (name, *_read(ts, parser, query, ids, source, name))
        except Exception as exc:  # noqa: BLE001 - an unparsable file is noted and skipped
            errors.append({"path": rel.as_posix(), "error": str(exc)[:200]})
    defined = {d[1] for _, defs, _, _ in read.values() for d in defs}
    files: dict[str, dict] = {}
    for rel, (name, defs, refs, mentions) in read.items():
        for ident, lines in mentions.items():
            if ident in defined:                  # a use of something the repo defines
                refs[ident] |= lines
        merged = {n: sorted(lines) for n, lines in sorted(refs.items()) if lines}
        if defs or merged:
            files[rel] = {"lang": name, "defs": defs, "refs": merged}
            languages[name] += 1
    if not files and grammars.problems and not languages:
        return None
    return {
        "version": MAP_VERSION,
        "backend": f"tree-sitter {getattr(ts, '__version__', '') or _version('tree-sitter')}".strip(),
        "files": files,
        "languages": dict(sorted(languages.items())),
        "skipped": {"too_large": too_large, "errors": errors,
                    "grammar_missing": dict(no_grammar), "grammar_problems": grammars.problems},
    }


def _version(dist: str) -> str:
    try:
        from importlib.metadata import version
        return version(dist)
    except Exception:  # noqa: BLE001
        return ""


# --- the plan folder ---------------------------------------------------------------------

def for_plan(plan) -> dict | None:
    """The map of the plan's repo, built at most once per commit (``.state/codemap.json``).
    None when the extra isn't installed or nothing could be parsed."""
    repo = plan.meta.get("repo") or {}
    root = Path(repo.get("path", ""))
    if _tree_sitter() is None or not root.is_dir():
        return None
    key = repo.get("commit") or repo.get("fingerprint")
    path = plan.dir / "codemap.json"
    if path.exists() and key:
        try:
            cached = json.loads(path.read_text())
            if cached.get("key") == key and cached.get("version") == MAP_VERSION:
                return cached
        except ValueError:
            pass
    cmap = build(root)
    if cmap is None:
        return None
    cmap["key"] = key
    path.write_text(json.dumps(cmap))
    return cmap

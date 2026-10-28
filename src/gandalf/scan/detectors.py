"""Deterministic detectors for the repo scan (DESIGN.md §5.1, first pass).

Every detector only reports what it can read from files. Each fact carries the file(s)
it came from as evidence. Nothing here guesses: what can't be read is left for the
scan to record as UNKNOWN.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter, defaultdict
from pathlib import Path


def fact(value: str, *evidence: str, kind: str = "declared") -> dict:
    """kind: 'declared' (stated in a file), 'conventional' (the tool's standard
    invocation, given a config file that declares the tool) or 'observed' (counted
    from the files present)."""
    return {"value": value, "evidence": sorted(set(evidence)), "kind": kind}


def _read_text(path: Path, limit: int = 2_000_000) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text(errors="replace")
    except OSError:
        return None


# --- dependency name -> what it tells us -------------------------------------------

FRAMEWORKS = {
    # python
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "starlette": "Starlette",
    "djangorestframework": "Django REST Framework",
    "sanic": "Sanic", "aiohttp": "aiohttp", "tornado": "Tornado", "litestar": "Litestar",
    # js/ts
    "express": "Express", "fastify": "Fastify", "koa": "Koa", "@nestjs/core": "NestJS",
    "hono": "Hono", "next": "Next.js", "react": "React", "vue": "Vue", "svelte": "Svelte",
    "@sveltejs/kit": "SvelteKit", "@angular/core": "Angular", "nuxt": "Nuxt",
    "@remix-run/react": "Remix", "solid-js": "Solid",
    # ruby
    "rails": "Ruby on Rails", "sinatra": "Sinatra",
    # go
    "github.com/gin-gonic/gin": "Gin", "github.com/labstack/echo/v4": "Echo",
    "github.com/gofiber/fiber/v2": "Fiber", "github.com/go-chi/chi/v5": "chi",
    "connectrpc.com/connect": "Connect RPC", "github.com/grpc-ecosystem/grpc-gateway/v2": "gRPC-Gateway",
    "google.golang.org/grpc": "gRPC", "github.com/gorilla/mux": "gorilla/mux",
    "github.com/labstack/echo": "Echo", "github.com/gin-gonic/gin/v2": "Gin",
    # rust
    "actix-web": "Actix Web", "axum": "Axum", "rocket": "Rocket",
    # php / jvm
    "laravel/framework": "Laravel", "symfony/framework-bundle": "Symfony",
    "spring-boot-starter-web": "Spring Boot",
}

ORMS = {
    "sqlalchemy": "SQLAlchemy", "sqlmodel": "SQLModel", "alembic": "Alembic (migrations)",
    "django": "Django ORM",
    "peewee": "peewee", "tortoise-orm": "Tortoise ORM", "pony": "Pony ORM",
    "prisma": "Prisma", "@prisma/client": "Prisma", "typeorm": "TypeORM",
    "sequelize": "Sequelize", "mongoose": "Mongoose", "drizzle-orm": "Drizzle",
    "knex": "Knex", "@mikro-orm/core": "MikroORM", "objection": "Objection.js",
    "activerecord": "Active Record", "gorm.io/gorm": "GORM", "diesel": "Diesel",
    # Go database/sql drivers: no ORM, hand-written SQL per driver
    "github.com/go-sql-driver/mysql": "database/sql: MySQL driver",
    "github.com/lib/pq": "database/sql: Postgres driver (lib/pq)",
    "github.com/jackc/pgx/v5": "Postgres driver (pgx)",
    "modernc.org/sqlite": "database/sql: SQLite driver (modernc)",
    "github.com/mattn/go-sqlite3": "database/sql: SQLite driver (go-sqlite3)",
    "github.com/jmoiron/sqlx": "sqlx", "entgo.io/ent": "ent", "github.com/uptrace/bun": "bun",
    "sqlx": "SQLx", "sea-orm": "SeaORM", "doctrine/orm": "Doctrine",
    "spring-boot-starter-data-jpa": "Spring Data JPA",
}

TEST_RUNNERS = {
    "pytest": "pytest", "jest": "Jest", "vitest": "Vitest", "mocha": "Mocha",
    "@playwright/test": "Playwright", "cypress": "Cypress", "ava": "AVA",
    "rspec": "RSpec", "rspec-rails": "RSpec", "minitest": "Minitest",
    "phpunit/phpunit": "PHPUnit",
}

LANG_BY_EXT = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".rb": "Ruby",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".php": "PHP",
    ".cs": "C#", ".swift": "Swift", ".scala": "Scala", ".ex": "Elixir", ".exs": "Elixir",
    ".vue": "Vue", ".svelte": "Svelte", ".dart": "Dart", ".sql": "SQL", ".sh": "Shell",
}


def _dep_name(spec: str) -> str:
    """'fastapi[all]>=0.110' -> 'fastapi'"""
    return re.split(r"[\s\[<>=!~;@]", spec.strip(), maxsplit=1)[0].lower()


# --- manifests -----------------------------------------------------------------------

MAX_MANIFEST_PARTS = 4   # e.g. apps/api/pyproject.toml, services/x/y/package.json


def is_requirements(rel: Path) -> bool:
    return bool(re.fullmatch(r"requirements[\w.-]*\.txt", rel.name)) or (
        rel.parent.name == "requirements" and rel.suffix == ".txt")


def python_dependencies(root: Path, files: list[Path]) -> dict[str, list[str]]:
    """Map dependency name -> evidence files, from pyproject and requirements files
    at the root and in workspace packages."""
    deps: dict[str, list[str]] = defaultdict(list)
    for rel in files:
        if len(rel.parts) > MAX_MANIFEST_PARTS + 1:
            continue
        if rel.name == "pyproject.toml" and len(rel.parts) <= MAX_MANIFEST_PARTS:
            text = _read_text(root / rel)
            if text is None:
                continue
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            project = data.get("project", {})
            specs = list(project.get("dependencies", []))
            for group in project.get("optional-dependencies", {}).values():
                specs.extend(group)
            for group in data.get("dependency-groups", {}).values():
                specs.extend(s for s in group if isinstance(s, str))
            poetry = data.get("tool", {}).get("poetry", {})
            specs.extend(poetry.get("dependencies", {}).keys())
            for group in poetry.get("group", {}).values():
                specs.extend(group.get("dependencies", {}).keys())
            specs.extend(poetry.get("dev-dependencies", {}).keys())
            for spec in specs:
                deps[_dep_name(spec)].append(rel.as_posix())
        elif is_requirements(rel):
            text = _read_text(root / rel) or ""
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line and not line.startswith("-"):
                    deps[_dep_name(line)].append(rel.as_posix())
    return deps


def package_jsons(root: Path, files: list[Path]) -> list[tuple[Path, dict]]:
    out = []
    for rel in files:
        if rel.name == "package.json" and len(rel.parts) <= MAX_MANIFEST_PARTS:
            text = _read_text(root / rel)
            try:
                out.append((rel, json.loads(text or "")))
            except json.JSONDecodeError:
                continue
    return out


def js_package_manager(root: Path, pkg_dir: Path) -> tuple[str, str | None]:
    for lock, manager in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
                          ("bun.lockb", "bun"), ("bun.lock", "bun"),
                          ("package-lock.json", "npm")):
        for base in (pkg_dir, Path(".")):
            if (root / base / lock).exists():
                return manager, (base / lock).as_posix()
    return "npm", None


def other_manifest_deps(root: Path, files: list[Path]) -> dict[str, list[str]]:
    deps: dict[str, list[str]] = defaultdict(list)
    for rel in files:
        if len(rel.parts) > MAX_MANIFEST_PARTS:
            continue
        text = None
        if rel.name == "Gemfile":
            text = _read_text(root / rel) or ""
            for m in re.finditer(r"""^\s*gem\s+['"]([\w-]+)['"]""", text, re.M):
                deps[m.group(1).lower()].append(rel.as_posix())
        elif rel.name == "go.mod":
            text = _read_text(root / rel) or ""
            for m in re.finditer(r"^\s*(?:require\s+)?([\w./-]+\.[\w./-]+)\s+v", text, re.M):
                deps[m.group(1).lower()].append(rel.as_posix())
        elif rel.name == "Cargo.toml":
            try:
                data = tomllib.loads(_read_text(root / rel) or "")
            except tomllib.TOMLDecodeError:
                continue
            for table in ("dependencies", "dev-dependencies"):
                for name in data.get(table, {}):
                    deps[name.lower()].append(rel.as_posix())
        elif rel.name == "composer.json":
            try:
                data = json.loads(_read_text(root / rel) or "")
            except json.JSONDecodeError:
                continue
            for table in ("require", "require-dev"):
                for name in data.get(table, {}):
                    deps[name.lower()].append(rel.as_posix())
        elif rel.name in ("pom.xml", "build.gradle", "build.gradle.kts"):
            text = _read_text(root / rel) or ""
            for m in re.finditer(r"(spring-boot-starter[\w-]*)", text):
                deps[m.group(1).lower()].append(rel.as_posix())
    return deps


GO_NET_HTTP = re.compile(r'^\s*(?:\w+\s+)?"net/http"\s*$', re.M)
GO_SERVES = re.compile(r"\bhttp\.(ListenAndServe(TLS)?|Serve(TLS)?|NewServeMux|Handle(Func)?)\(")


def go_stdlib_http(root: Path, files: list[Path], here: str, limit: int = 1000) -> str | None:
    """The Go file under ``here`` that serves HTTP on the standard library (or, failing that,
    imports net/http), nearest the root: a Go service with no framework in go.mod is a fact
    with evidence, not an unknown (fourth eval run, E-14). Tests and files past ``limit``
    are not read."""
    best: tuple | None = None
    seen = 0
    for rel in files:
        posix = rel.as_posix()
        if rel.suffix != ".go" or rel.name.endswith("_test.go") or not posix.startswith(here):
            continue
        seen += 1
        if seen > limit:
            break
        text = _read_text(root / rel, limit=200_000) or ""
        if GO_NET_HTTP.search(text):
            rank = (0 if GO_SERVES.search(text) else 1, len(rel.parts), posix)
            if best is None or rank < best:
                best = rank
    return best[2] if best else None


# --- run and test commands -------------------------------------------------------------

MAKE_RUN_TARGETS = ("run", "dev", "start", "serve", "server", "up")
MAKE_TEST_TARGETS = ("test", "tests", "check")


def in_dir(directory: str) -> str:
    """Command prefix for running something inside a workspace package."""
    return "" if directory in ("", ".") else f"cd {directory} && "


def makefile_targets(root: Path, files: list[Path], dirs: set[str]) -> tuple[list[dict], list[dict]]:
    runs, tests = [], []
    for rel in files:
        parent = rel.parent.as_posix()
        if rel.name in ("Makefile", "makefile", "GNUmakefile") and parent in dirs:
            text = _read_text(root / rel) or ""
            targets = re.findall(r"^([A-Za-z][\w.-]*)\s*:(?!=)", text, re.M)
            for t in targets:
                if t in MAKE_RUN_TARGETS:
                    runs.append(fact(f"{in_dir(parent)}make {t}", rel.as_posix()))
                if t in MAKE_TEST_TARGETS:
                    tests.append(fact(f"{in_dir(parent)}make {t}", rel.as_posix()))
    return runs, tests


def makefile_all_targets(root: Path, files: list[Path], dirs: set[str]) -> list[dict]:
    out = []
    for rel in files:
        parent = rel.parent.as_posix()
        if rel.name in ("Makefile", "makefile", "GNUmakefile") and parent in dirs:
            text = _read_text(root / rel) or ""
            for t in re.findall(r"^([A-Za-z][\w.-]*)\s*:(?!=)", text, re.M):
                out.append(fact(f"{in_dir(parent)}make {t}", rel.as_posix()))
    return out


def procfile_and_compose(root: Path, files: list[Path]) -> list[dict]:
    runs = []
    for rel in files:
        if len(rel.parts) != 1:
            continue
        if rel.name == "Procfile":
            for line in (_read_text(root / rel) or "").splitlines():
                if ":" in line and not line.lstrip().startswith("#"):
                    proc, cmd = line.split(":", 1)
                    runs.append(fact(f"{cmd.strip()}  (Procfile: {proc.strip()})", rel.as_posix()))
        if rel.name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            runs.append(fact("docker compose up", rel.as_posix()))
    return runs


# --- layers ---------------------------------------------------------------------------

LAYER_DIRS = {
    "database": {"migrations", "migration", "alembic", "migrate", "prisma", "db", "database",
                 "sql", "schema", "seeds", "seeders"},
    "models": {"models", "model", "entities", "entity", "domain", "services", "service",
               "repositories", "repository", "crud", "dal", "store", "stores"},
    "api": {"routes", "routers", "router", "api", "controllers", "controller", "handlers",
            "endpoints", "resolvers", "graphql", "views"},
    "frontend": {"components", "pages", "frontend", "web", "client", "ui", "public",
                 "static", "templates", "layouts", "hooks", "screens", "assets"},
    "tests": {"tests", "test", "__tests__", "spec", "specs", "e2e", "cypress", "integration"},
}
FRONTEND_EXTS = {".tsx", ".jsx", ".vue", ".svelte"}
# what a directory must hold to count as a frontend: components, templates, styles or scripts
FRONTEND_FILE_EXTS = FRONTEND_EXTS | {".html", ".htm", ".tmpl", ".gohtml", ".hbs", ".ejs", ".erb", ".twig",
                                      ".njk", ".jinja", ".jinja2", ".css", ".scss", ".less", ".js", ".ts", ".mjs"}
# top-level folders that hold other people's or packaging code, never the product's layers
NON_SOURCE_PARENTS = {"vendor", "node_modules", "third_party", "contrib", "packaging"}


def infrastructure_roots(files: list[Path]) -> list[str]:
    """Folders config management makes infrastructure (D-128), as path prefixes."""
    roots = set()
    for rel in files:
        managed = config_management(rel.as_posix())
        if managed and managed[1]:
            roots.add(managed[1])
    return sorted(roots)


def detect_layers(files: list[Path]) -> dict[str, list[dict]]:
    """Directories named like a layer that hold that layer's kind of file: code for the
    api, models, database and tests layers, components, templates, styles or scripts for the
    frontend. Deployment code (an Ansible role, D-128), vendored and contributed folders never
    count: a role's handlers/ is not an API and its templates/ are not a frontend, and a Go
    package named client/ is a library, not a user interface (fourth eval run, E-14)."""
    skip = infrastructure_roots(files)
    found: dict[str, dict[str, int]] = {layer: Counter() for layer in LAYER_DIRS}
    for rel in files:
        parts = rel.parts[:-1]
        posix = rel.as_posix()
        if (parts and parts[0] in NON_SOURCE_PARENTS) or any(posix.startswith(r) for r in skip):
            continue
        code, front = rel.suffix in LANG_BY_EXT, rel.suffix in FRONTEND_FILE_EXTS
        for depth, part in enumerate(parts[:5]):
            for layer, names in LAYER_DIRS.items():
                if part.lower() in names and (front if layer == "frontend" else code):
                    found[layer]["/".join(parts[: depth + 1])] += 1
        if rel.suffix in FRONTEND_EXTS and parts:
            found["frontend"]["/".join(parts[:2])] += 1
        if rel.name == "schema.prisma":
            found["database"][rel.as_posix()] += 1
    layers = {}
    for layer, counter in found.items():
        # keep the outermost matching directories only
        dirs = sorted(counter)
        outer = [d for d in dirs if not any(d != o and d.startswith(o + "/") for o in dirs)]
        layers[layer] = [fact(d, d, kind="observed") for d in outer[:12]]
    return layers


# --- rule files -------------------------------------------------------------------------

RULE_PATTERNS = [
    (r"(.*/)?CLAUDE\.md", "agent instructions"),
    (r"(.*/)?AGENTS\.md", "agent instructions"),
    (r"\.cursorrules|\.cursor/rules/.*", "agent instructions"),
    (r"\.github/copilot-instructions\.md", "agent instructions"),
    (r"CONTRIBUTING(\.md|\.rst|\.txt)?|docs/CONTRIBUTING\.md|\.github/CONTRIBUTING\.md", "contribution guide"),
    (r"(\.github/)?(PULL_REQUEST_TEMPLATE|pull_request_template)\.md|\.github/PULL_REQUEST_TEMPLATE/.*\.md", "PR template"),
    (r"CODEOWNERS|\.github/CODEOWNERS|docs/CODEOWNERS", "code owners"),
    (r"commitlint\.config\.\w+|\.commitlintrc(\.\w+)?|\.czrc|\.cz\.toml", "commit conventions"),
    (r"\.pre-commit-config\.yaml", "pre-commit hooks"),
    (r"\.editorconfig", "editor config"),
    (r"\.eslintrc(\.\w+)?|eslint\.config\.\w+", "linter"),
    (r"\.prettierrc(\.\w+)?|prettier\.config\.\w+", "formatter"),
    (r"biome\.jsonc?", "linter/formatter"),
    (r"ruff\.toml|\.ruff\.toml|\.flake8|\.pylintrc|mypy\.ini|\.mypy\.ini", "linter/type checker"),
    (r"\.golangci\.ya?ml|rustfmt\.toml|\.rustfmt\.toml|clippy\.toml|\.rubocop\.yml|phpcs\.xml(\.dist)?", "linter/formatter"),
    (r"tsconfig\.json", "TypeScript config"),
    (r"(.*/)?buf(\.gen)?\.ya?ml", "API contracts: protobuf (buf); generated code comes from these"),
    (r"(.*/)?openapi\.(ya?ml|json)", "API contracts: OpenAPI"),
    (r"\.tool-versions|\.mise\.toml|\.nvmrc|\.python-version|\.ruby-version", "tool versions"),
    (r"\.?gitleaks\.toml|\.secrets\.baseline|\.trufflehog.*|\.gitguardian\.ya?ml", "secret scanner"),
]


# A package that depends on one of these builds a documentation site: not an app, so its
# lint and language config are not repo rules and its dev script is not how the app runs (D-176).
SITE_GENERATORS = ("astro", "@astrojs/starlight", "vitepress", "vuepress", "@docusaurus/core",
                   "nextra", "gatsby", "@11ty/eleventy", "eleventy", "hexo", "mkdocs")


def is_docs_site(pkg: dict) -> bool:
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    return any(name in deps for name in SITE_GENERATORS)


LICENCE_NAME = re.compile(r"(LICEN[CS]E|COPYING|COPYRIGHT)(\.(md|txt|rst))?", re.I)   # not license.ex


def detect_rules(root: Path, files: list[Path], app_dirs: set[str] = frozenset()) -> list[dict]:
    """Rule files at the root, plus lint/format config inside app-like workspace
    packages (library packages are skipped to keep the list readable), plus licence files
    that cover part of the repo, such as an enterprise folder (D-114)."""
    rules = []
    for rel in files:
        if 1 < len(rel.parts) <= 3 and LICENCE_NAME.fullmatch(rel.name):
            folder = rel.parent.as_posix() + "/"
            rules.append({"path": rel.as_posix(), "kind": f"separate licence: {folder} is licensed apart from the "
                                                          "rest; a feature must say which part it belongs to"})
    for rel in files:
        posix = rel.as_posix()
        if len(rel.parts) > 4:
            continue
        parent = rel.parent.as_posix()
        candidates = [posix]
        if parent in app_dirs and parent != ".":
            candidates.append(posix[len(parent) + 1:])     # match as if at the root
        for pattern, kind in RULE_PATTERNS:
            if any(re.fullmatch(pattern, c) for c in candidates):
                rules.append({"path": posix, "kind": kind})
                break
    for directory in sorted({".", *app_dirs}):
        rel = "pyproject.toml" if directory == "." else f"{directory}/pyproject.toml"
        pyproject = root / rel
        if not pyproject.exists():
            continue
        try:
            tool = tomllib.loads(_read_text(pyproject) or "").get("tool", {})
        except tomllib.TOMLDecodeError:
            tool = {}
        for name, kind in (("ruff", "linter"), ("black", "formatter"), ("isort", "import order"),
                           ("mypy", "type checker"), ("pylint", "linter")):
            if name in tool:
                rules.append({"path": f"{rel} [tool.{name}]", "kind": kind})
    return rules


# --- naming conventions -------------------------------------------------------------------

def _case_of(stem: str) -> str | None:
    if re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)+", stem):
        return "snake_case"
    if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", stem):
        return "kebab-case"
    if re.fullmatch(r"[a-z]+[a-z0-9]*([A-Z][a-z0-9]*)+", stem):
        return "camelCase"
    if re.fullmatch(r"([A-Z][a-z0-9]+){2,}", stem):
        return "PascalCase"
    return None


TEST_FILE_PATTERNS = [
    (r"test_.+\.py", "test_*.py"), (r".+_test\.py", "*_test.py"),
    (r".+\.test\.[jt]sx?", "*.test.{js,ts}"), (r".+\.spec\.[jt]sx?", "*.spec.{js,ts}"),
    (r".+_test\.go", "*_test.go"), (r".+_spec\.rb", "*_spec.rb"), (r".+Test\.(java|kt|php)", "*Test.*"),
]


def detect_naming(files: list[Path]) -> dict:
    by_lang: dict[str, Counter] = defaultdict(Counter)
    examples: dict[tuple[str, str], str] = {}
    tests = Counter()
    test_examples: dict[str, str] = {}
    for rel in files:
        lang = LANG_BY_EXT.get(rel.suffix)
        if lang:
            stem = rel.stem.split(".")[0]
            case = _case_of(stem)
            if case:
                by_lang[lang][case] += 1
                examples.setdefault((lang, case), rel.as_posix())
        for pattern, label in TEST_FILE_PATTERNS:
            if re.fullmatch(pattern, rel.name):
                tests[label] += 1
                test_examples.setdefault(label, rel.as_posix())
    naming = []
    for lang, counter in sorted(by_lang.items()):
        case, count = counter.most_common(1)[0]
        total = sum(counter.values())
        naming.append({
            "language": lang, "file_case": case, "share": f"{count}/{total}",
            "example": examples[(lang, case)],
        })
    test_files = [{"pattern": label, "count": n, "example": test_examples[label]}
                  for label, n in tests.most_common()]
    return {"files": naming, "test_files": test_files}


# --- languages ----------------------------------------------------------------------------

def detect_languages(files: list[Path]) -> list[dict]:
    counter: Counter = Counter()
    examples: dict[str, str] = {}
    for rel in files:
        lang = LANG_BY_EXT.get(rel.suffix)
        if lang:
            counter[lang] += 1
            examples.setdefault(lang, rel.as_posix())
    return [fact(f"{lang} ({n} files)", examples[lang], kind="observed")
            for lang, n in counter.most_common(6)]


# Languages whose package files this scan reads. A repo mostly in any other language is
# still planned: the context stage establishes its stack by reading the code (D-112).
LANGUAGE_MANIFESTS = {
    "Python": ("pyproject.toml", "setup.py", "manage.py", "requirements.txt"),
    "TypeScript": ("package.json",), "JavaScript": ("package.json",), "Vue": ("package.json",),
    "Svelte": ("package.json",), "Go": ("go.mod",), "Rust": ("Cargo.toml",), "Ruby": ("Gemfile",),
    "PHP": ("composer.json",), "Java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    "Kotlin": ("pom.xml", "build.gradle", "build.gradle.kts"),
}
SUPPORTING_LANGUAGES = {"SQL", "Shell"}      # never the language an app is written in


def unrecognised_languages(files: list[Path]) -> list[tuple[str, int, int, str]]:
    """Main languages whose package files the scan can't read: (language, files, code
    files in total, an example file). 'Main' means at least a fifth of the code."""
    counter: Counter = Counter()
    examples: dict[str, str] = {}
    present = {rel.name for rel in files}
    for rel in files:
        lang = LANG_BY_EXT.get(rel.suffix)
        if lang and lang not in SUPPORTING_LANGUAGES:
            counter[lang] += 1
            if not rel.name.startswith("."):
                examples.setdefault(lang, rel.as_posix())
    total = sum(counter.values())
    return [(lang, n, total, examples.get(lang, "")) for lang, n in counter.most_common()
            if n >= max(5, total // 5) and not present & set(LANGUAGE_MANIFESTS.get(lang, ()))]


# --- commands from CI (any language) -------------------------------------------------------

CI_FILES = re.compile(r"\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.yml|\.circleci/config\.yml")
# "test", "tests", or a test script named like tools/test-backend or test_api (D-121)
# shell syntax lines inside a multi-line run block, not commands (case … in, esac, fi, x) …)
SHELL_STRUCTURE = re.compile(r"^(case|esac|if|then|else|elif|fi|for|while|do|done)\b|;;$|^[\w*|-]+\)$|\bin$")
TEST_WORDS = re.compile(r"(^|[\s/:.])(tests?([-_][\w-]+)?|pytest|rspec|phpunit|jest|vitest|playwright)(\s|$|:)")


def ci_commands(root: Path, files: list[Path]) -> list[tuple[str, str]]:
    """Commands the repo's CI runs, as (command, file): language-neutral evidence of how it
    tests, lints and builds itself (D-113)."""
    out = []
    for rel in files:
        if not CI_FILES.fullmatch(rel.as_posix()):
            continue
        lines = (_read_text(root / rel, limit=500_000) or "").splitlines()
        # GitHub's `script:` is JavaScript for actions/github-script, not shell;
        # GitLab's `script:` is a list of shell commands
        keys = "run" if rel.as_posix().startswith(".github/") else "run|script"
        i = 0
        while i < len(lines):
            m = re.match(rf"^(\s*)(?:-\s*)?(?:{keys}):\s*(.*)$", lines[i])
            i += 1
            if not m:
                continue
            indent, value = len(m.group(1)), m.group(2).strip()
            block = []
            if value in ("|", ">", "|-", ">-") or not value:
                while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
                    block.append(lines[i].strip())
                    i += 1
            else:
                quoted = len(value) > 1 and value[0] == value[-1] and value[0] in "'\""
                block = [value[1:-1] if quoted else value]
            if not value:             # a list of commands (GitLab `script:`), not a mapping
                block = [line[2:].strip() for line in block if line.startswith("- ")]
            joined, current = [], ""
            for line in block:
                if not line or line.startswith("#"):
                    continue
                current = f"{current} {line}".strip()
                if current.endswith("\\"):
                    current = current[:-1].strip()
                    continue
                joined.append(current)
                current = ""
            out += [(cmd, rel.as_posix()) for cmd in joined
                    if cmd and not cmd.startswith("echo ") and not SHELL_STRUCTURE.search(cmd)]
    return out


# --- feature tracing candidates -------------------------------------------------------------

def _feature_stem(rel: Path) -> str | None:
    stem = rel.name.split(".")[0].lower()
    stem = re.sub(r"^(test_|tests_)|(_test|_spec|_tests)$", "", stem)
    stem = re.sub(r"(controller|service|router|routes|model|repository|handler|schema)s?$", "", stem)
    stem = stem.strip("_-")
    if len(stem) < 3 or stem in {"index", "init", "__init__", "main", "app", "base", "utils",
                                 "util", "common", "config", "settings", "conftest", "types",
                                 "helpers", "constants", "setup", "env", "readme"}:
        return None
    return stem[:-1] if stem.endswith("s") and len(stem) > 3 else stem


def feature_candidates(files: list[Path], layers: dict[str, list[dict]], limit: int = 5) -> list[dict]:
    """Names that appear in files across two or more layers: good candidates for the
    agent to trace an existing feature end to end."""
    layer_roots = {layer: [f["value"] for f in facts] for layer, facts in layers.items()}
    seen: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for rel in files:
        if rel.suffix not in LANG_BY_EXT:
            continue
        posix = rel.as_posix()
        stem = _feature_stem(rel)
        if not stem:
            continue
        for layer, roots in layer_roots.items():
            if any(posix.startswith(r + "/") or posix == r for r in roots):
                seen[stem][layer].append(posix)
    ranked = sorted(seen.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    out = []
    for stem, by_layer in ranked:
        if len(by_layer) < 2:
            break
        out.append({"name": stem, "files": {k: sorted(v)[:4] for k, v in sorted(by_layer.items())}})
        if len(out) >= limit:
            break
    return out


# --- dependencies and migrations, shared by the plan checks and PR monitoring ------------------
DEPENDENCY_MANIFESTS = {"package.json", "pyproject.toml", "setup.py", "setup.cfg", "go.mod", "Cargo.toml", "Gemfile",
                        "composer.json", "mix.exs", "pom.xml", "build.gradle", "build.gradle.kts"}
LOCKFILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "poetry.lock", "uv.lock",
             "Pipfile.lock", "go.sum", "Cargo.lock", "Gemfile.lock", "composer.lock", "mix.lock"}
REQUIREMENTS_FILE = re.compile(r"(^|/)requirements[\w.-]*\.(txt|in)$")
MIGRATION_DIR = re.compile(r"(^|/)(migrations?|alembic/versions|db/migrate)/", re.I)


# --- infrastructure as code (§8.6, D-90) ------------------------------------------------
# Cloud and deployment resources, and configuration management, are another team's work: GANDALF never changes these,
# even inside the target repo. Paths that mark a whole folder as infrastructure.
IAC_BY_NAME = [
    (r"(.*/)?[^/]+\.tf(\.json)?|(.*/)?[^/]+\.tfvars|(.*/)?\.terraform\.lock\.hcl|(.*/)?terragrunt\.hcl", "Terraform"),
    (r"(.*/)?Pulumi(\.[\w-]+)?\.ya?ml", "Pulumi"),
    (r"(.*/)?cdk\.json", "AWS CDK"),
    (r"(.*/)?[^/]+\.bicep(param)?", "Bicep"),
    (r"(.*/)?Chart\.ya?ml", "Helm"),
    (r"(.*/)?kustomization\.ya?ml", "Kustomize"),
    (r"(.*/)?serverless\.ya?ml", "Serverless Framework"),
    (r"(.*/)?(samconfig\.toml|template\.ya?ml)", "AWS SAM"),
]
FOLDER_KINDS = {"Pulumi", "AWS CDK", "Helm", "Kustomize"}     # the whole folder is the project
# Configuration management is infrastructure too, even when the product ships it (D-128).
# `root` is the folder the match makes infrastructure: a Puppet module, an Ansible role,
# a Chef cookbook, a Salt state tree. Empty means the repo root: only the file is marked.
CONFIG_MANAGEMENT = [
    (r"(?P<root>(.*/)?)manifests/.+\.pp", "Puppet"),
    (r"(?P<root>(.*/)?)(Puppetfile|hiera\.yaml)", "Puppet"),
    (r"(?P<root>(.*/)?roles/[^/]+/)(tasks|handlers|defaults|vars|meta)/main\.ya?ml", "Ansible"),
    (r"(?P<root>(.*/)?)ansible\.cfg", "Ansible"),
    (r"(?P<root>(.*/)?cookbooks/[^/]+/).+", "Chef"),
    (r"(?P<root>(.*/)?)(Berksfile|Policyfile\.rb)", "Chef"),
    (r"(?P<root>(.*/)?)([^/]+\.sls|Saltfile)", "Salt"),
    # GitHub and CI configuration counts too (D-135): repo settings, workflows, code owners
    (r"(?P<root>\.github/).+", "GitHub configuration"),
    (r"(?P<root>)(docs/)?CODEOWNERS", "GitHub configuration"),
    (r"(?P<root>\.(circleci|buildkite|gitlab)/).+", "CI pipeline"),
    (r"(?P<root>)(\.gitlab-ci\.yml|Jenkinsfile|azure-pipelines\.yml|bitbucket-pipelines\.yml|\.travis\.yml)", "CI pipeline"),
]
IAC_MARKERS = [                         # found inside YAML/JSON files
    (re.compile(r"AWSTemplateFormatVersion|Transform:\s*AWS::Serverless"), "CloudFormation"),
    (re.compile(r"schema\.management\.azure\.com/schemas/[^\"']*deploymentTemplate"), "Azure ARM"),
    (re.compile(r"^apiVersion:\s*\S+\s*$(?s:.*)^kind:\s*(Deployment|StatefulSet|DaemonSet|Service|Ingress|"
                r"CronJob|Job|ConfigMap|Secret|Namespace)\s*$", re.M), "Kubernetes"),
]


def config_management(path: str) -> tuple[str, str] | None:
    """(kind, the folder it makes infrastructure, "" for the repo root) for a config-management path."""
    for pattern, kind in CONFIG_MANAGEMENT:
        m = re.fullmatch(pattern, path)
        if m:
            return kind, m.group("root")
    return None


def iac_kind_by_name(path: str) -> str | None:
    managed = config_management(path)
    if managed:
        return managed[0]
    for pattern, kind in IAC_BY_NAME:
        if re.fullmatch(pattern, path):
            if kind == "AWS SAM" and not path.endswith("samconfig.toml"):
                return None                 # template.yaml is only SAM if its contents say so
            return kind
    return None


def detect_iac(root: Path, files: list[Path]) -> list[dict]:
    """Infrastructure-as-code files and folders: {"path", "kind"}; a folder ends in "/"."""
    found: dict[str, str] = {}
    for rel in files:
        posix = rel.as_posix()
        kind = iac_kind_by_name(posix)
        if kind is None and rel.suffix in (".yaml", ".yml", ".json", ".template"):
            text = _read_text(root / rel, limit=256_000) or ""
            kind = next((k for marker, k in IAC_MARKERS if marker.search(text)), None)
        if kind is None:
            continue
        folder = rel.parent.as_posix()
        managed = config_management(posix)
        if managed and managed[1]:
            found[managed[1]] = kind
        elif kind in FOLDER_KINDS and folder != ".":
            found[folder + "/"] = kind
        else:
            found.setdefault(posix, kind)
    folders = [f for f in found if f.endswith("/")]
    return [{"path": p, "kind": k} for p, k in sorted(found.items())
            if not any(p != f and p.startswith(f) for f in folders)]

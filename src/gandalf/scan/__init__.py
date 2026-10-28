"""Deterministic repo scan: the first of the two scan passes (DESIGN.md §5.1, D-48).

``scan_repo`` reads manifests, config and directory structure and returns facts, each
with file evidence. Whatever it cannot determine is listed under ``unknowns`` so it can
become a clarifying question. It never calls a model and never writes to the repo.

Monorepos: every directory holding a manifest (up to ``MAX_MANIFEST_PARTS`` deep) is a
workspace package and is scanned in its own right. Commands are prefixed with
``cd <package> && `` where they must run inside it. For app-like packages, a missing
framework or test command is reported as UNKNOWN rather than silently absent.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from gandalf.repo import RepoInfo, iter_files
from gandalf.scan import detectors as d

MAX_FILES = 50_000
# files that mark where a package or app starts (not the same as its dependency files)
PACKAGE_MARKERS = {"package.json", "pyproject.toml", "setup.py", "manage.py", "go.mod", "Cargo.toml",
             "Gemfile", "composer.json", "pom.xml", "build.gradle", "build.gradle.kts"}
# Top-level folders whose packages are applications rather than shared libraries.
APP_PARENTS = {"apps", "services", "app", "backend", "frontend", "web", "server", "api", "cmd"}


def _dedupe(facts: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for f in facts:
        if f["value"] in merged:
            merged[f["value"]]["evidence"] = sorted(set(merged[f["value"]]["evidence"]) | set(f["evidence"]))
        else:
            merged[f["value"]] = dict(f)
    return list(merged.values())


def _from_deps(deps: dict[str, list[str]], table: dict[str, str]) -> list[dict]:
    return [d.fact(label, *deps[name]) for name, label in table.items() if name in deps]


def package_dirs(files: list[Path]) -> list[str]:
    """Directories (as posix, '.' for the root) that hold a manifest."""
    dirs = set()
    for rel in files:
        if len(rel.parts) <= d.MAX_MANIFEST_PARTS and (rel.name in PACKAGE_MARKERS or d.is_requirements(rel)):
            parent = rel.parent
            if parent.name == "requirements":
                parent = parent.parent
            dirs.add(parent.as_posix())
    return sorted(dirs, key=lambda p: (p != ".", p.count("/"), p))


def is_app(directory: str) -> bool:
    if directory == ".":
        return True
    parts = directory.split("/")
    return parts[0] in APP_PARENTS or len(parts) == 1


def _manifest_dir(evidence: str) -> str:
    """'apps/api/requirements/base.txt' -> 'apps/api'; 'pyproject.toml' -> '.'"""
    parent = PurePosixPath(evidence).parent
    if parent.name == "requirements":
        parent = parent.parent
    return parent.as_posix()


def _deps_in(deps: dict[str, list[str]], directory: str) -> dict[str, list[str]]:
    """The dependencies declared by the manifests of one package."""
    out = {}
    for name, evidence in deps.items():
        here = [e for e in evidence if _manifest_dir(e) == directory]
        if here:
            out[name] = here
    return out


def scan_repo(info: RepoInfo) -> dict:
    root = info.path
    files = []
    for rel in iter_files(root):
        files.append(rel)
        if len(files) >= MAX_FILES:
            break
    names = {rel.as_posix() for rel in files}
    dirs = package_dirs(files)
    docs_dirs = {rel.parent.as_posix() for rel, pkg in d.package_jsons(root, files) if d.is_docs_site(pkg)}
    app_dirs = {p for p in dirs if is_app(p)} - docs_dirs

    py_deps = d.python_dependencies(root, files)
    other_deps = d.other_manifest_deps(root, files)
    frameworks = _from_deps(py_deps, d.FRAMEWORKS) + _from_deps(other_deps, d.FRAMEWORKS)
    orms = _from_deps(py_deps, d.ORMS) + _from_deps(other_deps, d.ORMS)
    runners = _from_deps(py_deps, d.TEST_RUNNERS) + _from_deps(other_deps, d.TEST_RUNNERS)
    run_cmds: list[dict] = []
    test_cmds: list[dict] = []
    declared: list[dict] = []
    managers: list[dict] = []
    workspaces: dict[str, dict] = {p: {"path": p, "manifests": [], "frameworks": [], "test_commands": [],
                                       "kind": "docs" if p in docs_dirs else "app" if p in app_dirs else "library"}
                  for p in dirs}
    for rel in files:
        parent = rel.parent.parent if rel.parent.name == "requirements" else rel.parent
        key = parent.as_posix()
        if key in workspaces and (rel.name in PACKAGE_MARKERS or d.is_requirements(rel)):
            workspaces[key]["manifests"].append(rel.as_posix())

    # JavaScript / TypeScript
    for rel, pkg in d.package_jsons(root, files):
        directory = rel.parent.as_posix()
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        lowered = {k.lower(): [rel.as_posix()] for k in deps}
        found = _from_deps(lowered, d.FRAMEWORKS)
        frameworks += found
        orms += _from_deps(lowered, d.ORMS)
        runners += _from_deps(lowered, d.TEST_RUNNERS)
        pm, lock = d.js_package_manager(root, rel.parent)
        managers.append(d.fact(pm, *(e for e in (lock, rel.as_posix()) if e)))
        prefix = d.in_dir(directory)
        scripts = pkg.get("scripts", {}) or {}
        for name in scripts:   # every declared script, for grounding static checks
            forms = [f"{pm} run {name}"] + ([f"{pm} {name}"] if pm != "npm" or name in ("test", "start") else [])
            declared += [d.fact(prefix + form, rel.as_posix()) for form in forms]
        if "test" in scripts:
            test = "bun run test" if pm == "bun" else f"{pm} test"
            test_cmds.append(d.fact(prefix + test, rel.as_posix()))
            workspaces[directory]["test_commands"].append(prefix + test)
        if directory in app_dirs:          # libraries' dev scripts are usually watch builds
            for name in ("dev", "start", "serve"):
                if name in scripts:
                    cmd = f"{pm} start" if (name == "start" and pm != "bun") else f"{pm} run {name}"
                    run_cmds.append(d.fact(prefix + cmd, rel.as_posix()))
        workspaces[directory]["frameworks"] += [f["value"] for f in found]

    # Python, per package
    for directory in dirs:
        here = "" if directory == "." else directory + "/"
        local_deps = _deps_in(py_deps, directory)
        workspaces[directory]["frameworks"] += [f["value"] for f in _from_deps(local_deps, d.FRAMEWORKS)]
        cfg = [here + p for p in ("pytest.ini", "conftest.py", "tests/conftest.py", "tox.ini", "setup.cfg")
               if here + p in names]
        pyproject = root / here / "pyproject.toml"
        if pyproject.exists() and "[tool.pytest" in (d._read_text(pyproject) or ""):
            cfg.append(here + "pyproject.toml")
        if cfg or "pytest" in local_deps:
            evidence = cfg + local_deps.get("pytest", [])
            runners.append(d.fact("pytest", *evidence))
            cmd = d.in_dir(directory) + "pytest"
            test_cmds.append(d.fact(cmd, *evidence, kind="conventional"))
            workspaces[directory]["test_commands"].append(cmd)
        if here + "manage.py" in names:
            cmd = d.in_dir(directory) + "python manage.py test"
            test_cmds.append(d.fact(cmd, here + "manage.py", kind="conventional"))
            workspaces[directory]["test_commands"].append(cmd)
            run_cmds.append(d.fact(d.in_dir(directory) + "python manage.py runserver", here + "manage.py",
                                   kind="conventional"))
        for lock, manager in (("uv.lock", "uv"), ("poetry.lock", "poetry"), ("Pipfile.lock", "pipenv"),
                              ("pdm.lock", "pdm")):
            if here + lock in names:
                managers.append(d.fact(manager, here + lock))

        # Other ecosystems: the toolchain's standard test command, given its manifest
        local_other = _deps_in(other_deps, directory)
        workspaces[directory]["frameworks"] += [f["value"] for f in _from_deps(local_other, d.FRAMEWORKS)]
        conventional = []
        if here + "go.mod" in names:
            conventional.append(("go test ./...", "go.mod"))
            runners.append(d.fact("go test", here + "go.mod", kind="conventional"))   # the toolchain's own
            if not workspaces[directory]["frameworks"]:
                served = d.go_stdlib_http(root, files, here)
                if served:   # no framework in go.mod, but the code serves HTTP on the standard library
                    frameworks.append(d.fact("Go standard library (net/http)", served, kind="observed"))
                    workspaces[directory]["frameworks"].append("Go standard library (net/http)")
            # a Go command's entry point: cmd/<name>/main.go -> go run ./cmd/<name>
            for rel in files:
                parts = rel.parts
                if (rel.name == "main.go" and len(parts) >= 3 and parts[-3] == "cmd"
                        and rel.as_posix().startswith(here)):
                    entry = "/".join(parts[:-1])[len(here):]
                    run_cmds.append(d.fact(d.in_dir(directory) + f"go run ./{entry}", rel.as_posix(),
                                           kind="conventional"))
        if here + "Cargo.toml" in names:
            conventional.append(("cargo test", "Cargo.toml"))
            runners.append(d.fact("cargo test", here + "Cargo.toml", kind="conventional"))   # the toolchain's own
            if here + "src/main.rs" in names:
                run_cmds.append(d.fact(d.in_dir(directory) + "cargo run", here + "src/main.rs", kind="conventional"))
            if not workspaces[directory]["frameworks"] and "hyper" in local_other:
                # no framework in Cargo.toml, but the crate serves HTTP on hyper directly (seventh eval run, E-27)
                frameworks.append(d.fact("hyper (Rust HTTP library, no framework)", *local_other["hyper"], kind="observed"))
                workspaces[directory]["frameworks"].append("hyper (Rust HTTP library, no framework)")
        if here + "Gemfile" in names and ("rspec" in local_other or "rspec-rails" in local_other):
            conventional.append(("bundle exec rspec", "Gemfile"))
        if here + "composer.json" in names and "phpunit/phpunit" in local_other:
            conventional.append(("vendor/bin/phpunit", "composer.json"))
        if here + "pom.xml" in names:
            conventional.append(("mvn test", "pom.xml"))
        for gradle in ("build.gradle", "build.gradle.kts"):
            if here + gradle in names:
                conventional.append(("./gradlew test" if here + "gradlew" in names else "gradle test", gradle))
        for cmd, manifest in conventional:
            full = d.in_dir(directory) + cmd
            test_cmds.append(d.fact(full, here + manifest, kind="conventional"))
            workspaces[directory]["test_commands"].append(full)

    for cmd, evidence in d.ci_commands(root, files):     # language-neutral (D-113)
        declared.append(d.fact(cmd, evidence, kind="ci"))
        if d.TEST_WORDS.search(cmd) and not re.search(r"\b(install|ci|setup|merge-reports)\b", cmd):
            test_cmds.append(d.fact(cmd, evidence, kind="ci"))
    make_runs, make_tests = d.makefile_targets(root, files, set(dirs) | {"."})
    declared += d.makefile_all_targets(root, files, set(dirs) | {"."})
    run_cmds += make_runs + d.procfile_and_compose(root, files)
    test_cmds = make_tests + test_cmds  # explicit targets first

    layers = d.detect_layers(files)
    stack = {
        "languages": d.detect_languages(files),
        "frameworks": _dedupe(frameworks),
        "orm": _dedupe(orms),
        "test_runner": _dedupe(runners),
        "package_manager": _dedupe(managers),
        "run_commands": _dedupe(run_cmds),
        "test_commands": _dedupe(test_cmds),
    }

    unknowns = []
    labels = {
        "languages": "the programming language", "frameworks": "the application framework",
        "orm": "the ORM / database access layer", "test_runner": "the test runner",
        "run_commands": "how the app is started locally", "test_commands": "how tests are run",
    }
    for key, label in labels.items():
        if not stack[key]:
            unknowns.append({"area": f"stack.{key}", "what": f"Could not determine {label} from manifests or config."})
    for lang, n, total, example in d.unrecognised_languages(files):
        unknowns.insert(0, {"area": "stack.unrecognised_language", "what": (
            f"Most of this repo is {lang} ({n} of {total} code files, e.g. {example}), which GANDALF's scan "
            "doesn't recognise: its framework, database layer, tests and commands must be established by "
            "reading the code. The facts above may describe only the smaller parts of the repo.")})
    for layer, facts in layers.items():
        if not facts:
            unknowns.append({"area": f"layers.{layer}", "what": f"No directory recognisable as the {layer} layer."})
    multi = len(dirs) > 1
    # The root counts as an app package when it holds a language manifest of its own
    # (e.g. go.mod), not just a JS workspace's package.json: a backend at the root
    # must not hide behind a frontend's framework.
    root_manifests = {m for m in workspaces.get(".", {}).get("manifests", [])}
    root_is_app = bool(root_manifests - {"package.json"})
    if multi:
        for directory in sorted((app_dirs - {"."}) | ({"."} if root_is_app else set())):
            ws = workspaces[directory]
            label = "root" if directory == "." else directory
            if not ws["frameworks"]:
                unknowns.append({"area": f"workspaces.{label}.frameworks",
                                 "what": f"Could not determine the framework for the {label} package."})
            if not ws["test_commands"]:
                unknowns.append({"area": f"workspaces.{label}.test_commands",
                                 "what": f"Could not determine how tests are run for the {label} package."})
    for ws in workspaces.values():
        ws["frameworks"] = sorted(set(ws["frameworks"]))

    return {
        "repo": info.to_dict(),
        "file_count": len(files),
        "truncated": len(files) >= MAX_FILES,
        "top_level": sorted({rel.parts[0] + ("/" if len(rel.parts) > 1 else "") for rel in files}),
        "workspaces": [workspaces[p] for p in dirs] if multi else [],
        "declared_commands": _dedupe(declared),
        "stack": stack,
        "layers": layers,
        "rules": d.detect_rules(root, files, app_dirs),
        "infrastructure_as_code": d.detect_iac(root, files),
        "naming": d.detect_naming(files),
        "feature_candidates": d.feature_candidates(files, layers),
        "unknowns": unknowns,
    }

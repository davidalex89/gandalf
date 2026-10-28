"""Target-repo identity, plan-folder location and the write guard (DESIGN.md §8.5, §10.1)."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Folders skipped when walking a repo or fingerprinting it: build output, dependencies,
# caches and version-control metadata.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", "target",
    ".next", ".nuxt", ".turbo", ".cache", "coverage", ".gradle", ".idea", ".vscode",
    "vendor", ".terraform", ".eggs",
}


class GuardError(Exception):
    """Raised when GANDALF is asked to write inside the target repo."""


@dataclass
class RepoInfo:
    path: Path          # the boundary GANDALF must never write inside
    name: str
    vcs: str            # "git" or "none"
    commit: str | None
    dirty: bool | None
    fingerprint: str | None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "vcs": self.vcs,
            "commit": self.commit,
            "dirty": self.dirty,
            "fingerprint": self.fingerprint,
        }


def _git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def git_toplevel(path: Path) -> Path | None:
    top = _git(path, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else None


def iter_files(root: Path):
    """Yield every file under root as a path relative to root, skipping SKIP_DIRS."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            yield full.relative_to(root)


def fingerprint(root: Path) -> str:
    """Hash every file's relative path and contents (reduced mode, §8.5)."""
    digest = hashlib.sha256()
    for rel in iter_files(root):
        digest.update(rel.as_posix().encode())
        digest.update(b"\0")
        try:
            digest.update(hashlib.sha256((root / rel).read_bytes()).digest())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def identify(repo_path: str | Path) -> RepoInfo:
    path = Path(repo_path).expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"target repo not found: {path}")
    top = git_toplevel(path)
    if top is not None:
        commit = _git(top, "rev-parse", "HEAD")
        status = _git(top, "status", "--porcelain")
        return RepoInfo(top, top.name, "git", commit, bool(status), None)
    return RepoInfo(path, path.name, "none", None, None, fingerprint(path))


def slugify(text: str, max_len: int = 48) -> str:
    """'Let users' favourites sync' -> 'let-users-favourites-sync', cut between words."""
    slug = re.sub(r"[^a-z0-9]+", "-", re.sub(r"['’]", "", text.lower())).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len + 1]
        slug = cut[:cut.rfind("-")] if "-" in cut else slug[:max_len]
    return slug.strip("-") or "feature"


def plans_home() -> Path:
    env = os.environ.get("GANDALF_HOME")
    return Path(env).expanduser() if env else Path.home() / "gandalf-plans"


def default_plan_dir(repo: RepoInfo, feature_name: str) -> Path:
    return plans_home() / slugify(repo.name) / slugify(feature_name)


def is_inside(child: Path, parent: Path) -> bool:
    child, parent = child.resolve(), parent.resolve()
    return child == parent or parent in child.parents


def guard_plan_dir(plan_dir: Path, repo: RepoInfo) -> Path:
    """Refuse any plan folder inside the target repo, whatever --out says (D-44)."""
    resolved = plan_dir.expanduser().resolve()
    if is_inside(resolved, repo.path):
        raise GuardError(
            f"refusing to write the plan inside the target repo ({repo.path}). "
            "Plans must live outside the codebase so they can never be committed. "
            "Choose an --out path outside the repo, or omit --out to use "
            f"{plans_home()}."
        )
    return resolved

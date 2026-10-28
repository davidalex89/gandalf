"""Run the repo's own test command in an isolated git worktree (DESIGN.md §8.4, §8.5).

The worktree is created outside the target repo and removed afterwards, so nothing
from verification can reach a commit. Only a test command the repo itself defines (as
found by the scan or confirmed by the context stage) is ever run; the model only says
*why* it wants verification.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from gandalf.repo import is_inside
from gandalf.state import PlanState

TIMEOUT_SECONDS = 600


def _defined_in(root: Path, command: str, evidence: list[str]) -> bool:
    """Whether the command's text appears verbatim in one of the files it cites."""
    for cite in evidence:
        path = root / cite.split(":", 1)[0]
        try:
            if path.is_file() and command in path.read_text(errors="replace"):
                return True
        except OSError:
            continue
    return False


def repo_test_command(plan: PlanState) -> str | None:
    """The repo's own test command, never a model-written one (§8.4, D-168): a scan fact, or a
    context-stage command whose text appears verbatim in the file it cites as evidence."""
    scan = plan.scan
    known = [c["value"] for c in scan["stack"]["test_commands"]]
    known += [c["value"] for c in scan.get("declared_commands", [])]
    root = Path(plan.meta["repo"]["path"])
    context = plan.stage("context")
    for c in (context or {}).get("test_commands") or []:
        if c["command"] in known or _defined_in(root, c["command"], c.get("evidence") or []):
            return c["command"]
    return known[0] if known else None


def run_in_worktree(repo: Path, command: str, timeout: int = TIMEOUT_SECONDS) -> tuple[int, str]:
    tmp = Path(tempfile.mkdtemp(prefix="gandalf-verify-"))
    if is_inside(tmp, repo):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("the temporary worktree would be inside the target repo")
    worktree = tmp / "worktree"
    try:
        add = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), "HEAD"],
                             capture_output=True, text=True, timeout=120)
        if add.returncode != 0:
            return add.returncode, f"could not create a worktree: {add.stderr.strip()}"
        # The command runs in its own process group, so a timeout kills the whole tree: killing
        # only the shell leaves a test runner holding the pipes and the call never returns (D-178).
        proc = subprocess.Popen(command, shell=True, cwd=worktree, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
            return proc.returncode, out
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, _ = proc.communicate()
            return 124, (out or "") + f"\n[timed out after {timeout}s]"
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True)
        shutil.rmtree(tmp, ignore_errors=True)


def run_verification(plan: PlanState, stage: str) -> list[dict]:
    pending = plan.meta.get("verify", {}).get(stage, {})
    reasons = [r["reason"] for r in pending.get("requests", [])] or ["verification requested"]
    repo = plan.meta["repo"]
    command = repo_test_command(plan)
    results = []
    if repo["vcs"] != "git":
        for reason in reasons:
            results.append({"reason": reason, "command": "(none)", "exit_code": -1,
                            "output": "Skipped: reduced mode (not a git repository). Mark findings "
                                      "that depended on this as not verified by running."})
    elif command is None:
        for reason in reasons:
            results.append({"reason": reason, "command": "(none)", "exit_code": -1,
                            "output": "Skipped: the repo defines no test command. Ask the designer how tests are run."})
    else:
        code, output = run_in_worktree(Path(repo["path"]), command)
        for reason in reasons:
            results.append({"reason": reason, "command": command, "exit_code": code, "output": output})
    plan.record_verification(stage, results)
    return results

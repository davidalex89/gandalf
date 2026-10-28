"""Claude Code PostToolUse hook: validate stage output the moment it's written (R-15).

When Claude writes ``<plan folder>/.state/inbox/<stage>.json``, this runs the same
validation as ``gandalf submit``. Accepted output is stored and Claude is told the next
step; rejected output is returned to Claude with the errors so it can fix them. Any
other file write is ignored.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

INBOX = re.compile(r"(?P<plan>.+)/\.state/inbox/(?P<stage>[a-z]+)\.json$")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    path = (event.get("tool_input") or {}).get("file_path") or ""
    m = INBOX.match(path.replace("\\", "/"))
    if not m:
        return 0

    from gandalf.adapters import extract_json
    from gandalf.cli import describe_next, submit
    from gandalf.state import OldPlanError, PlanState

    stage, plan_dir = m.group("stage"), Path(m.group("plan"))
    try:
        plan = PlanState(plan_dir)
    except (FileNotFoundError, OldPlanError) as exc:
        print(json.dumps({"decision": "block", "reason": f"GANDALF: {exc}"}))
        return 0
    text = ""
    try:
        text = Path(path).read_text()
        data = extract_json(text)
    except (OSError, ValueError) as exc:
        errors = [f"could not read a JSON object from {path}: {exc}"]
        plan.record_rejection(stage, "hook", errors, text)
    else:
        try:
            errors = submit(plan, stage, data, via="hook")
        except Exception as exc:  # a GANDALF bug: log it, and tell Claude it isn't its fault
            plan.record_internal_error(f"hook: submit {stage}", exc)
            print(json.dumps({"decision": "block", "reason": (
                f"GANDALF hit an internal error validating {stage} ({type(exc).__name__}: {exc}). "
                f"It is logged in {plan.dir / 'log.jsonl'}. Tell the designer; don't keep retrying.")}))
            return 0
    if errors:
        reason = (f"GANDALF rejected {stage} ({len(errors)} problem(s)). Fix them and write the file again:\n"
                  + "\n".join(f"- {e}" for e in errors))
        print(json.dumps({"decision": "block", "reason": reason}))
        return 0
    plan.archive_inbox_file(Path(path), stage)
    duplicate = (" Warning: this output is identical to one accepted earlier (a stale file?)."
                 if plan.log_entries()[-1].get("duplicate") else "")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": f"GANDALF accepted {stage}.{duplicate} {describe_next(plan)}",
    }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

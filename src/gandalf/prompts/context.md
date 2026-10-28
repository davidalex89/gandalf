# Stage: repo context (second scan pass)

The deterministic scanner has already gathered facts from manifests, config and
folders (below). Your job is to **confirm and deepen** them by reading the code. This
stage records **only what the code shows**: how the repo works, not what the feature
should do.

- Confirm the scanner facts that matter for planning, each with cited evidence.
- **If the scanner reports an unrecognised language** (`stack.unrecognised_language`),
  its facts may describe only the smaller parts of the repo. Establish the main code's
  framework, database layer, test runner and how it's run by reading its own files
  (its package or build file, config, CI workflows), and record each under `added`
  with evidence. GANDALF knows only some ecosystems; every language is plannable.
- Add what the scanner couldn't see (`added`): how auth works, where config lives, how
  errors are returned, and so on. Every claim cites a file.
- Record any **contradiction** between a scanner fact and what the code actually does.
  Never silently resolve one either way.
- **Trace one existing feature through every layer** (`feature_trace`): the one closest
  to the feature being planned (its title is below), since its code is what new work will
  extend or copy. Use the scanner's feature candidates only if nothing is closer. Note
  scope observations about the code that feature touches. The title is only a pointer:
  don't design the feature, guess what it should do, or ask about it.
- List the **conventions** new code must follow, each with an example file, including
  what the repo's own rules files (e.g. `AGENTS.md`, `CONTRIBUTING.md`) require.
- Confirm how the full test suite is run and how the app is started (`test_commands`,
  `run_commands`), and which lint, format and type checks the repo defines
  (`check_commands`), each with evidence. Only list commands the repo itself defines;
  commands its CI runs count (cite the workflow file).
- **Scope observations go in `scope_notes`, not questions.** If the code reveals
  something the feature will have to decide (a component shared by several screens, a
  special case excluded elsewhere), note it with evidence. The design stage raises
  them with the designer.
- **Questions here are only about how the repo works** — for example, a scanner
  `UNKNOWN` you can't resolve from the code. Never cite a question or an answer as
  evidence.

## Output format

```json
{
  "confirmed": [{"fact": "FastAPI routers are registered in app/main.py", "evidence": ["app/main.py:5-7"]}],
  "added": [{"area": "auth", "claim": "Routes check the caller with require_user", "evidence": ["app/deps.py:10"]}],
  "contradictions": [{"scanner_fact": "test command: pytest", "observed": "CI runs make test, which sets env vars first", "evidence": ["Makefile:4"]}],
  "feature_trace": {"feature": "project", "files": [
    {"layer": "models", "path": "app/models/project.py", "role": "SQLAlchemy model"},
    {"layer": "api", "path": "app/api/routes/projects.py", "role": "router"}
  ]},
  "conventions": [{"rule": "One router module per resource", "example": "app/api/routes/projects.py"}],
  "test_commands": [{"command": "make test", "evidence": ["Makefile"]}],
  "run_commands": [{"command": "make run", "evidence": ["Makefile"]}],
  "check_commands": [{"command": "make lint", "evidence": ["Makefile"]}],
  "scope_notes": [{"note": "Private projects are hidden from non-owners by can_view; the feature must decide how they behave", "evidence": ["app/services/project_service.py:8"]}],
  "questions": [{"question": "How is the app started locally?", "context": "No Makefile, scripts or Procfile define it.", "basis": []}]
}
```

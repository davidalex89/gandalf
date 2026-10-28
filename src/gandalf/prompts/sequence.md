# Stage: slice and sequence

Group every leaf into PRs (each leaf in exactly one PR) and give each PR its spec.
Each PR is built by a **fresh agent session** that has the repo with earlier PRs merged
but no memory of them, so each spec must carry everything it needs.

**Right-sized PRs: logically whole beats small.** Estimate each PR's size in changed
production lines (excluding tests, docs, migrations, generated code, lock files,
fixtures and config); the estimate is shown to the reader as a band and never checked.
Aim for PRs that each do one coherent thing, up to ~350 lines. A PR that changes more
than 10 files says why it's kept whole (`size_reason`); over ~500 lines, split unless
keeping it whole is justified, and say so there too.
**Prefer a slightly larger PR to splitting something that belongs together.** In
particular:
- Scaffolding with no behaviour of its own (types, strings, constants): if one PR uses
  it, put it in that PR. If several independent PRs use it, land it before all of them
  as its own small PR, so they aren't chained through one another. Keep it separate too
  if its consumer is already large, or if it carries its own risk (e.g. a shared type
  change that can break type checks elsewhere in a monorepo). Say which in
  `kept_separate_because`.
- Capabilities that switch on together share **one** enabling PR.
- Splitting by layer is fine when each PR is logically whole: it has its own tests,
  exposes a surface a later PR builds on, and a reviewer could take it alone. Don't
  split a layer further than that, and merge two PRs that would always be reviewed
  together.
- A PR that is a single leaf must say why it can't be merged into a neighbour
  (`kept_separate_because`); the switch-on step always is one.

**Inert first, enable last.**
- `inert` PRs change nothing that users or existing clients can observe: no visible UI,
  no change to the behaviour of any existing request. Purely additive internals (a
  column nothing reads, a service nothing calls, a router that isn't registered) are
  inert. A new field in an existing API response, a new validation error on an
  existing endpoint, or anything visible to external API consumers is **not** inert.
  Name the `layer`, say in `unobservable_how` why behaviour doesn't change, and never
  include a leaf tagged `breaking`.
- `enabling` PRs switch a design capability on. Name the `capability` exactly as in the
  design, and give `switch_off`: how to turn it back off.
- `precursor` PRs fix an existing issue (`X-n`) before the work.

**Say what must be true, never how to check it.** GANDALF adds the standard statements
for each PR type (existing tests pass, the repo's own checks pass, it switches off).
Proving them is the builder's work, the way the repo checks its own code; don't write
test or lint commands.

**Safety.**
- **Every in-between state honours the designer's decisions.** After each PR merges
  and before the next lands, which may be days, the system must still honour every
  designer decision and every intent constraint, not just the finished plan. Check what
  each PR exposes on its own: a new column can leak through existing code that sends
  every field, for example. Check correctness too, not just exposure: a half-shipped
  rule can leave data in a state the design forbids. If a split would temporarily break a
  decision, reorder the work or move the fix into the earlier PR. If neither is possible,
  raise a pause. Never call it harmless in the order of operations.
- **Build in the security review's fixes.** You're shown the fixes the security review
  asked for. Each one goes into exactly one PR: the PR that builds what it protects, or
  an earlier one if the protection must be there first. List its id under that PR's
  `security`, put the work in its `changes` and `steps`, and make it checkable in
  `done_when`. Keep every other part of the spec in line with it: a fix that changes how
  something works changes that PR's `out_of_scope`, `breaking` and decisions text too.
  A "consider" fix is optional: list its id under the `security` of the PR it belongs
  with, where the builder sees it as a non-blocking note, but leave it out of `changes`,
  `steps`, `done_when` and `breaking`.
- **Write the check down in `boundaries`**, one entry per PR, in order: what users and
  clients can do and see once it has merged (`state`), and whether every decision and
  every security fix placed so far holds then (`decisions_hold`). If one doesn't,
  describe the `gap` and raise a pause; a plan with an unresolved gap is rejected. A
  separate reviewer checks these states afterwards.
- **One change per file per PR.** If several leaves edit the same file, list the file
  once, say everything that changes in it, and name every leaf it covers in `leaves`.
- Every PR leaves the main branch working. State `breaking` honestly; a breaking change
  needs a mitigation, written in its `detail`. Use separate `expand` and `contract` PRs (both planned now, linked
  by `expand_contract`) rather than leaving a contract step for later.
- **Follow the repo's own rules for how code is produced.** GANDALF plans the feature
  and the order; the repo decides the mechanics.
  - A PR that changes the database says what data changes, in plain terms, in
    `schema_change` (e.g. "tasks gain an optional due date"), and cites the repo's
    migration convention under `conventions`.
  - What a repo tool produces (migrations, protobuf or OpenAPI clients, ORM models) is a
    `generate` change naming the folder the tool writes into, named the way the repo's
    convention names it; never a hand edit. If the repo writes these by hand, they're
    ordinary `create` or `modify` changes. The repo's conventions win over these defaults.
  - A PR holding a migration, auth or breaking leaf alongside other leaves needs
    `steps` that isolate it, and the order of operations says when the migration runs.
- Flags only if the repo already has a flag mechanism; a PR introducing one names the
  PR that removes it (`flag.removed_by`, which sets `removes_flag`).

**Spec content.**
- `summary`: one plain-language line for a person, e.g. "Admins can set a limit on each
  column in settings". `title` and `goal` may be technical.
- `depends_on`: a PR may only use contracts exposed by, and modify files created by, PRs
  it depends on (directly or transitively).
- `starting_state.facts`: what must already be true when the PR starts, as plain
  statements (e.g. "PR-01's favorite model exists, with a unique user and project").
- `read_first`: files to read first, including an existing example to copy.
- `contracts_used` / `contracts_exposed`: contract ids; every contract is exposed by
  exactly one PR.
- `changes`: each file to `create`, `modify` or `delete`, what changes, which pattern to
  follow, and which leaf it implements. Say what changes in the file, not the code that does it. `reuse`: existing code the builder must use
  rather than recreate (cited). A switch-on changes what existing tests assert (a list
  gains an option, a hidden element shows): list each existing test file that asserts
  the behaviour the PR changes as a `modify` change saying what it must assert now,
  where the trace or the repo's test layout names it; when you can't name the file, the
  PR's `steps` or `goal` says the builder updates the existing tests the switch-on
  changes, naming the behaviour. Don't leave the builder to find them from a red suite.
- `conventions`: only those relevant to this PR, each with an example file.
- `done_when`: outcome-level results, each tracing the `O-`/`B-`/`C-`/`X-` ids it proves.
  Every outcome, behaviour and precursor must be proved by some PR. Don't write test
  cases.
- `out_of_scope`: what not to do even if tempting.
- `steps`: only when order inside the PR matters (a large PR, a migration or auth change
  next to others, a repo that wants a series of small commits). Each step is one commit,
  names the files it touches (`changes`, paths from this PR's changes; a file may grow
  over several steps) and says what's true once it's in (`done_when`). Every change
  belongs to at least one step. A fresh builder session may be handed a single step, so a
  step must make sense from the spec plus the earlier steps' commits. Don't add a final
  "run all the checks" step.
- `external`: one entry for each external dependency (`E-n`) this PR's code uses, each
  with its own `when_missing`: what the code does while that dependency isn't there yet.
  It must stay safe (e.g. the feature stays off when its setting is absent). Inert PRs can merge
  before the dependency is ready; an enabling PR waits for every dependency its work
  uses (GANDALF works this out). Write the spec with the settings named in the design;
  where the owning team still has to decide something, say so in the spec.
  Client apps (`kind` `client`) never go here or in the order of operations: nothing
  waits for them.
- `switched_on_by`: if merging an enabling PR changes nothing until certain settings are
  set (the configuration is the real switch), name those `settings`. That PR can then
  merge before its external dependencies are ready. Add the order-of-operations step that
  sets the settings, with `switches_on` naming the PR, after the steps that set up or
  confirm each dependency (`external`). Its `switch_off` shows the settings unset.
- Never change infrastructure-as-code files (rule 12); a PR that does is rejected.

  These fields of an enabling PR that uses an external dependency look like this:

  ```json
  {"steps": [
     {"what": "Add the alert settings table", "changes": ["db/migrations"],
      "done_when": "The table exists after migrating and the repo's migration tests pass"},
     {"what": "Send alerts, off while SMTP_URL is unset", "changes": ["src/lib/alerts.ts", "src/app/api/alert/route.ts"],
      "done_when": "With SMTP_URL set a drop sends one alert; without it the endpoints answer 503"}],
   "switched_on_by": {"settings": ["SMTP_URL", "SMTP_FROM"]},
   "switch_off": {"how": "Unset SMTP_URL; the panel disappears and no mail is sent"},
   "external": [
     {"id": "E-1", "when_missing": "Without SMTP_URL the alert endpoints answer 503 and the panel stays hidden"},
     {"id": "E-2", "when_missing": "Without the hourly call, settings and test alerts work but no real alert is sent"}]}
  ```
- `order_of_operations`: deploy, migrate, backfill and flag steps between merges. A step
  that sets up or confirms an external dependency names it in `external`; follow each
  dependency's `after` (GANDALF checks the order).

## Output format

```json
{
  "prs": [
    {"id": "PR-01", "title": "Add the favorite service", "summary": "The groundwork for favourites, invisible to users",
     "slug": "favorite-service", "type": "inert", "layer": "models",
     "goal": "Implement add/remove/list, called by nothing yet.", "leaves": ["1.1"], "depends_on": [],
     "size": {"production_lines": 70, "total_lines": 150, "files": 2},
     "starting_state": {"description": "Repo as planned.", "facts": ["The projects table and Project model exist"]},
     "read_first": [{"path": "app/services/project_service.py", "why": "service style to copy"}],
     "contracts_used": [], "contracts_exposed": ["C-1"],
     "changes": [{"path": "app/services/favorite_service.py", "action": "create", "what": "C-1 functions",
                  "pattern": "app/services/project_service.py", "leaves": ["1.1"]}],
     "reuse": [{"what": "can_view rule", "where": "app/services/project_service.py:8"}],
     "steps": [],
     "conventions": [{"rule": "Services take the session first", "example": "app/services/project_service.py"}],
     "decisions": ["DEC-1"],
     "breaking": {"is_breaking": false, "detail": "New module only."},
     "unobservable_how": "Nothing calls the service yet.",
     "done_when": [{"outcome": "Adding a favourite twice keeps one", "traces": ["B-1", "C-1"]}],
     "out_of_scope": ["Routes"], "security": ["S-2"]}
  ],
  "order_of_operations": [{"after": "PR-01", "kind": "deploy", "step": "Deploy as usual."}],
  "boundaries": [{"after": "PR-01", "state": "Nothing visible changes; the favorite service exists but nothing calls it.", "decisions_hold": true}]
}
```

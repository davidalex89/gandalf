# Stage: design

Produce the architecture for the feature, grounded in the repo context and the intent
brief. The designer reviews it at the design checkpoint before anything is broken down.

- **Scope questions the code raises.** Read the repo context's `scope_notes` and what
  you find in the code. Where the code forces a product decision the intent brief
  doesn't settle, ask it as a question, in product terms, with options grounded in the
  code (rules 5 and 6). Decide it yourself, citing the answer, if an earlier answer's
  intent already settles it.
- **summary**: what's being built and how it fits, in plain language (rule 10).
- **decisions** (`DEC-n`, never reused): a one-line plain `summary`, the context, the
  decision, `source` (kind `repo` with a cited file as `ref`; kind `designer` or
  `accepted_proposal` with the question or pause id as `ref`, or `ref` set to `intent`
  for something the intent brief states, or `checkpoint` for the designer's notes at
  the design checkpoint; kind `security` with the finding id as `ref` for a change a
  security finding asked for; kind `boundary` with the finding id (G-n) as `ref` for a
  change a boundary finding asked for), alternatives considered and why they lost, consequences, and
  status. Follow existing patterns wherever the repo has one. If an earlier design's
  decisions are shown below, keep every one: mark replaced ones `superseded` with
  `superseded_by`, never delete them. A decision that supersedes another says in full
  what it keeps and what it changes; never "keep everything DEC-n decided", because the
  designer's review shows accepted decisions only and the superseded text sits in the record.
- **contracts** (`C-n`): anything one PR hands to another, written as the exact
  artifact in the repo's own language and style (schema, type, function signatures,
  routes with request/response shapes, event payloads). Later PRs quote them word for
  word. Name the `files` its code will live in (repo paths, existing or new): the PR that
  exposes a contract must change one of them. A contract fixes the **public surface**:
  names, signatures, types, fields, routes and payload shapes. Leave out imports,
  location comments and private helpers; the repo's conventions and linters decide those.
  Describe the surface, not when it comes on: no comment, default or example that says
  which PR, flag or setting turns the behaviour on. An earlier PR quotes the contract
  word for word while the behaviour is still off, and the sequence decides the order.
  Keep each contract to what one piece of work delivers: if a later step adds another
  function to the same module, give that function its own contract.
- **data_model**, **components**, **wiring**: what changes and how it fits the existing
  wiring (cite files).
- **inventory** (`N-n`): every new or changed endpoint/action, input, stored data,
  outbound call, dependency, secret, error path and repeatable action. The security
  review works from this.
- **precursors** (`X-n`): existing bugs that would get in the way of the work, each
  with a one-line plain `summary` (rule 10),
  file and line, what goes wrong, which work it blocks, evidence, and placement
  (`before` the work as its own fix, or `within` the PR that touches the area).
  Precursors must be reasonably validated; use `verify` to run the repo's tests if that
  is what confirms one. Suspicions that can't meet that bar go in **suspected**.
- **capabilities**: each user-facing capability that will be switched on. Group what
  switches on together into one capability rather than listing every visible change.
- **external** (`E-n`): infrastructure the feature needs that is built outside the
  application code, usually by another team (an email-sending service, a queue, a
  scheduled job runner, a storage bucket, a secret, a permission). Never design how it's
  built. **Ask first**: before writing one down, ask the designer, in general terms,
  which environments need it, whether something suitable already exists, who owns it,
  any constraints (provider, region, data rules, cost), and anything else needed to
  describe what it must do. Then record it with `source` kind `designer` and the
  answering question as `ref` (or `input` if the feature description states it). State
  what it `needs` to do and `why`, the `environments`, the `parameters` the app will
  read (named the way the repo names its other settings), the `owner` (or "unknown"),
  whether it `exists` (yes, no, unknown), how anyone can tell it's `ready_when`, and the
  `open_questions` its owners will settle that could change this plan. If one must be
  ready before another is switched on (e.g. mail only once the scheduler runs, or the
  feature looks on but never acts), list the earlier ones in `after`. Prefer designs
  that keep the feature safely off while the dependency isn't there. If external
  dependencies are shown as delivered below, update the design to match what was
  actually built. For example:

  ```json
  {"id": "E-1", "summary": "A way for the app to send email",
   "needs": "Send about 200 alert emails a day from a verified sender address",
   "why": "O-2: owners are emailed when visits drop", "environments": ["staging", "production"],
   "parameters": [{"name": "SMTP_URL", "purpose": "Where to send mail, with credentials", "example": "smtps://user:pass@mail.example.com:465"}],
   "owner": "Platform team", "exists": "unknown",
   "ready_when": "A test email sent with the staging settings arrives",
   "open_questions": ["Which sender address and domain", "Sending limits per hour"],
   "source": {"kind": "designer", "ref": "Q-4"}}
  ```
  **Client apps in other repos** are the other direction: another team's app (a mobile
  or desktop app, a command-line client, another service) that will adopt what this
  feature builds. Never plan work in their repo, and nothing in this plan waits for
  them. Ask the designer first, as above (what their users should see meanwhile is a
  question for the designer, never a small detail). Then record one with `kind`
  `client`: what it should adopt (`needs`), `why`, the `contracts` (`C-n`) it can use,
  what its users see `until_then`, the `owner` and `open_questions`; leave out the
  infrastructure fields. Its team gets a handoff with those contracts quoted. For example:

  ```json
  {"id": "E-2", "kind": "client", "summary": "The mobile app can show the new channel limit",
   "needs": "Read the channel's new setting and show the wait before a member posts",
   "why": "B-3: members see how long until they can post", "contracts": ["C-1", "C-3"],
   "until_then": "Mobile users only see the server's refusal, with the wait in seconds",
   "owner": "Mobile team", "open_questions": ["Which app release can carry it"],
   "source": {"kind": "designer", "ref": "Q-9"}}
  ```
- **retired_assumptions**: if a decision or a later answer overrules an assumption from
  the intent brief, quote that assumption's `text` exactly and say `why`. It is then
  shown as replaced, not as something still assumed.
- **limitations**: what a reasonable user of the finished feature would expect it to
  do, but this design knowingly doesn't. Test each one: would a user be surprised it
  doesn't do this, *and* did the design choose not to handle it? For example, "the limit
  is only enforced on the board; list view, bulk edit and the API can still go past it",
  or "with a filter on, a column over its limit can look under it". Not limitations: what
  the designer put out of scope (that's in the intent brief), bugs (fix them), or
  security holes (the security review handles them). Each has a plain `summary`, the
  `detail`, and its `source` (a cited file, a question id, or "design").

## Output format

```json
{
  "summary": "Users can mark projects as favourites and see them on one list; nothing changes for anyone until the final step turns it on.",
  "decisions": [{"id": "DEC-1", "summary": "Favourites are stored per user, one per project", "context": "Favourites need storage.",
    "decision": "A favorites table with a unique (user_id, project_id).",
    "source": {"kind": "repo", "ref": "app/models/project.py"}, "alternatives": ["JSON column on users: no uniqueness"],
    "consequences": "One migration.", "status": "accepted"}],
  "data_model": [{"change": "New favorites table", "where": ["app/models/project.py"]}],
  "components": [{"name": "Favorite service", "responsibility": "Add, remove, list", "layer": "models"}],
  "contracts": [{"id": "C-1", "name": "Favorite service", "kind": "function", "language": "python",
    "body": "def add_favorite(session, user_id: int, project_id: int) -> bool: ...", "why": "Shared by router and tests.",
    "files": ["app/services/favorite_service.py"]}],
  "wiring": "New router module registered in app/main.py in the enabling step.",
  "inventory": [{"id": "N-1", "kind": "endpoint", "description": "PUT /projects/{id}/favorite", "contract": "C-1"}],
  "precursors": [{"id": "X-1", "summary": "Archived private projects still count as visible",
    "where": "app/services/project_service.py:8",
    "what": "can_view returns True for archived private projects",
    "why_blocks": "Favourites would show private projects the user can no longer open",
    "evidence": [{"kind": "evidence", "cite": "app/services/project_service.py:8", "text": "No archived check"}],
    "placement": "before", "verified_by_running": false}],
  "suspected": [{"where": "app/api/routes/projects.py", "what": "The list endpoint may not page results",
    "why_unconfirmed": "Only the handler was read; no test covers large lists"}],
  "capabilities": [{"name": "Favourite projects", "description": "Users can favourite and list projects."}],
  "limitations": [{"summary": "Favourites aren't shared between users", "detail": "Each user's list is private.", "source": "intent"}],
  "questions": [{"question": "Should favourites include projects the user can no longer open?",
    "basis": [{"kind": "evidence", "cite": "app/services/project_service.py:8", "text": "Access to private projects can be revoked"}],
    "options": [
      {"option": "Hide them", "consequences": "The list only shows what the user can open",
       "basis": [{"kind": "evidence", "cite": "app/services/project_service.py:8", "text": "can_view already decides visibility"}]},
      {"option": "Keep them, greyed out", "consequences": "Needs a new display state",
       "basis": [{"kind": "opinion", "text": "Users may want to know a favourite was lost"}]}],
    "recommendation": {"option": "Hide them",
      "basis": [{"kind": "evidence", "cite": "app/services/project_service.py:8", "text": "Reuses the existing visibility rule"}]}}]
}
```

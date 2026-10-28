<p align="right"><img src="src/gandalf/assets/gandalf.png" alt="" width="128"></p>

# GANDALF

**G**rounded **A**rchitecture, **N**arrow **D**iffs, **A**gent-**L**ed **F**low

GANDALF takes a feature idea and an existing codebase and produces a design plus a
sequence of small PR specs, each one written so a fresh AI coding agent can build it
without a conversation. You describe the feature. It reads the repo, asks you the
questions the repo can't answer, has the design reviewed for security, breaks the work
into PRs that land in a safe order, checks what's true between each PR, and writes the
specs.

It plans. It never writes code, opens PRs or runs agents, and it never touches the
target repo. How code gets produced in your project (migrations, codegen, test commands,
lint, conventions) comes from the repo's own files and is quoted, never invented.

I built it because I kept watching agents do good work on a feature's first PR and then
drift on the third, once the context that justified the first one was gone. A spec that
carries its own starting state, its exact contracts and its done-when fixes that. Getting
the specs to that standard turned out to need a planner with checks of its own, and that
planner is what this repo is.

A few ideas run through everything it does. Every claim about your code cites a file.
Anything the repo and your request leave open, if it changes what gets built or what
users see, becomes a question for you, one at a time. Small details get decided for you
and listed as assumptions you approve at the checkpoint. Work is sliced inert first:
PRs nothing can observe yet, then one small enabling PR that switches each capability
on, so the main branch is never half-shipped. Whatever one PR hands to another, a type,
a signature, a route, is written out exactly and quoted word for word on both sides. And
the parts of the plan that code can check (ordering, coverage, file existence, contract
dependencies, size, who exposes what) are checked by code, and anything wrong goes back
to the model with the reason. The core never calls a model itself. It hands out prompts
and validates what comes back, so any model can drive it, and the Claude Code plugin is
the way I use it every day.

## Requirements

Python 3.11 or newer, and git for the target repo. There are no third-party packages.
To use the plugin you need Claude Code.

## Install

Clone or copy this folder somewhere on your machine. Then, as a Claude Code plugin loaded
straight from the folder:

```bash
claude --plugin-dir /path/to/gandalf
```

Or as a command-line tool, so `gandalf` is on your PATH:

```bash
uv tool install --editable /path/to/gandalf
```

`pipx install --editable /path/to/gandalf` works too, and so does running
`/path/to/gandalf/bin/gandalf` directly. The plugin's `bin/gandalf` uses `$GANDALF_PYTHON`
if you set it, then a `.venv` inside the GANDALF folder, then `python3`.

## Planning in Claude Code

Open Claude Code in the repo you want to plan for, with the plugin loaded, and run:

```text
/gandalf:plan Let users favourite projects and see a list of their favourites.
```

You can pass a file instead, an RFC or a draft spec, plus any supporting files.

Claude orchestrates. Each planning stage runs in a fresh, read-only subagent that sees
only that stage's prompt, so later stages work from what was agreed and never from the
whole chat. The stages are:

1. **Scan.** Code reads the repo's manifests, config and folders. Claude then reads the
   code to confirm and deepen what it found, with file citations.
2. **Questions.** Anything unclear is asked one question at a time. If you don't know,
   say so and Claude researches it and proposes an answer with evidence for you to accept
   or replace. Or say "you decide", for one question or a whole topic, and Claude decides
   and lists what it assumed for you to check at the checkpoint.
3. **Security review.** A separate read-only subagent reviews the design before you see
   it. A fix that changes a contract or a decision sends the design back to be reworked
   and reviewed again. A fix that goes against one of your decisions comes to you as a
   question. Every other fix becomes something a PR must make true, and anything that
   would break your brief is always required.
4. **Design checkpoint.** You review the decisions with their sources, the contracts and
   the capabilities, together with what the security review asked for, and approve or
   say what to change. This is the one fixed stop.
5. **Breakdown and sequencing.** These run without stopping unless something below
   triggers a pause. Each security fix is built into the PR that needs it.
6. **Boundary check.** Another read-only subagent checks what's true after each PR
   merges and before the next lands. A gap sends the PRs back to be re-planned with the
   design standing. A gap only a design change can close goes back through the design,
   its review and the checkpoint. Until this passes, the sequence is a draft.
7. **Render.** The plan is written to the plan folder.

A review can send the plan back more than once. The first time it does so on its own.
From the second round each finding comes to you as a choice: rerun, which does what the
review asks and reviews again, or carry, which keeps the plan and hands the finding on.
A carried security finding becomes a required fix a PR builds in. A carried boundary gap
is written on the PR it follows, for the builder and for your own review.

GANDALF also stops to ask before it adds a dependency, changes existing architecture (an
existing contract, how data is stored, a new pattern where one exists) or adds anything
your request doesn't imply. Infrastructure run outside the app is a question too, and
the answer is recorded as an external dependency for the team that runs it. It never
proposes restructuring your code. When a feature can't fit without that, it tells you
what doesn't fit, where and why, and asks what you want.

## With any other model

`gandalf run` drives every stage through any command that reads a prompt on stdin and
prints JSON, asking you questions in the terminal:

```bash
gandalf run --repo ~/code/my-app --feature feature.md --model-cmd "claude -p"
```

Models that can't read your repo get excerpts of the relevant files up to a size budget
(`--budget`). Whatever was left out is listed in `plan.md`.

Or drive it by hand with any chat model:

```bash
gandalf scan --repo ~/code/my-app --feature feature.md
gandalf prompt --out ~/gandalf-plans/my-app/<feature> --excerpts   # paste this into the model
gandalf submit intent reply.json --out ~/gandalf-plans/my-app/<feature>   # or whichever stage
gandalf render --out ~/gandalf-plans/my-app/<feature>
```

`gandalf prompt` with no stage always tells you the next step. Your answers and the design
approval are JSON too (`gandalf submit answers`, `gandalf submit approval`), and GANDALF
shows the shape when it asks.

## What you get

Plans live outside your repo, by default in `~/gandalf-plans/<repo>/<feature>/`, so they
can't end up in a commit. Move them with `--out` or `GANDALF_HOME`. An `--out` inside the
target repo is refused.

| File | What it is |
|---|---|
| `plan.md` | The plan for people: summary, limitations, decisions that need your eye, what to review first, the slice map, the sequence, risks, and the mistakes caught and fixed during planning |
| `intent-brief.md` | What's being built: problem, outcomes (`O-n`), out of scope, behaviour examples (`B-n`), constraints, each with its source |
| `repo-context.md` | What the scan found, with file evidence: stack, layers, conventions, the repo's rules, run and test commands |
| `design.md` | Decisions with sources, alternatives and consequences, contracts (`C-n`), the inventory of what's new (`N-n`), the breakdown |
| `security-review.md` | What the review checked, abuse cases, findings and what each changed, and the boundary check |
| `sequence.md` | PR order, the grid of lanes and waves, deploy and migration steps, breaking-change mitigations |
| `prs/pr-NN-<name>.md` | One self-contained spec per PR |
| `plan.json` | The same plan as data, with a `schema_version` |

Small plans, two PRs at most and no breaking change, schema change or new dependency, get
a lighter set: `plan.md`, one `technical.md` holding the rest, `prs/` and `plan.json`. The
PR specs are identical either way.

### A PR spec

Every spec has the same fourteen sections, and a section that doesn't apply says "None",
so a builder never wonders whether something was forgotten. Goal. Starting state, the
facts that must already be true from earlier PRs. Read first. Contracts used, quoted
exactly. The contract this PR exposes, its public surface quoted exactly and the files it
lives in. Changes, each file tagged `exists`, `created by PR-nn`, `new (parent exists)`
or `unverified`, plus a list of existing code to reuse. Order of steps, when order
matters, each with what's true once its commit is in. Conventions, including the repo's
own rules from its `CLAUDE.md`, contribution guide and linters. Decisions. Breaking
changes, or how an inert PR stays unobservable, or how an enabling PR switches back off.
Security, the review's fixes this PR builds in. Done when, the outcomes it delivers plus
what holds for its type: existing tests pass unchanged for an inert PR, an enabling PR
works end to end and can be switched off, and every PR passes the repo's own lint,
format, type and secret checks the way the repo runs them. The spec says where those
checks are defined and never dictates the commands. Out of scope, which always includes
"don't add the plan to the repo". And what to do if reality doesn't match.

The header carries the type (inert, enabling, precursor), the lane, which is a chain one
builder can take end to end, the wave, which says what must have merged first, risk tags,
a size band from the planner's estimate (Small to about 150 production lines, Medium to
350, Large to 500, Over), and dependencies.

### Building from the plan

Hand each spec to a fresh agent session in the target repo, in order, with read access to
the plan folder:

```bash
claude --add-dir ~/gandalf-plans/my-app/<feature>
```

A PR with ordered steps can also be built one step per session. Give the session the spec
and a step number. It builds that step as one commit and stops when the step's done-when
holds.

Section 14 of every spec has two tiers. A small fix inside the PR's own files that leaves
every contract, decision and starting-state fact as written is the builder's to make. It
carries on and hands you a deviation note with the finished PR. Keep those notes with the
plan folder, and give each later builder the notes from the PRs its spec depends on, since
a note can move where something lives. Anything bigger, a starting-state fact that isn't
true, a contract that can't be provided as written, a file not listed, a new dependency,
stops the builder, which writes a mismatch report. Repair the unbuilt part of the plan
with:

```bash
gandalf repair --out <plan folder> --report mismatch.md --merged PR-01,PR-02
```

Merged PRs and the contracts they expose are frozen. The repo is scanned again, the
affected stages re-run for the unbuilt PRs with every check applying, and nothing is
deleted: replaced outputs go to `.state/history/` and replaced decisions are marked
superseded.

### Watching PRs as they're built

Builders change things along the way, often for good reasons. Monitoring is off by
default. Turn it on for a plan with `gandalf monitor on`, then check a branch:

```bash
gandalf monitor check PR-03 --branch <branch> --out <plan folder> [--done] [--merged PR-01,PR-02] [--model-cmd "claude -p"]
```

It reads the branch from your local repo without checking out or fetching anything. Code
inside the PR's planned files is the builder's business. What gets flagged is what other
PRs rely on: a contract written differently, files outside the spec, planned files left
out when the builder says it's done, new dependencies or migrations the plan lacks, and
infrastructure code or anything that looks like a real secret. A model judges only the
flagged signals. A change that stays inside its PR updates that PR's spec. A change that
reaches other PRs or touches something you approved waits for `gandalf monitor approve`
or `reject`. Every finding goes into the plan's change history.

### Two rules that never bend

No secrets in the repo. GANDALF never plans a password, key or token anywhere in the
codebase, not even as a local default. Secrets are read at runtime through the repo's own
configuration, examples use placeholders like `<your-api-key>`, and a plan containing
anything that looks like a real secret is rejected.

Application code only. GANDALF never writes infrastructure as code or configuration
management, and that includes everything under `.github/`, `CODEOWNERS` and CI pipeline
files, even when the repo ships that code. The app's own settings, `.env.example`,
Dockerfile and local compose file are normal work. When a feature needs infrastructure
from elsewhere, say an email service or a job runner, the design asks you about it and
records an external dependency (`E-n`): `plan.md` lists it under "Needed from other
teams", `requests/e-NN-<name>.md` is a tool-neutral brief for the owning team, and every
PR that uses it says what its code does while it's missing. When it's built, record what
was delivered with `gandalf deliver E-1 --details built.md`, and GANDALF re-plans the
unbuilt work if the delivery differs from the request. The other direction, another
team's app that should adopt the feature, is recorded as a client app with a handoff
document in `handoffs/`, and nothing in your plan waits for it.

## Folders that aren't git repos

GANDALF works on plain folders with less: no test runs, a content fingerprint instead of
a commit to pin the plan to, and ordered change sets instead of PRs. `plan.md` opens with
a banner saying so.

## Commands

| Command | What it does |
|---|---|
| `gandalf scan` | Scans the target repo without a model and creates the plan folder |
| `gandalf run` | Plans start to finish with a model command, asking you questions in the terminal |
| `gandalf prompt [stage]` | Prints the prompt for the next stage, or a named one. `--excerpts` adds repo files for models that can't read the repo |
| `gandalf submit <stage> FILE` | Validates and stores a stage's output, your answers or your approval |
| `gandalf render` | Writes the plan once every stage is complete and every check passes |
| `gandalf repair` | Repairs the unbuilt part of a plan after a mismatch report |
| `gandalf deliver E-n` | Records what another team built for an external dependency |
| `gandalf monitor ...` | `on`, `off`, `check`, `prompt`, `submit`, `approve`, `reject`, `history` |

Everything that happens to a plan is logged to `<plan folder>/.state/log.jsonl`: every
rejection with its reasons, every acceptance, every error. Rejected outputs are kept in
`.state/rejected/`, and `plan.md` summarises the log so you can see what the checks caught.
You can stop at any point and pick up later with `gandalf prompt --out <plan folder>`.

From 1.0 the plan folder's layout, the fourteen spec sections, `plan.json` with its
`schema_version` and the command line change only in a major version. Prompts and checks
keep improving in minor ones.

## Development

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .
```

The core is `src/gandalf/`, the prompt templates are `src/gandalf/prompts/`, and the
Claude Code plugin is the repo root: `.claude-plugin/`, `skills/`, `agents/`, `hooks/`
and `bin/`.

## Licence

[Apache License 2.0](LICENSE).

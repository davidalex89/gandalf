# GANDALF

**G**rounded **A**rchitecture, **N**arrow **D**iffs, **A**gent-**L**ed **F**low

GANDALF turns a feature idea into a **grounded design and a sequence of small,
self-contained PR specs** for an existing codebase. The specs are written for AI coding
agents: each one can be handed to a fresh agent session and built without re-planning,
guessing, or wandering off on tangents.

It doesn't write code, open PRs or run agents. It plans, and it checks its own plan.
It defines the product feature and the order of the work. How code is produced
(migrations, generated code, test and lint commands, conventions) follows the target
repo's own rules, as found in the repo.

- **Grounded, never invented.** Every claim about your code cites a file. Anything the
  repo and your request don't settle, and that changes what's built or what users
  experience, becomes a question for you. Smaller details are decided for you and listed
  as assumptions for you to approve at the design checkpoint.
- **Don't break what works.** Work is sliced *inert first* (code nothing can observe
  yet), with a small **enabling** PR at the end that switches each capability on.
- **Exact contracts.** Whatever one PR hands to another (a schema, function
  signatures, routes) is written out exactly and quoted word for word in both specs.
- **Checked by code.** Code validates the ordering, coverage, file existence, contract
  dependencies and size bands, and sends anything wrong back to the model with the reason.
  Plans are checked against the commit they were made from, so whatever branch you have
  checked out doesn't change the answer.
- **Model-agnostic, tailored to Claude Code.** The core never calls a model. It hands
  out prompts and validates what comes back, so any model can drive it.

## Requirements

- Python 3.11 or newer. No other runtime dependencies, and no third-party packages.
- git, for the target repo. Without it GANDALF runs in [reduced mode](#reduced-mode-non-git-folders).
- Claude Code, to use the plugin (the main way to run it).

## Status: what has been exercised

Everything in GANDALF is tested in the unit suite, but only part of it has been through a
real run on a real repository, and that line is worth knowing. From 1.0 the plan folder's
layout, the fourteen sections of a PR spec, `plan.json` with its `schema_version` and the
command line change only in a major version; prompts, checks and the experimental features
below may change in a minor one.

**Exercised on real repositories** (Python/FastAPI, Go, Django and Node/Vue targets, six
planning runs, two of them built end to end from the specs by fresh agents with no
questions): the seven stages from scan to render, the deterministic checks and the
rejection loop, the security review and its required fixes, the design checkpoint
including a rejection and re-run, the boundary check routing a finding back to design,
contracts quoted across PRs, and the inert-then-enabling order.

**Experimental: never fired in a real run.** Precursors (`X-n`), external dependencies
and client handoffs (`E-n`, `requests/`, `handoffs/`), feature flags and expand/contract
pairs, verification runs in a worktree, don't-know proposals, delegated questions,
restructuring pauses, the light tier, `gandalf repair` and `gandalf monitor`. They are
kept as designed and will change when a run shows what they get wrong.

**One review pass is not a gate.** On the same prompt and the same design, the boundary
check found a data leak in one run and missed it in the next. A plan that passes its
checks has been reviewed, not cleared: the specs that touch who can see what deserve your
own read, and a second review pass with a fresh context is cheap insurance.

## Install

Clone or copy this folder somewhere on your machine. Then choose either or both:

**As a Claude Code plugin**, loaded from the local folder with nothing to install:

```bash
claude --plugin-dir /path/to/gandalf
```

**As a command-line tool**, so `gandalf` is on your PATH:

```bash
uv tool install --editable /path/to/gandalf
```

(`pipx install --editable /path/to/gandalf` works too, or run `/path/to/gandalf/bin/gandalf`
directly.)

The plugin's `bin/gandalf` uses `$GANDALF_PYTHON` if you set it, else a `.venv` inside the
GANDALF folder, else `python3`.

## Use it in Claude Code

Open Claude Code **in the repo you want to plan for**, with the plugin loaded, and run:

```text
/gandalf:plan Let users favourite projects and see a list of their favourites.
```

You can also pass a path to a file holding the description (an existing RFC or spec is
fine), plus any supporting files.

Claude acts as the orchestrator. Each planning stage runs in a fresh, read-only
subagent that sees only that stage's prompt, so later stages work from agreed results
rather than the whole conversation. With you, it goes through these stages:

1. **Scan.** Code reads the repo's manifests, config and folders. Claude then reads the
   code to confirm and deepen what it found, citing files.
2. **Questions.** Anything unclear is asked **one question at a time**. If you don't
   know, say so: Claude researches it and proposes an answer, with its evidence, for
   you to accept or replace. Or say "you decide" (for one question or a whole topic):
   Claude decides and lists what it assumed, for you to check at the design checkpoint.
   Only questions that change what's built or what users experience are asked. Smaller
   details are decided and listed the same way.
3. **Security review.** A separate subagent with read-only tools reviews the design
   before you see it. A fix that changes a contract or decision sends the design back
   to be reworked and reviewed again. A fix that goes against one of your decisions
   comes to you as a question. Every other fix becomes something a PR must make true.
   Anything that would break your brief is always a required fix.
4. **Design checkpoint.** You review the design (decisions with their sources,
   contracts, capabilities) together with what the security review asked for, and
   approve it or say what to change. This is the one fixed pause.
5. **Breakdown and sequencing.** These run without stopping, unless something triggers a
   pause (below). Each security fix is built into the PR that needs it, in its changes,
   steps and "done when".
6. **Boundary check.** A separate subagent checks what's true after each PR merges and
   before the next lands. A gap sends the PRs back to be re-planned with the design
   standing; a gap only a design change closes goes back to the design, its review and
   the checkpoint first. Until this check passes, the PR sequence is a draft.
7. **Render.** The plan is written to the plan folder.

A review can send the plan back more than once. The first time it does so without asking.
From the second round on, each finding that would send the plan back comes to you as a
choice: **rerun**, which does what the review asks and reviews again, or **carry**, which
keeps the plan and hands the finding on. A carried security finding becomes a required fix
a PR builds in; a carried boundary gap is written on the PR it follows, for the builder and
for your own review. The next-step text says which round it is.

GANDALF also stops and asks you whenever it would:
- add a new dependency
- change the existing architecture (an existing contract or interface, how existing
  data is stored, a new pattern where one already exists)
- add something your request doesn't imply.

Infrastructure run outside the app isn't a pause: GANDALF asks you about it and records
it as an external dependency for the team that runs it.

It never proposes restructuring your code. If a feature can't fit without it, GANDALF
explains what doesn't fit, where and why, and asks for your guidance.

## Use it with any other model

`gandalf run` drives every stage through any command that reads a prompt on stdin and
prints JSON. It asks you questions in the terminal:

```bash
gandalf run --repo ~/code/my-app --feature feature.md --model-cmd "claude -p"
```

Models that can't read your repo themselves get excerpts of the relevant files, up to a
size budget (`--budget`). Anything left out is listed in `plan.md`, never dropped
silently.

To drive it by hand (copy and paste with any chat model):

```bash
gandalf scan --repo ~/code/my-app --feature feature.md
gandalf prompt --out ~/gandalf-plans/my-app/<feature> --excerpts   # paste this into the model
gandalf submit intent reply.json --out ~/gandalf-plans/my-app/<feature>   # or whichever stage
gandalf render --out ~/gandalf-plans/my-app/<feature>
```

`gandalf prompt` with no stage always tells you the next step. Designer answers and the
design approval are submitted as JSON too (`gandalf submit answers ...`,
`gandalf submit approval ...`). The formats are shown when GANDALF asks for them.

## What it produces

Plans **never go inside your repo**, so they can't end up in a commit. The default
location is `~/gandalf-plans/<repo>/<feature>/`. Change it with `--out`, or move the root
with `GANDALF_HOME`. GANDALF refuses any `--out` inside the target repo.

**Full tier** (most features):

| File | What it is |
|---|---|
| `plan.md` | The human-readable plan, in plain language. Start here: summary, known limitations, decisions needing your attention, "review these first", slice map, sequence, risks, notes, and the mistakes GANDALF caught and had fixed during planning |
| `intent-brief.md` | What's being built: problem, outcomes (`O-n`), out of scope, behaviour examples (`B-n`), constraints. Each item carries its source |
| `repo-context.md` | What the scan found: stack, layers, conventions, the repo's own rules, run/test commands, contradictions. All with file evidence |
| `design.md` | Decisions (with sources, alternatives and consequences), contracts (`C-n`), inventory of what's new (`N-n`), precursors (`X-n`), the breakdown, the slice map |
| `security-review.md` | What the separate review checked, abuse cases, findings and what each changed, and the boundary check between PRs |
| `sequence.md` | PR order, the grid of lanes and waves, deploy/migration steps, breaking-change mitigations, flags |
| `prs/pr-NN-<name>.md` | One self-contained spec per PR |
| `plan.json` | The same plan as structured data, versioned (`schema_version`) |

**Light tier.** This applies when the plan has at most two PRs and no breaking change,
schema change, new dependency or external dependency. You get `plan.md`, `technical.md`, `prs/` and `plan.json`.
`technical.md` holds the intent, repo context, design, security review (with the
boundary check) and order of operations in one file. PR specs are identical in both tiers.

In both tiers, `plan.md` is written for a person. `plan.json`, the PR specs and the
supporting documents stay technical, for the builders and any tooling.

### Each PR spec

Every spec has the same 14 sections. A section that doesn't apply says "None", so an
agent never has to wonder whether something was forgotten:

1. **Goal**
2. **Starting state**: what must already be true from earlier PRs
3. **Read first**
4. **Contracts used**, quoted exactly
5. **Contract exposed**: its public surface (names, signatures, types, shapes) quoted
   exactly, and the files it lives in. Imports and layout follow the repo's conventions
6. **Changes**, each file tagged:
   - `exists`
   - `created by PR-nn`
   - `new (parent exists)`
   - `unverified`

   plus a "Reuse — do not recreate" list
7. **Order of steps**, each with what's true once its commit is in
8. **Conventions**, including the repo's own rules (its `CLAUDE.md`, contribution guide,
   linters and so on)
9. **Decisions**
10. **Breaking changes**: how an inert PR stays unobservable, or how to switch an
    enabling PR back off
11. **Security**: the security review's fixes this PR builds in, and any optional
    "Consider" notes
12. **Done when**: the outcomes this PR delivers, plus what must hold for its type.
    Existing tests pass unchanged for an inert PR. An enabling PR works end to end and
    can be switched back off. Every PR passes the repo's own lint, format, type and secret
    checks, the way the repo enforces them. The spec says where the repo defines those
    checks. It never prescribes the commands: showing the work is done is the builder's
    job.
13. **Out of scope**, which always includes "don't add the plan to the repo"
14. **If reality doesn't match**: stop, and write a mismatch report

The header shows:
- the type (inert/enabling/precursor)
- the lane, a chain one builder can take end to end, and the wave, which says what must
  have merged before it can start
- risk tags
- a size band, the planner's estimate shown for the reader: Small up to ~150 production
  lines, Medium up to ~350, Large up to ~500, Over. A PR of one leaf says why it stays
  separate; a PR of more than ten files says why it stays whole
- dependencies.

### Building from the plan

Give each PR spec to a **fresh** agent session in the target repo, in order, with read
access to the plan folder:

```bash
claude --add-dir ~/gandalf-plans/my-app/<feature>
```

A PR with ordered steps (section 7 of its spec) can also be built one step per fresh
session: give the session the spec and a step number. It builds that step as one commit
on the PR's branch, after the earlier steps' commits, and stops once that step's "done
when" holds.

Your own review of built PRs is up to you. `plan.md` suggests a natural point for it after
each capability is switched on, but nothing in GANDALF waits for it.

### When reality doesn't match

Section 14 of every spec has two tiers. A small fix inside the PR's own files that leaves
every contract, decision and starting-state fact as written, and adds no dependency or data
change, is the builder's to make: it carries on and lists the fix in a `GANDALF DEVIATION
NOTE` handed to you with the finished PR. Keep those notes with the plan folder; they are
your record of what the specs didn't foresee, and the plan doesn't change. Hand each later
builder the notes from the PRs its spec depends on: a note can move where something lives
(a style block, a helper), and the later spec was written before that happened.

Anything further, a starting-state fact that isn't true, a contract that can't be provided as
written, a file not listed, a new dependency or data change, stops the builder, which writes a
`GANDALF MISMATCH REPORT`. Repair the unbuilt part of the plan with:

```bash
gandalf repair --out <plan folder> --report mismatch.md --merged PR-01,PR-02 [--from context|design|decompose|sequence]
```

Repair works like this:
- **Merged PRs and the contracts they expose are frozen.** They're never changed.
- **The repo is re-scanned**, because it has moved on since planning.
- **The affected stages are re-run** for the unbuilt PRs, with every check applying as
  normal. A repair from the design stage goes back through the security review and the
  design checkpoint.
- **Nothing is deleted.** Replaced outputs are archived in `.state/history/`, and
  replaced decisions are marked superseded.

### Watching PRs as they're built (optional)

Builders sometimes change things along the way, often for good reasons. PR monitoring
checks whether a change disrupts the rest of the plan. It's off unless you turn it on
for a plan:

```bash
gandalf monitor on --out <plan folder>
```

```bash
gandalf monitor check PR-03 --branch <branch> --out <plan folder> [--done] [--merged PR-01,PR-02] [--model-cmd "claude -p"]
```

GANDALF reads the branch from the repo on your machine without changing anything there:
no checkout and no fetch, so the branch must already be present locally. Code inside
the PR's planned files is the builder's business. GANDALF flags only what other PRs rely
on:
- a contract written differently from the plan
- files outside the spec, especially ones another PR plans to change
- planned files left out (with `--done`)
- new dependencies, and migrations the plan doesn't have
- infrastructure code or real-looking secrets, which are never accepted.

If nothing is flagged, that's the answer. Otherwise a model judges only the flagged
signals: pass `--model-cmd`, or use `gandalf monitor prompt CH-n` and
`gandalf monitor submit CH-n FILE`.
- **A real change that stays inside its PR** updates that PR's spec automatically.
- **A change that reaches other PRs**, or touches a contract, decision, dependency or
  data change you approved, waits for you: `gandalf monitor approve CH-n` or
  `gandalf monitor reject CH-n`, with optional `--notes`. Rejecting writes the builder a
  note in `monitor/` saying what to bring back to the spec.
- **Work that's simply missing** when the builder says the PR is done is "not finished":
  the builder gets a note of what's left, and the plan doesn't change.
- **A change against a design decision** sends the design back for re-planning when you
  approve it. A change that alters a contract runs the security review again, and the
  unbuilt PRs are re-sequenced to build in its fixes.

Every finding goes into the plan's **change history**, in `plan.md`, the specs it
changed and `plan.json`. `gandalf monitor history` lists it. To run checks
automatically, call the command from your own hook or scheduled task. GANDALF doesn't
install either, and there's no CI integration.

### No secrets in the repo

GANDALF never plans a password, passcode, key, token or other secret anywhere in the repo
(code, config, tests, docs or examples), not even as a default for local development.
Secrets are read at runtime through the repo's own configuration. Plans containing
anything that looks like a real secret are rejected. Examples use empty or obviously fake
values such as `<your-api-key>`. If the repo runs a secret scanner, every PR runs it too.

### Infrastructure built by another team

GANDALF plans application code only. It never writes infrastructure as code
(Terraform, CloudFormation, Pulumi, CDK, Bicep/ARM, Kubernetes or Helm manifests) or
configuration management. That covers Puppet, Ansible, Chef and Salt, and also the repo's
GitHub and CI configuration: everything under `.github/`, `CODEOWNERS`, and CI pipeline
files. The rule holds even when the repo contains that code or ships it with the product.
GANDALF never plans PRs for another repo either. What the app itself owns (its settings,
`.env.example`, Dockerfile, local docker-compose) is normal work, and so are linter,
formatter and pre-commit configuration and contribution docs.

When a feature needs infrastructure from elsewhere (an email service, a queue, a
scheduled job runner), the design stage first asks you general questions: which
environments, whether something suitable exists, who owns it, any constraints. It then
records an **external dependency** (`E-n`):
- `plan.md` lists it under "Needed from other teams", and the Sequence marks each PR that
  waits for it as blocked, with who owns it and how to confirm it's ready
- `requests/e-NN-<name>.md` is a tool-neutral brief for the owning team: what it must
  do, the settings the app will read, environments, security requirements, open
  questions, and what to send back
- every application PR is still fully specified. PRs that use the dependency say, for
  each one, what their code does while it's missing. The switch-on PR waits for it to be
  ready, unless the feature is switched on by configuration: then the PR can merge
  early, and the step that sets the configuration waits instead
- if one dependency must be ready before another (say, mail only once the scheduler
  runs), the plan says so and GANDALF checks the order.

When it's built, record what was actually delivered:

```bash
gandalf deliver E-1 --out <plan folder> --details built.md [--as-requested] [--merged PR-01,PR-02]
```

`--as-requested` just records it. Otherwise GANDALF re-plans the unbuilt work from the
design stage, the same way as a repair, so the specs match what was really built.

The other direction is **another team's app** that should adopt the feature: a mobile or
desktop app, a command-line client, another service. GANDALF asks you about it first,
including what its users see meanwhile, then records it as a client app (`E-n` with
`kind: client`). It lists the app under "For other teams to adopt" in `plan.md`, and
writes `handoffs/e-NN-<name>.md` for that team: what to adopt, the exact contracts, when
it's available and what its users see until then. Nothing in the plan waits for it, and
GANDALF never plans work in its repo.

## Reduced mode (non-git folders)

GANDALF works on folders that aren't git repositories, with less available:
- **No test runs** to verify suspected bugs. Findings that would have needed one are
  marked "not verified by running".
- **No commit to record.** A content fingerprint of the files stands in for the commit
  the plan was made against.
- **Ordered change sets** instead of pull requests.

`plan.md` opens with a banner saying exactly what's unavailable. Reduced mode is covered
by GANDALF's own test suite only.

## Commands

| Command | What it does |
|---|---|
| `gandalf scan` | Scans the target repo (no model) and creates the plan folder |
| `gandalf run` | Plans start to finish with a model command, asking you questions in the terminal |
| `gandalf prompt [stage]` | Prints the prompt for the next stage, or a given one. `--excerpts` adds repo files for models that can't read the repo |
| `gandalf submit <stage> FILE` | Validates and stores a stage's output, your answers, or your design approval |
| `gandalf render` | Writes the plan once every stage is complete and every check passes |
| `gandalf repair` | Repairs the unbuilt part of a plan after a mismatch report. Covered by the test suite only |
| `gandalf deliver E-n` | Records what another team built for an external dependency, re-planning if it differs from the request. Covered by the test suite only |
| `gandalf monitor …` | Optional: compares a PR's branch with its spec and reconciles changes (`on`, `off`, `check`, `prompt`, `submit`, `approve`, `reject`, `history`) |

Every rejected submission, acceptance, value normalisation and internal error is logged
to `<plan folder>/.state/log.jsonl`. Rejected outputs are kept in `.state/rejected/`,
and `plan.md` summarises the log.

Planning progress lives in `<plan folder>/.state/`. That's where every stage's
validated output, your answers, question counts and history are kept. You can stop at
any point and pick up later with `gandalf prompt --out <plan folder>`. A plan made by an
older GANDALF, in an earlier format, isn't read: start it again with `gandalf scan`.

## Development

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .
```

What the pieces are:
- **The core** is in `src/gandalf/`.
- **Prompt templates** are in `src/gandalf/prompts/`.
- **The Claude Code plugin** is the repo root: `.claude-plugin/`, `skills/`, `agents/`,
  `hooks/` and `bin/`.

## Licence

[Apache License 2.0](LICENSE).

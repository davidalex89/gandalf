"""Which model runs each stage.

Each stage names a tier (``strong``, ``standard``, ``fast``) or a model directly, and each
tier names a model. Model names change every few months; what a stage needs doesn't, so a
new model is a one-line change to a tier. Four layers, each overriding the one before:

1. built-in defaults (below)
2. the user's file, ``<plans home>/config.toml`` (``$GANDALF_CONFIG`` points elsewhere)
3. the copy saved with a plan when it was scanned, changed later with ``gandalf models --set``
4. ``--model STAGE=VALUE`` and ``--tier TIER=MODEL`` on ``scan`` and ``run``

A plan keeps the settings it was scanned with, so a plan resumed next week runs on the same
models even if the user's file changed. The core never calls a model: the plugin's skill
starts each stage's subagent on the model named here, and ``gandalf run`` puts it into
``model_cmd`` wherever the command says ``{model}``.

Escalation: after ``escalate_after`` rejections of a stage since it was last
accepted, its next attempt runs one tier up (fast to standard, standard to strong), and again
after as many more. A stage that names a model directly stays on it. This is what makes a
cheap model a safe default for the structured stages: a wrong shape costs a retry, and a
stage that keeps failing moves to a model that can do it.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from gandalf.repo import plans_home

STAGES = ("context", "intent", "design", "security", "decompose", "sequence", "boundary", "monitor")
TIERS = ("strong", "standard", "fast")
NEXT_TIER = {"fast": "standard", "standard": "strong"}
DEFAULT_ESCALATE_AFTER = 2

# Defaults from measured runs. Every real catch came from the design,
# security and boundary stages, and the security and boundary stages on Sonnet missed findings
# the strong tier caught, so they stay strong. Context and sequence on Haiku were tested on two
# repositories each: each sequence needed one retry, as Sonnet's did, and the boundary check
# found no more in them than in Sonnet's; Haiku's context found a real defect Sonnet's missed.
# Intent and decompose have no fast-tier evidence yet and stay standard. Escalation
# covers a fast stage that keeps failing.
DEFAULT_TIERS = {"strong": "opus", "standard": "sonnet", "fast": "haiku"}
DEFAULT_STAGES = {"context": "standard", "intent": "standard", "design": "strong", "security": "strong",
                  "decompose": "standard", "sequence": "fast", "boundary": "strong", "monitor": "standard"}

TEMPLATE = """\
# GANDALF settings: which model runs each planning stage.
# A stage names a tier (strong, standard, fast) or a model directly.
# Settings are copied into a plan when it's scanned; change a plan later with
#   gandalf models --out <plan folder> --set design=sonnet

[tiers]
strong = "opus"
standard = "sonnet"
fast = "haiku"

[stages]
context = "standard"
intent = "standard"
design = "strong"
security = "strong"
decompose = "standard"
sequence = "fast"
boundary = "strong"
monitor = "standard"

[retry]
# After this many rejections in a row, a stage's next attempt runs one tier up
# (fast to standard, standard to strong). 0 turns it off.
escalate_after = 2

[run]
# For `gandalf run` and `gandalf monitor check`: a command that reads a prompt on stdin and
# prints JSON. {model} becomes each stage's model.
# model_cmd = "claude -p --model {model}"
"""


class SettingsError(ValueError):
    pass


def config_path() -> Path:
    env = os.environ.get("GANDALF_CONFIG")
    return Path(env).expanduser() if env else plans_home() / "config.toml"


def _check(layer: str, tiers: dict, stages: dict) -> None:
    problems = []
    for key, value in tiers.items():
        if key not in TIERS:
            problems.append(f"{layer}: unknown tier '{key}' (tiers are {', '.join(TIERS)})")
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{layer}: tier '{key}' needs a model name")
    for key, value in stages.items():
        if key not in STAGES:
            problems.append(f"{layer}: unknown stage '{key}' (stages are {', '.join(STAGES)})")
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{layer}: stage '{key}' needs a tier or a model name")
    if problems:
        raise SettingsError("; ".join(problems))


def load_user() -> dict:
    """The user's file as {"tiers": {...}, "stages": {...}, "run": {...}}; empty when absent."""
    path = config_path()
    if not path.is_file():
        return {"tiers": {}, "stages": {}, "run": {}, "retry": {}}
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path}: not valid TOML ({exc})") from None
    unknown = set(data) - {"tiers", "stages", "run", "retry"}
    if unknown:
        raise SettingsError(f"{path}: unknown section(s) {', '.join(sorted(unknown))} "
                            "(sections are tiers, stages, retry, run)")
    out = {k: dict(data.get(k) or {}) for k in ("tiers", "stages", "run", "retry")}
    _check(str(path), out["tiers"], out["stages"])
    if set(out["run"]) - {"model_cmd"}:
        raise SettingsError(f"{path}: [run] only takes model_cmd")
    if set(out["retry"]) - {"escalate_after"}:
        raise SettingsError(f"{path}: [retry] only takes escalate_after")
    if "escalate_after" in out["retry"]:
        _check_escalate(str(path), out["retry"]["escalate_after"])
    return out


def _check_escalate(layer: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SettingsError(f"{layer}: escalate_after must be a whole number, 0 or more (0 turns it off)")


def parse_flags(models: list[str] | None, tiers: list[str] | None, escalate_after: int | None = None) -> dict:
    """``--model design=sonnet``, ``--tier strong=claude-opus-5-5`` and ``--escalate-after N``
    as a settings layer."""
    def pairs(items, flag):
        out = {}
        for item in items or []:
            key, sep, value = item.partition("=")
            if not sep or not key.strip() or not value.strip():
                raise SettingsError(f"{flag} {item!r}: write it as NAME=VALUE")
            out[key.strip()] = value.strip()
        return out
    layer = {"tiers": pairs(tiers, "--tier"), "stages": pairs(models, "--model"), "run": {}, "retry": {}}
    _check("command line", layer["tiers"], layer["stages"])
    if escalate_after is not None:
        _check_escalate("--escalate-after", escalate_after)
        layer["retry"]["escalate_after"] = escalate_after
    return layer


def resolve(*layers: tuple[str, dict]) -> dict:
    """Merge (name, layer) pairs over the defaults. Returns the plan's settings:
    {"tiers": {tier: model}, "stages": {stage: tier-or-model}, "run": {...}, "retry": {...},
    "source": {key: layer}}."""
    tiers, stages, run = dict(DEFAULT_TIERS), dict(DEFAULT_STAGES), {}
    retry = {"escalate_after": DEFAULT_ESCALATE_AFTER}
    source = ({f"tiers.{k}": "default" for k in tiers} | {f"stages.{k}": "default" for k in stages}
              | {"retry.escalate_after": "default"})
    for name, layer in layers:
        for k, v in (layer.get("tiers") or {}).items():
            tiers[k] = v
            source[f"tiers.{k}"] = name
        for k, v in (layer.get("stages") or {}).items():
            stages[k] = v
            source[f"stages.{k}"] = name
        for k, v in (layer.get("run") or {}).items():
            run[k] = v
            source[f"run.{k}"] = name
        for k, v in (layer.get("retry") or {}).items():
            retry[k] = v
            source[f"retry.{k}"] = name
    return {"tiers": tiers, "stages": stages, "run": run, "retry": retry, "source": source}


def model_for(settings: dict, stage: str) -> tuple[str, str]:
    """(model, tier) for a stage; tier is "" when the stage names a model directly."""
    value = settings["stages"].get(stage) or DEFAULT_STAGES.get(stage, "standard")
    if value in TIERS:
        return settings["tiers"].get(value) or DEFAULT_TIERS[value], value
    return value, ""


def escalate_after(settings: dict) -> int:
    return (settings.get("retry") or {}).get("escalate_after", DEFAULT_ESCALATE_AFTER)


def model_for_attempt(settings: dict, stage: str, rejections: int) -> tuple[str, str, str]:
    """(model, tier, base model) for a stage's next attempt after ``rejections`` in a row since
    it was last accepted. The model differs from the base model once it has escalated."""
    base, tier = model_for(settings, stage)
    after = escalate_after(settings)
    if not tier or not after:
        return base, tier, base
    for _ in range(rejections // after):
        if tier not in NEXT_TIER:
            break
        tier = NEXT_TIER[tier]
    return (settings["tiers"].get(tier) or DEFAULT_TIERS[tier]), tier, base


def for_plan(plan) -> dict:
    """A plan's saved settings; a plan scanned before 1.1 gets the defaults."""
    saved = plan.meta.get("models")
    return saved if saved else resolve()


def save_for_plan(plan, settings: dict) -> None:
    plan.meta["models"] = settings
    plan.save()


def command_for(template: str, model: str) -> str:
    """``claude -p --model {model}`` with the stage's model in place; a command without
    ``{model}`` is used as it is, for every stage."""
    return template.replace("{model}", model) if "{model}" in template else template


def table(settings: dict) -> list[str]:
    """One line per stage for people: stage, model, tier, where the setting came from."""
    lines = []
    for stage in STAGES:
        model, tier = model_for(settings, stage)
        src = settings.get("source", {}).get(f"stages.{stage}", "default")
        if src == "default" and settings["stages"].get(stage) != DEFAULT_STAGES.get(stage):
            src = "the default when the plan was scanned"       # defaults change between versions
        via = f"{tier} tier" if tier else "set directly"
        tier_src = settings.get("source", {}).get(f"tiers.{tier}", "default") if tier else src
        lines.append(f"  {stage:<10} {model:<24} {via} (stage from {src}"
                     + (f", tier from {tier_src})" if tier else ")"))
    after = escalate_after(settings)
    lines.append(f"  a stage moves up one tier after {after} rejections in a row" if after
                 else "  escalation is off: a rejected stage retries on the same model")
    return lines

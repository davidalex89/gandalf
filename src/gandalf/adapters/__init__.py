"""Adapters move prompts and responses between the core and a model (DESIGN.md §11.2)."""

from __future__ import annotations

import json
import re


def extract_json(text: str) -> dict:
    """Pull one JSON object out of a model response: bare JSON, or the last fenced
    ```json block, or the outermost {...} span. Raises ValueError if none parses."""
    text = text.strip()
    candidates = [text]
    candidates += reversed(re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.S))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    last_error = None
    for c in candidates:
        try:
            value = json.loads(c)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f"no JSON object found ({last_error})")

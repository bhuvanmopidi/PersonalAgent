"""Turn a Tier-1 Intent into a concrete Action using the intent's template."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

from jarvis.models import Action, Intent
from jarvis.router.regex_router import IntentSpec

_FIELD = re.compile(r"\{(\w+)(?:\|(\w+))?\}")  # {slot} or {slot|url}


def render(template: str, slots: dict[str, Any]) -> str:
    def substitute(match: re.Match[str]) -> str:
        value = str(slots.get(match.group(1), ""))
        return quote_plus(value) if match.group(2) == "url" else value

    return _FIELD.sub(substitute, template)


def to_action(spec: IntentSpec, intent: Intent) -> Action:
    template = spec.action
    return Action(
        kind=template.kind,
        target=render(template.target, intent.args),
        args={
            k: render(v, intent.args) if isinstance(v, str) else v for k, v in template.args.items()
        },
        description=render(spec.description, intent.args),
    )

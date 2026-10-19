"""The LLM's menu: every intent from config/intents.yaml, with its slots and example phrasings.

Generated from the same file the regex router uses, so the LLM can only ever choose something
Jarvis can actually do. Rendered deterministically (sorted, no timestamps) so the prompt is
byte-identical between calls and Claude's prompt cache hits.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from jarvis.apps.media_time import _CLOCK, _UNIT_SECONDS
from jarvis.models import Intent
from jarvis.planner.schema import RouterDecision
from jarvis.router.regex_router import VAGUE, IntentSpec, normalize

_FIELD = re.compile(r"\{(\w+)(?:\|\w+)?\}")  # {slot} or {slot|url} in action templates

RULES = """You are the part of Jarvis that understands commands. Jarvis is a voice assistant that \
controls a Windows PC. You get one command, transcribed by speech recognition (expect small \
mishearings, e.g. "pulse" for "pause"), plus the recent turns. Choose exactly ONE decision:

- run_intent: the command matches an intent below. Set `intent` and fill `slots` using only that \
intent's slot names. Write numbers as digits ("ten" -> "10"). Keep names and search words in the \
user's own words, fixing obvious mishearings.
- answer: a question you can answer in one or two short sentences: what Jarvis can do (only from \
this list), simple facts, arithmetic, definitions. Never claim you did something.
- clarify: too vague to act on even with the recent turns (e.g. "open it" with nothing before). \
Ask one short question.
- unsupported: the user wants something not in the list (e.g. sending messages or email, \
shopping, controlling the Spotify app, editing documents, files outside the listed ones). Give a \
short reason. If the request is to delete, send, buy, book, pay, turn off or change settings, \
it is unsupported even when a similar-looking intent exists (never "open" something instead).

Rules: never invent intents or slot names. "it", "that", "again", "louder" refer to the recent \
turns. To play a video or music, use youtube_play. Prefer run_intent whenever an intent fits."""


@dataclass(frozen=True)
class IntentInfo:
    name: str
    description: str
    slot_docs: dict[str, str]
    slot_names: frozenset[str]
    required: frozenset[str]
    defaults: dict[str, str] = field(default_factory=dict)
    examples: tuple[str, ...] = ()


def _template_slots(spec: IntentSpec) -> set[str]:
    texts = [spec.action.target, *(v for v in spec.action.args.values() if isinstance(v, str))]
    return {m.group(1) for t in texts for m in _FIELD.finditer(t)}


def intent_info(spec: IntentSpec) -> IntentInfo:
    used = _template_slots(spec)
    pattern_slots = {name for p in spec.patterns for name in re.compile(p).groupindex}
    names = used | pattern_slots | set(spec.slots)
    return IntentInfo(
        name=spec.name,
        description=_FIELD.sub(lambda m: f"<{m.group(1)}>", spec.description) or spec.name,
        slot_docs=dict(spec.slots),
        slot_names=frozenset(names),
        required=frozenset(used - set(spec.defaults) - set(spec.optional)),
        defaults=dict(spec.defaults),
        examples=tuple(spec.examples),
    )


class Catalog:
    def __init__(self, specs: Iterable[IntentSpec]) -> None:
        self.intents = {s.name: intent_info(s) for s in specs if s.llm}

    def render(self) -> str:
        lines = ["Intents:"]
        for info in sorted(self.intents.values(), key=lambda i: i.name):
            lines.append(f"- {info.name}: {info.description}")
            for slot in sorted(info.slot_names):
                doc = info.slot_docs.get(slot, "")
                need = "required" if slot in info.required else "optional"
                lines.append(f"    slot {slot} ({need}){': ' + doc if doc else ''}")
            if info.examples:
                lines.append("    e.g. " + " | ".join(f'"{e}"' for e in info.examples))
        return "\n".join(lines)

    def system_prompt(self) -> str:
        return f"{RULES}\n\n{self.render()}"

    def to_intent(
        self, decision: RouterDecision, text: str = ""
    ) -> tuple[Intent | None, str | None]:
        """For run_intent: (Intent, None), or (None, what's wrong) so the model can retry.

        Beyond names, slot *values* are checked: numbers must be numbers, units real units that
        agree with the words used, choices from their list, and never a bare "it"/"that".
        """
        info = self.intents.get(decision.intent or "")
        if info is None:
            return None, f"'{decision.intent}' is not one of the listed intents."
        slots = decision.slot_dict()
        unknown = sorted(set(slots) - info.slot_names)
        if unknown:
            allowed = ", ".join(sorted(info.slot_names)) or "none"
            return None, f"{info.name} has no slot(s) {unknown}; its slots are: {allowed}."
        missing = sorted(info.required - set(slots))
        if missing:
            return None, f"{info.name} needs slot(s): {', '.join(missing)}."
        for slot, value in slots.items():
            problem = slot_problem(slot, value.strip(), text)
            if problem:
                return None, f"{info.name}: {problem}"
        problem = said_but_left_out(info, slots, text)
        if problem:
            return None, f"{info.name}: {problem}"
        args = {**info.defaults, **{k: v.strip() for k, v in slots.items()}}
        return Intent(name=info.name, args=args, source="llm"), None


# What each slot's value must look like â€” the same for every intent that uses the slot.
NUMBER_SLOTS = {"value", "amount", "rate"}
CHOICES = {
    "mode": {"always", "never", "ask"},
    "prefer": {"app", "website", "site", "auto"},
    "folder": {
        "downloads",
        "documents",
        "desktop",
        "pictures",
        "photos",
        "music",
        "videos",
        "home",
    },
}
_UNIT_WORDS = {
    "seconds": r"\b(?:s|secs?|seconds?)\b",
    "minutes": r"\b(?:m|mins?|minutes?)\b",
    "hours": r"\b(?:h|hrs?|hours?)\b",
}


def _unit_kind(unit: str) -> str | None:
    seconds = _UNIT_SECONDS.get(unit.lower())
    return {1: "seconds", 60: "minutes", 3600: "hours"}.get(seconds or 0)


def slot_problem(slot: str, value: str, text: str) -> str | None:
    """Why this slot value can't be right, or None. `text` is what the user said."""
    if normalize(value) in VAGUE:
        return (
            f"slot {slot} is only '{value}', which refers to something else. Use the recent turns "
            "to fill in what it is, or choose clarify."
        )
    if slot in NUMBER_SLOTS:
        try:
            float(value.rstrip("x%"))
        except ValueError:
            return f"slot {slot} must be a number written in digits, not '{value}'."
    if slot == "unit":
        kind = _unit_kind(value)
        if kind is None:
            return f"slot unit must be seconds, minutes or hours, not '{value}'."
        said = [k for k, pattern in _UNIT_WORDS.items() if re.search(pattern, text.lower())]
        if said and kind not in said:
            return f"the user said {' or '.join(said)}, but unit is '{value}'."
    if slot == "time" and not _CLOCK.match(value):
        return f"slot time must be a clock time like 5:30 or 1:02:03, not '{value}'."
    if slot in CHOICES and value.lower() not in CHOICES[slot]:
        return f"slot {slot} must be one of {sorted(CHOICES[slot])}, not '{value}'."
    return None


_NUMBER_WORDS = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|"
    r"thirty|forty|fifty|sixty|ninety|hundred|half|couple)\b"
)


def said_but_left_out(info: IntentInfo, slots: dict[str, str], text: str) -> str | None:
    """The user gave a number or a unit, but the model left the slot empty — the default
    (e.g. 10 seconds) would then silently replace what they said."""
    lowered = text.lower()
    if "unit" in info.slot_names and "unit" not in slots:
        said = [k for k, pattern in _UNIT_WORDS.items() if re.search(pattern, lowered)]
        if said:
            return f"the user said {said[0]}: set slot unit."
    left_out = "amount" in info.slot_names and "amount" not in slots and "time" not in slots
    if left_out and _NUMBER_WORDS.search(lowered):
        return "the user gave an amount: set slot amount (in digits)."
    return None


DEFAULT_TEXT = {
    "clarify": "Sorry, what should I do?",
    "unsupported": "I can't do that yet.",
}


def check(decision: RouterDecision) -> str | None:
    """Problems with a non-intent decision. An answer needs words; a question back or a "can't"
    without words gets a standard sentence instead of being thrown away."""
    if decision.kind == "run_intent" or (decision.text or "").strip():
        return None
    if decision.kind in DEFAULT_TEXT:
        decision.text = DEFAULT_TEXT[decision.kind]
        return None
    return f"a '{decision.kind}' decision needs `text`."

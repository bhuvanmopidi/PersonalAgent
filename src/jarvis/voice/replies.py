"""Understanding short spoken replies: yes/no for confirmations, done/cancel for handoffs, stop."""

from __future__ import annotations

import re

from jarvis.router.regex_router import normalize

_YES = re.compile(
    r"\b(?:yes|yeah|yep|yup|ya|sure|ok|okay|confirm(?:ed)?|correct|go ahead|do it|send it|"
    r"place (?:the )?order|pay|haan|ha)\b"
)
_NO = re.compile(
    r"\b(?:no|nope|nah|don'?t|do not|cancel|stop|wait|hold on|never ?mind|wrong|nahi)\b"
)
_DONE = re.compile(r"\b(?:done|finished|continue|go on|i'?m done|completed|ready|resume)\b")

STOP_PHRASES = {
    "stop", "stop it", "stop that", "stop now", "cancel", "cancel that", "cancel it", "abort",
    "never mind", "nevermind", "wait", "hold on", "shut up", "quiet", "be quiet", "stop talking",
}  # fmt: skip


def parse_yes_no(text: str) -> bool | None:
    """True / False, or None if unclear (both, or neither)."""
    t = normalize(text)
    yes, no = bool(_YES.search(t)), bool(_NO.search(t))
    if yes == no:
        return None
    return yes


def parse_handoff(text: str) -> bool | None:
    """True = user finished the step, False = cancel, None = unclear."""
    t = normalize(text)
    if _NO.search(t):
        return False
    return True if _DONE.search(t) else None


def is_stop(text: str) -> bool:
    """A whole utterance that means "stop what you're doing" (not "stop the music on spotify")."""
    return normalize(text) in STOP_PHRASES

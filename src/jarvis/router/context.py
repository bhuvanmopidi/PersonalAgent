"""Recent turns, so the LLM understands "do that again", "louder", "the other one"."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from jarvis.safety.redact import redact

MAX_TURNS = 5


@dataclass(frozen=True)
class Turn:
    said: str
    did: str  # "volume_set(value=30) → Volume 30%" or "answered: …"


class SessionContext:
    def __init__(self, max_turns: int = MAX_TURNS) -> None:
        self._turns: deque[Turn] = deque(maxlen=max_turns)

    def add(self, said: str, did: str) -> None:
        self._turns.append(Turn(redact(said)[:200], redact(did)[:200]))

    def render(self) -> str:
        if not self._turns:
            return "(none)"
        return "\n".join(f'- user: "{t.said}" -> {t.did}' for t in self._turns)

    def __len__(self) -> int:
        return len(self._turns)

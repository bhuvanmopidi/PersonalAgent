"""Skills, put together: the store, the learner and the replayer — and learning in the background
so the user hears the result without waiting for it."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from jarvis.telemetry.logging import get_logger
from jarvis.tiers.skills.learner import Learner
from jarvis.tiers.skills.replay import Replayer
from jarvis.tiers.skills.store import SkillStore
from jarvis.tiers.structured.agent import RunStep

log = get_logger(__name__)


class Skills:
    def __init__(self, store: SkillStore, learner: Learner, replayer: Replayer) -> None:
        self.store, self.learner, self.replayer = store, learner, replayer
        self._pending: set[asyncio.Task[None]] = set()

    def learn_later(self, goal: str, steps: Sequence[RunStep], where: str) -> None:
        task = asyncio.get_running_loop().create_task(self.learn(goal, steps, where))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def learn(self, goal: str, steps: Sequence[RunStep], where: str) -> None:
        try:
            skill = await self.learner.learn(goal, steps, where)
            if skill is not None:
                self.store.save(skill)
        except Exception:  # learning must never break anything
            log.warning("learning a skill failed", exc_info=True)

    async def settle(self) -> None:
        """Wait for background learning (tests, shutdown)."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

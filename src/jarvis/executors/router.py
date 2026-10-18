"""One Executor that sends each action kind to the executor that handles it.

ActionRunner still sees a single executor, so there is still exactly one path: policy → execute.
"""

from __future__ import annotations

from collections.abc import Iterable

from jarvis.executors.base import Executor
from jarvis.models import Action, ActionResult


class KindRouter:
    def __init__(self, default: Executor, routes: Iterable[tuple[Iterable[str], Executor]] = ()):
        self.default = default
        self._by_kind = {kind: executor for kinds, executor in routes for kind in kinds}

    def executor_for(self, kind: str) -> Executor:
        return self._by_kind.get(kind, self.default)

    async def execute(self, action: Action) -> ActionResult:
        return await self.executor_for(action.kind).execute(action)

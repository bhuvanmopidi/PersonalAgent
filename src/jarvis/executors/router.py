"""One Executor that sends each action kind to the executor that handles it.

ActionRunner still sees a single executor, so there is still exactly one path: policy → execute.
The general agent's actions name their surface (`args["surface"]`, e.g. "desktop"): the same kinds
(click, type_text, …) go to the browser or to the desktop executor accordingly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from jarvis.executors.base import Executor
from jarvis.models import Action, ActionResult


class KindRouter:
    def __init__(
        self,
        default: Executor,
        routes: Iterable[tuple[Iterable[str], Executor]] = (),
        surfaces: Mapping[str, Executor] | None = None,
    ):
        self.default = default
        self._by_kind = {kind: executor for kinds, executor in routes for kind in kinds}
        self._by_surface = dict(surfaces or {})

    def executor_for(self, kind: str, surface: str | None = None) -> Executor:
        if surface is not None and surface in self._by_surface:
            return self._by_surface[surface]
        return self._by_kind.get(kind, self.default)

    async def execute(self, action: Action) -> ActionResult:
        surface = action.args.get("surface")
        return await self.executor_for(action.kind, str(surface) if surface else None).execute(
            action
        )

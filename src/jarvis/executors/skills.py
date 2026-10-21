"""'what skills do you have' / 'forget the spotify skill' — managing learned skills by voice.

Through ActionRunner like every other action (both kinds are GREEN: they only touch Jarvis's own
learned recipes, and a forgotten skill is simply learned again next time)."""

from __future__ import annotations

from jarvis.models import Action, ActionResult
from jarvis.tiers.skills.store import SkillStore, spoken

SKILL_KINDS = frozenset({"list_skills", "forget_skill"})


class SkillsExecutor:
    def __init__(self, store: SkillStore) -> None:
        self.store = store

    async def execute(self, action: Action) -> ActionResult:
        if action.kind == "list_skills":
            skills = self.store.all()
            if not skills:
                return ActionResult(ok=True, observation="I haven't learned any skills yet.")
            names = "; ".join(spoken(s.name) for s in skills[:10])
            more = f", and {len(skills) - 10} more" if len(skills) > 10 else ""
            return ActionResult(ok=True, observation=f"I know {len(skills)}: {names}{more}.")
        words = action.target.strip()
        found = self.store.find(words) if words else []
        if not found:
            return ActionResult(ok=False, error="no_skill",
                                observation=f'I have no skill matching "{words}".')  # fmt: skip
        if len(found) > 1:
            names = "; ".join(spoken(s.name) for s in found[:5])
            return ActionResult(
                ok=False,
                error="ambiguous",
                observation=f"Several skills match: {names}. Which one?",
            )
        self.store.remove(found[0].id)
        return ActionResult(ok=True, observation=f"Forgot how to {spoken(found[0].name)}.")

"""Resolve "open X" / "close X" to something concrete, then launch or close it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

import psutil
import yaml
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from jarvis.config import CONFIG_DIR
from jarvis.telemetry.logging import get_logger
from jarvis.tiers.instant.app_index import AppEntry, AppIndex, Quality, best_match

log = get_logger(__name__)

ALIASES_PATH = CONFIG_DIR / "apps" / "aliases.yaml"
REFRESH_COOLDOWN_S = 300
_DOMAIN = re.compile(r"^(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?$")
_RANK: dict[Quality, int] = {"exact": 0, "prefix": 1, "fuzzy": 2}

# Never closed by the agent: Windows itself, and anything Jarvis runs inside.
_PROTECTED = {
    "explorer", "csrss", "winlogon", "wininit", "lsass", "services", "svchost",
    "dwm", "smss", "system", "fontdrvhost", "sihost",
}  # fmt: skip


class AppAlias(BaseModel):
    name: str | None = None  # Start-menu name to look up
    process: str | None = None  # exe name without .exe, used for closing
    uri: str | None = None  # e.g. ms-settings:
    command: str | None = None  # exe on PATH


class AliasConfig(BaseModel):
    apps: dict[str, AppAlias] = Field(default_factory=dict)
    sites: dict[str, str] = Field(default_factory=dict)
    # sites that open in the browser even if a same-named app is installed (e.g. GitHub Desktop)
    prefer_web: list[str] = Field(default_factory=list)


Prefer = Literal["auto", "app", "web"]
_PREFER = {"app": "app", "website": "web", "site": "web", "web": "web"}


def parse_prefer(value: str) -> Prefer:
    """'app' / 'website' / 'site' from the command → a resolve preference; anything else = auto."""
    return cast(Prefer, _PREFER.get(value.lower().strip(), "auto"))


def load_aliases(path: Path = ALIASES_PATH) -> AliasConfig:
    if not path.exists():
        return AliasConfig()
    return AliasConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


class LaunchPlan(BaseModel):
    kind: Literal["app", "uri", "command", "url"]
    value: str  # AppID, URI, exe path or URL
    display: str  # what to tell the user


class AppLauncher:
    def __init__(
        self,
        aliases: AliasConfig,
        index_loader: Callable[..., AppIndex] = AppIndex.load,
        opener: Callable[[str], None] = os.startfile,
    ) -> None:
        self.aliases = aliases
        self._index_loader = index_loader
        self._index: AppIndex | None = None
        self._last_refresh = float("-inf")
        self._open = opener

    @property
    def index(self) -> AppIndex:
        if self._index is None:
            self._index = self._index_loader()
        return self._index

    def prewarm(self) -> None:
        _ = self.index

    def resolve(self, query: str, prefer: Prefer = "auto") -> LaunchPlan | None:
        """Decide what "open <query>" means: an installed app, a website, a URI or an exe.

        prefer="web" (user said "website"/"site") → websites only.
        prefer="app" (user said "app")            → apps only.
        prefer="auto": a typed domain wins; then app/command aliases; then the better match,
        best first: exact app · exact site · prefix app · prefix site · fuzzy app · fuzzy site
        (so an installed WhatsApp beats web.whatsapp.com, but "insta" still finds the site).
        Sites listed in prefer_web win over same-named apps.
        """
        q = query.lower().strip()
        if not q:
            return None
        if _DOMAIN.match(q) and prefer != "app":
            return LaunchPlan(
                kind="url", value=q if q.startswith("http") else f"https://{q}", display=q
            )

        alias = self.aliases.apps.get(q)
        if prefer != "web" and alias and alias.uri:
            return LaunchPlan(kind="uri", value=alias.uri, display=q)
        if prefer != "web" and alias and alias.command:
            return LaunchPlan(
                kind="command", value=shutil.which(alias.command) or alias.command, display=q
            )

        site = None if prefer == "app" else self._find_site(q)
        prefers_web = site is not None and site[1] == "exact" and site[0] in self.aliases.prefer_web
        if site and (prefer == "web" or prefers_web):
            return self._site_plan(site[0])
        if prefer == "web":
            return None

        name = alias.name if alias and alias.name else q
        app = self._find_app(name, retry=site is None)
        if app and app[0].is_web_app:
            # a browser web app is a website: open its site in the Jarvis Chrome window, never the
            # installed app (it runs in your normal profile, which Jarvis can't control)
            web = self._find_site(app[0].name.lower())
            if web:
                return self._site_plan(web[0])
            log.warning("web app %r has no address in aliases.yaml sites", app[0].name)
            app = None
        candidates: list[tuple[int, LaunchPlan]] = []
        if app:
            entry, quality = app
            plan = LaunchPlan(kind="app", value=entry.app_id, display=entry.name)
            candidates.append((_RANK[quality] * 2, plan))
        if site:
            candidates.append((_RANK[site[1]] * 2 + 1, self._site_plan(site[0])))
        if candidates:
            return min(candidates, key=lambda c: c[0])[1]

        exe = shutil.which(name)
        return LaunchPlan(kind="command", value=exe, display=name) if exe else None

    def _find_site(self, q: str) -> tuple[str, Quality] | None:
        # whole-string similarity only: "instagarm" → instagram, but "xbox" must not hit "x"
        return best_match(q, list(self.aliases.sites), scorer=fuzz.ratio)

    def _site_plan(self, name: str) -> LaunchPlan:
        return LaunchPlan(kind="url", value=self.aliases.sites[name], display=name)

    def _find_app(self, name: str, retry: bool) -> tuple[AppEntry, Quality] | None:
        found = self.index.find_scored(name)
        cooled = time.monotonic() - self._last_refresh > REFRESH_COOLDOWN_S
        if found is None and retry and cooled:
            # maybe installed after the cache was built (~2 s, so at most every few minutes)
            self._last_refresh = time.monotonic()
            self._index = self._index_loader(refresh=True)
            found = self._index.find_scored(name)
        return found

    def launch(self, plan: LaunchPlan) -> None:
        # ShellExecute handles all four: shell:AppsFolder IDs, URIs, exes (incl. UAC), URLs.
        self._open(f"shell:AppsFolder\\{plan.value}" if plan.kind == "app" else plan.value)

    def close(self, query: str) -> list[str]:
        """Gracefully close the app (WM_CLOSE, like clicking X). Returns the exe names closed."""
        q = query.lower().strip()
        alias = self.aliases.apps.get(q)
        target = (alias.process if alias and alias.process else q).lower().replace(" ", "")
        if target in _PROTECTED:
            raise PermissionError(f"'{query}' is protected and won't be closed")

        protected_pids = _self_and_ancestors()
        pids, names = [], set()
        for proc in psutil.process_iter(["pid", "name"]):
            name = (proc.info["name"] or "").lower().removesuffix(".exe")
            if name == target:
                if proc.info["pid"] in protected_pids:
                    raise PermissionError(f"'{query}' is running Jarvis itself")
                pids.append(proc.info["pid"])
                names.add(proc.info["name"])
        if not pids:
            return []

        args = ["taskkill"] + [a for pid in pids for a in ("/PID", str(pid))]  # no /F: graceful
        out = subprocess.run(args, capture_output=True, text=True, timeout=10).stdout
        return sorted(names) if "SUCCESS" in out else []


def _self_and_ancestors() -> set[int]:
    me = psutil.Process()
    return {me.pid} | {p.pid for p in me.parents()}

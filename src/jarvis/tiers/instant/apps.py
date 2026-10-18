"""Resolve "open X" / "close X" to something concrete, then launch or close it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import psutil
import yaml
from pydantic import BaseModel, Field

from jarvis.config import CONFIG_DIR
from jarvis.tiers.instant.app_index import AppIndex

ALIASES_PATH = CONFIG_DIR / "apps" / "aliases.yaml"
REFRESH_COOLDOWN_S = 300
_DOMAIN = re.compile(r"^(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?$")

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

    def resolve(self, query: str) -> LaunchPlan | None:
        """alias → website alias → installed app → exe on PATH → domain → not found."""
        q = query.lower().strip()
        alias = self.aliases.apps.get(q)
        if alias and alias.uri:
            return LaunchPlan(kind="uri", value=alias.uri, display=q)
        if alias and alias.command:
            path = shutil.which(alias.command) or alias.command
            return LaunchPlan(kind="command", value=path, display=q)
        if q in self.aliases.sites:
            return LaunchPlan(kind="url", value=self.aliases.sites[q], display=q)

        name = alias.name if alias and alias.name else q
        entry = self.index.find(name)
        if entry is None and time.monotonic() - self._last_refresh > REFRESH_COOLDOWN_S:
            # maybe installed after the cache was built (~2 s, so at most every few minutes)
            self._last_refresh = time.monotonic()
            self._index = self._index_loader(refresh=True)
            entry = self._index.find(name)
        if entry:
            return LaunchPlan(kind="app", value=entry.app_id, display=entry.name)

        exe = shutil.which(name)
        if exe:
            return LaunchPlan(kind="command", value=exe, display=name)
        if _DOMAIN.match(q):
            url = q if q.startswith("http") else f"https://{q}"
            return LaunchPlan(kind="url", value=url, display=q)
        return None

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

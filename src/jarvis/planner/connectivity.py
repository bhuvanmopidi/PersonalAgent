"""Are we online? A quick TCP check to the Claude API, cached so it never slows commands down."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Callable

HOST, PORT = "api.anthropic.com", 443
CHECK_TIMEOUT_S = 1.0
CACHE_S = 30.0  # re-check at most this often
OFFLINE_AFTER_ERROR_S = 60.0  # after a failed Claude call, don't retry the network for a minute


def tcp_reachable(host: str = HOST, port: int = PORT, timeout: float = CHECK_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Connectivity:
    def __init__(
        self,
        probe: Callable[[], bool] = tcp_reachable,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe, self._clock = probe, clock
        self._online: bool | None = None
        self._checked_at = float("-inf")
        self._offline_until = float("-inf")

    async def online(self) -> bool:
        now = self._clock()
        if now < self._offline_until:
            return False
        if self._online is None or now - self._checked_at > CACHE_S:
            self._online = await asyncio.to_thread(self._probe)
            self._checked_at = self._clock()
        return self._online

    def mark_offline(self) -> None:
        """A Claude call failed to connect: treat as offline for a while (no repeated waits)."""
        self._online = False
        self._offline_until = self._clock() + OFFLINE_AFTER_ERROR_S

"""System controls: volume (Core Audio), brightness (WMI / DDC-CI), lock screen."""

from __future__ import annotations

import ctypes
import warnings
from typing import Any


def _clamp(value: int) -> int:
    return max(0, min(100, value))


class Volume:
    """Default speakers via pycaw. Calls take a few ms, so they run inline."""

    def _endpoint(self) -> Any:
        from pycaw.pycaw import AudioUtilities

        return AudioUtilities.GetSpeakers().EndpointVolume

    def get(self) -> int:
        return round(float(self._endpoint().GetMasterVolumeLevelScalar()) * 100)

    def set(self, percent: int) -> None:
        self._endpoint().SetMasterVolumeLevelScalar(_clamp(percent) / 100, None)

    def set_mute(self, muted: bool) -> None:
        self._endpoint().SetMute(int(muted), None)


class Brightness:
    """Built-in laptop panel (WMI) and DDC/CI monitors. Fails on monitors without DDC/CI."""

    def _sbc(self) -> Any:
        with warnings.catch_warnings():  # the wmi package emits SyntaxWarnings on import
            warnings.simplefilter("ignore")
            import screen_brightness_control

        return screen_brightness_control

    def get(self) -> int:
        return int(self._sbc().get_brightness(display=0)[0])

    def set(self, percent: int) -> None:
        self._sbc().set_brightness(_clamp(percent), display=0)


def lock_screen() -> None:
    if not ctypes.windll.user32.LockWorkStation():
        raise OSError("LockWorkStation failed")

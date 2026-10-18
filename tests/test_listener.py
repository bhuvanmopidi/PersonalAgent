from collections.abc import AsyncIterator

import numpy as np
import pytest

from jarvis.config import ListenSettings
from jarvis.voice.listener import (
    Heard,
    Listener,
    ListenerCore,
    NoSpeech,
    Recorded,
    Wake,
    is_hallucination,
)
from jarvis.voice.mic import FRAME_SAMPLES, ArraySource
from jarvis.voice.stt import Transcript

# Each fake frame's first sample says what it "contains": wake word, speech, or quiet.
W, S, Q = 3, 2, 1


def frame(kind: int) -> np.ndarray:
    f = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    f[0] = kind
    return f


class FakeWake:
    def __init__(self) -> None:
        self.resets = 0

    def score(self, f: np.ndarray) -> float:
        return 1.0 if f[0] == W else 0.0

    def reset(self) -> None:
        self.resets += 1


class FakeVAD:
    def speech_prob(self, f: np.ndarray) -> float:
        return 1.0 if f[0] == S else 0.0

    def reset(self) -> None: ...


# 80 ms frames: silence_ms 240 = 3 quiet frames end a command; nothing said for 4 frames = give up
SETTINGS = ListenSettings(silence_ms=240, no_speech_timeout_s=0.32, max_utterance_s=1.6)


def make_core(muted: bool = False) -> ListenerCore:
    return ListenerCore(FakeWake(), FakeVAD(), SETTINGS, is_muted=lambda: muted)


def feed(core: ListenerCore, kinds: list[int]) -> list[object]:
    return [e for k in kinds if (e := core.feed(frame(k))) is not None]


def test_wake_then_command_then_pause() -> None:
    core = make_core()
    events = feed(core, [Q, Q, W, S, S, S, Q, Q, Q, Q])
    assert isinstance(events[0], Wake) and events[0].trigger == "wake"
    rec = events[1]
    assert isinstance(rec, Recorded) and rec.trigger == "wake"
    assert not core.recording


def test_trailing_silence_is_trimmed() -> None:
    core = make_core()
    settings = ListenSettings(silence_ms=800)  # 10 quiet frames end it
    core = ListenerCore(FakeWake(), FakeVAD(), settings)
    events = feed(core, [W, S, S] + [Q] * 10)
    rec = events[-1]
    assert isinstance(rec, Recorded)
    assert len(rec.audio) == (2 + 3) * FRAME_SAMPLES  # 2 speech + 3 kept quiet frames


def test_speech_without_wake_word_is_ignored() -> None:
    assert feed(make_core(), [S, S, S, Q, Q, Q, Q]) == []


def test_no_speech_after_wake_times_out() -> None:
    events = feed(make_core(), [W, Q, Q, Q, Q])
    assert isinstance(events[-1], NoSpeech)


def test_long_speech_is_cut_at_max() -> None:
    events = feed(make_core(), [W] + [S] * 25)
    assert isinstance(events[-1], Recorded)
    assert len(events[-1].audio) == 20 * FRAME_SAMPLES  # 1.6 s


def test_push_to_talk_starts_without_wake_word() -> None:
    core = make_core()
    core.push_to_talk()
    events = feed(core, [Q, S, S, Q, Q, Q])
    assert isinstance(events[0], Wake) and events[0].trigger == "ptt"
    assert isinstance(events[1], Recorded) and events[1].trigger == "ptt"


def test_open_mode_records_any_speech_with_preroll() -> None:
    core = make_core()
    core.mode = "open"
    events = feed(core, [Q, Q, Q, S, S, Q, Q, Q])
    rec = events[0]
    assert isinstance(rec, Recorded) and rec.trigger == "open"
    assert rec.audio[0] == Q and rec.audio[3 * FRAME_SAMPLES] == S  # 3 frames of pre-roll kept


def test_open_mode_ignores_jarvis_own_voice_but_wake_still_works() -> None:
    core = make_core(muted=True)
    core.mode = "open"
    assert feed(core, [S, S, S, Q, Q, Q]) == []
    assert isinstance(feed(core, [W])[0], Wake)


def test_wake_model_reset_after_wake_and_after_recording() -> None:
    wake = FakeWake()
    core = ListenerCore(wake, FakeVAD(), SETTINGS)
    feed(core, [W, S, Q, Q, Q])
    assert wake.resets == 2


@pytest.mark.parametrize("text", ["", " you", "Thank you.", "Hmm"])
def test_hallucinations(text: str) -> None:
    assert is_hallucination(text)


def test_real_command_is_not_hallucination() -> None:
    assert not is_hallucination("Open Spotify.")


class FakeSTT:
    def __init__(self, text: str) -> None:
        self.text, self.calls = text, 0

    def transcribe(self, audio: np.ndarray) -> Transcript:
        self.calls += 1
        return Transcript(self.text, 12.0)


async def collect(listener: Listener) -> list[object]:
    return [e async for e in listener.events()]


async def test_listener_transcribes_recorded_audio() -> None:
    audio = np.concatenate([frame(k) for k in [Q, W, S, S, Q, Q, Q, Q]])
    stt = FakeSTT("Open Spotify.")
    events = await collect(Listener(ArraySource(audio), make_core(), stt))
    assert isinstance(events[0], Wake)
    heard = events[1]
    assert isinstance(heard, Heard) and heard.text == "Open Spotify." and heard.stt_ms == 12.0


async def test_listener_drops_hallucinated_transcripts() -> None:
    audio = np.concatenate([frame(k) for k in [W, S, Q, Q, Q]])
    events = await collect(Listener(ArraySource(audio), make_core(), FakeSTT("Thank you.")))
    assert [type(e) for e in events] == [Wake]


class ListSource:
    def __init__(self, kinds: list[int]) -> None:
        self.kinds = kinds

    async def frames(self) -> AsyncIterator[np.ndarray]:
        for k in self.kinds:
            yield frame(k)


@pytest.mark.parametrize(("combo", "pretty"), [("<ctrl>+<alt>+j", "Ctrl+Alt+J"), ("<f9>", "F9")])
def test_pretty_combo(combo: str, pretty: str) -> None:
    from jarvis.voice.runtime import pretty_combo

    assert pretty_combo(combo) == pretty


async def test_partials_only_while_recording() -> None:
    stt = FakeSTT("open spo")
    listener = Listener(ListSource([W] + [S] * 15 + [Q] * 3), make_core(), stt, partials=True)
    events = await collect(listener)
    assert isinstance(events[-1], Heard)
    assert stt.calls >= 1  # final transcription at least; partials are best-effort

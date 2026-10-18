import asyncio

from fakes_voice import FakeEngine, FakeSink

from jarvis.voice.speaker import Speaker


async def test_say_plays_all_chunks() -> None:
    engine, sink = FakeEngine(chunks=3), FakeSink()
    speaker = Speaker(engine, sink)
    await speaker.say("Opening Spotify", wait=True)
    assert engine.texts == ["Opening Spotify"] and sink.played == [3]
    assert speaker.last_first_audio_ms is not None


async def test_say_returns_before_speech_finishes() -> None:
    speaker = Speaker(FakeEngine(), FakeSink(seconds_per_chunk=0.2))
    loop = asyncio.get_running_loop()
    start = loop.time()
    await speaker.say("Opening Spotify")
    assert loop.time() - start < 0.1  # actions don't wait for speech
    await speaker.wait()


async def test_new_speech_replaces_old() -> None:
    engine, sink = FakeEngine(chunks=5), FakeSink(seconds_per_chunk=0.1)
    speaker = Speaker(engine, sink)
    await speaker.say("Opening blahblah")
    await asyncio.sleep(0.05)
    await speaker.say("I couldn't find blahblah", wait=True)
    await asyncio.sleep(0.05)
    assert sink.stopped_early == 1 and sink.played[-1] == 5


async def test_stop() -> None:
    sink = FakeSink(seconds_per_chunk=0.2)
    speaker = Speaker(FakeEngine(chunks=10), sink)
    await speaker.say("a long sentence")
    await asyncio.sleep(0.05)
    speaker.stop()
    await speaker.wait()
    assert sink.stopped_early == 1


async def test_short_phrases_are_cached() -> None:
    engine, sink = FakeEngine(), FakeSink()
    speaker = Speaker(engine, sink)
    await speaker.say("Volume up", wait=True)
    await speaker.say("Volume up", wait=True)
    assert engine.texts == ["Volume up"] and sink.played == [2, 2]
    assert speaker.last_first_audio_ms == 0.0


async def test_long_text_not_cached() -> None:
    engine = FakeEngine()
    speaker = Speaker(engine, FakeSink())
    text = "This is a much longer sentence that should not be kept in the phrase cache at all."
    await speaker.say(text, wait=True)
    await speaker.say(text, wait=True)
    assert len(engine.texts) == 2


async def test_secrets_are_never_spoken() -> None:
    engine = FakeEngine()
    await Speaker(engine, FakeSink()).say("your OTP is 482913", wait=True)
    assert engine.texts == ["your OTP is [REDACTED]"]


async def test_disabled_and_empty_are_noops() -> None:
    sink = FakeSink()
    await Speaker(None, sink).say("hello", wait=True)
    await Speaker(FakeEngine(), sink).say("   ", wait=True)
    assert sink.played == []


async def test_audio_failure_never_raises() -> None:
    speaker = Speaker(FakeEngine(), FakeSink(fail=True))
    await speaker.say("hello", wait=True)  # logged, not raised

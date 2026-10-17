import pytest

from jarvis import __version__
from jarvis.main import cli


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli(["--version"])
    assert __version__ in capsys.readouterr().out


def test_run_starts(capsys: pytest.CaptureFixture[str]) -> None:
    cli(["run"])
    assert "starting" in capsys.readouterr().err

import pytest

from jarvis import __version__
from jarvis.main import cli


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli(["--version"])
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize("command", ["run", "listen-test", "do", "repl"])
def test_subcommands_have_help(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    # `run` itself opens the mic and loads models — covered by the desktop tests instead
    with pytest.raises(SystemExit):
        cli([command, "--help"])
    assert "usage" in capsys.readouterr().out

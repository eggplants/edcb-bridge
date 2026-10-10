from __future__ import annotations

import pytest

from edcb_bridge import __version__, cli
from edcb_bridge.config import Config


def test_version_is_available():
    assert __version__


def test_version_flag_prints_the_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_defaults_match_config(monkeypatch):
    for name in Config.__dataclass_fields__:
        monkeypatch.delenv(name.upper(), raising=False)
    assert cli.build_config(cli.parse_args([])) == Config()


def test_environment_and_arguments(monkeypatch):
    monkeypatch.setenv("RECORDER", "mirakc")
    monkeypatch.setenv("LISTEN_PORT", "14510")
    monkeypatch.setenv("MIRAKC_URL", "http://mirakc:40772/")
    config = cli.build_config(cli.parse_args(["--listen-port", "4511", "--recorded-dir", "/rec/"]))
    assert config.recorder == "mirakc"
    assert config.listen_port == 4511
    assert config.mirakc_url == "http://mirakc:40772"
    assert config.recorded_dir == "/rec"


def test_unknown_recorder_is_rejected(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["--recorder", "edcb"])
    assert "invalid choice" in capsys.readouterr().err
    with pytest.raises(ValueError, match="recorder"):
        Config(recorder="edcb")  # ty: ignore[invalid-argument-type]


def test_main_serves_the_parsed_config(monkeypatch):
    served: list[Config] = []

    async def fake_serve(config):
        served.append(config)

    monkeypatch.setattr(cli, "serve", fake_serve)
    cli.main(["--listen-port", "4599", "--log-level", "DEBUG"])
    assert served[0].listen_port == 4599

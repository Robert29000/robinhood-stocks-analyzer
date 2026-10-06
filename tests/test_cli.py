from pathlib import Path
from types import SimpleNamespace

import pytest

from stock_activity import cli


def test_collect_and_process_run_sequentially(monkeypatch, tmp_path, capsys):
    settings = SimpleNamespace(output_dir=tmp_path / "output")
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda path: settings)
    monkeypatch.setattr(
        cli, "collect", lambda received: calls.append(("collect", received)) or tmp_path / "collection.json"
    )
    monkeypatch.setattr(
        cli, "process", lambda received: calls.append(("process", received)) or [Path("one"), Path("two")]
    )

    assert cli.main(["collect", "process", "--config", "example.toml"]) == 0
    assert calls == [("collect", settings), ("process", settings)]
    assert "collected:" in capsys.readouterr().out


@pytest.mark.parametrize("action", ["collect", "process"])
def test_single_action_remains_supported(action, monkeypatch, tmp_path):
    settings = SimpleNamespace(output_dir=tmp_path / "output")
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda path: settings)
    monkeypatch.setattr(cli, "collect", lambda received: calls.append("collect") or tmp_path / "collection.json")
    monkeypatch.setattr(cli, "process", lambda received: calls.append("process") or [])

    assert cli.main([action]) == 0
    assert calls == [action]


def test_processing_does_not_run_when_collection_fails(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda path: object())
    monkeypatch.setattr(cli, "collect", lambda settings: (_ for _ in ()).throw(ValueError("failed")))
    monkeypatch.setattr(cli, "process", lambda settings: pytest.fail("process should not run"))

    assert cli.main(["collect", "process"]) == 1


@pytest.mark.parametrize("actions", [["collect", "collect"], ["process", "collect"]])
def test_invalid_action_sequences_are_rejected(actions):
    with pytest.raises(SystemExit) as error:
        cli.main(actions)
    assert error.value.code == 2

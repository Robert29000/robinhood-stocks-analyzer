from pathlib import Path
from types import SimpleNamespace

import pytest

from stock_activity import cli


def test_collect_and_process_run_sequentially(monkeypatch, tmp_path, capsys):
    settings = SimpleNamespace(output_dir=tmp_path / "output")
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda path: settings)
    monkeypatch.setattr(
        cli, "collect",
        lambda received, **kwargs: calls.append(("collect", received)) or tmp_path / "collection.json",
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
    monkeypatch.setattr(
        cli, "collect", lambda received, **kwargs: calls.append("collect") or tmp_path / "collection.json"
    )
    monkeypatch.setattr(cli, "process", lambda received: calls.append("process") or [])

    assert cli.main([action]) == 0
    assert calls == [action]


def test_processing_does_not_run_when_collection_fails(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda path: object())
    monkeypatch.setattr(
        cli, "collect", lambda settings, **kwargs: (_ for _ in ()).throw(ValueError("failed"))
    )
    monkeypatch.setattr(cli, "process", lambda settings: pytest.fail("process should not run"))

    assert cli.main(["collect", "process"]) == 1


@pytest.mark.parametrize("actions", [["collect", "collect"], ["process", "collect"]])
def test_invalid_action_sequences_are_rejected(actions):
    with pytest.raises(SystemExit) as error:
        cli.main(actions)
    assert error.value.code == 2


def test_collect_start_step_and_progress_are_passed_to_collector(monkeypatch, tmp_path):
    settings = SimpleNamespace(output_dir=tmp_path / "output")
    received = {}
    monkeypatch.setattr(cli, "load_config", lambda path: settings)

    def fake_collect(received_settings, **kwargs):
        received.update(kwargs)
        kwargs["progress"]("Chain logs", "activity for AAPL (1/1)")
        return tmp_path / "collection.json"

    monkeypatch.setattr(cli, "collect", fake_collect)

    assert cli.main(["collect", "--from", "logs"]) == 0
    assert received["start_from"] == "logs"
    assert received["event_source"] == "rpc"
    assert callable(received["progress"])


def test_event_source_is_passed_to_collector(monkeypatch, tmp_path):
    settings = SimpleNamespace(output_dir=tmp_path / "output")
    received = {}
    monkeypatch.setattr(cli, "load_config", lambda path: settings)
    monkeypatch.setattr(
        cli,
        "collect",
        lambda received_settings, **kwargs: received.update(kwargs) or tmp_path / "collection.json",
    )

    assert cli.main(["collect", "--event-source", "explorer"]) == 0
    assert received["event_source"] == "explorer"


def test_non_interactive_visualizer_prints_only_phase_changes():
    class Stream:
        def __init__(self):
            self.output = ""

        def isatty(self):
            return False

        def write(self, value):
            self.output += value

        def flush(self):
            pass

    stream = Stream()
    visualizer = cli.CollectVisualizer(stream)
    visualizer.update("Chain logs", "multiplier updates for AAPL (1/2)")
    visualizer.update("Chain logs", "multiplier updates for MSFT (2/2)")
    visualizer.update("Saving", "collection.json")

    assert stream.output.count("Chain logs") == 1
    assert "Saving" in stream.output

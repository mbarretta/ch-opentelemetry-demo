import json

import pytest

from scripts import demo


def test_both_backends_receive_intended_signals_without_secrets_in_config():
    env = {
        "CLICKSTACK_OTLP_ENDPOINT": "https://click.test",
        "CLICKSTACK_API_KEY": "sensitive-click",
        "LANGFUSE_BASE_URL": "https://lf.test",
        "LANGFUSE_PUBLIC_KEY": "pk-test",
        "LANGFUSE_SECRET_KEY": "sensitive-lf",
    }
    config = demo.collector_config(env)
    pipelines = config["service"]["pipelines"]
    for signal in ("traces", "metrics", "logs"):
        assert "otlp_http/clickstack" in pipelines[signal]["exporters"]
    assert pipelines["traces/langfuse"]["exporters"] == ["otlp_http/langfuse"]
    assert "sensitive" not in json.dumps(config)
    filters = config["processors"]["filter/agent_services"]["traces"]["span"]
    assert '"chatbot"' in filters[0] and '"mcp"' in filters[0]


def test_incomplete_langfuse_config_rejected():
    with pytest.raises(SystemExit):
        demo.collector_config({"LANGFUSE_BASE_URL": "https://lf.test"})


def test_capture_mode_needs_no_backends():
    config = demo.collector_config({})
    assert "traces/langfuse" not in config["service"]["pipelines"]
    assert "file/capture" in config["service"]["pipelines"]["traces"]["exporters"]


def test_fault_targets_only_selected_product(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "RUNTIME", tmp_path)
    (tmp_path / "flagd").mkdir()
    original = json.loads((demo.UPSTREAM / "src/flagd/demo.flagd.json").read_text())
    path = tmp_path / "flagd/demo.flagd.json"
    path.write_text(json.dumps(original))
    demo.scenario("backend-failure")
    changed = json.loads(path.read_text())
    fault = changed["flags"]["productCatalogFailure"]
    assert fault["targeting"]["if"][1] == "on"
    assert fault["targeting"]["if"][2] == "off"
    demo.scenario("shopping")
    assert json.loads(path.read_text()) == original

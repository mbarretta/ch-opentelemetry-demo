import httpx

from concierge.langfuse_api import LangfuseAPI


async def test_managed_prompt_is_cached_and_versioned(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    api = LangfuseAPI()
    calls = []

    async def request(*args, **kwargs):
        calls.append(args)
        return {"type": "text", "prompt": "Managed prompt", "version": 7}

    api.request = request
    prompt = await api.prompt()
    assert prompt.managed and prompt.version == 7
    assert (await api.prompt()) is prompt
    assert len(calls) == 1


async def test_prompt_outage_uses_explicit_bundled_fallback(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    api = LangfuseAPI()

    async def request(*args, **kwargs):
        raise httpx.ConnectError("unavailable")

    api.request = request
    prompt = await api.prompt()
    assert not prompt.managed
    assert prompt.version == 1
    assert "budget" in prompt.text


async def test_project_id_matches_by_name_and_is_cached(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    api = LangfuseAPI()
    calls = []

    async def request(*args, **kwargs):
        calls.append(args)
        return {"data": [{"id": "cmu34ursa02asad0g80z409rh", "name": "otel-demo"}]}

    api.request = request
    assert (await api.project_id("otel-demo")) == "cmu34ursa02asad0g80z409rh"
    assert (await api.project_id("otel-demo")) == "cmu34ursa02asad0g80z409rh"
    assert len(calls) == 1


async def test_project_id_falls_back_to_the_keys_sole_project(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    api = LangfuseAPI()

    async def request(*args, **kwargs):
        return {"data": [{"id": "cmu34ursa02asad0g80z409rh", "name": "some-other-name"}]}

    api.request = request
    assert (await api.project_id("otel-demo")) == "cmu34ursa02asad0g80z409rh"


async def test_project_id_returns_none_without_a_name_match(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    api = LangfuseAPI()

    async def request(*args, **kwargs):
        return {"data": [{"id": "a", "name": "one"}, {"id": "b", "name": "two"}]}

    api.request = request
    assert (await api.project_id("otel-demo")) is None


async def test_project_id_outage_returns_none(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    api = LangfuseAPI()

    async def request(*args, **kwargs):
        raise httpx.ConnectError("unavailable")

    api.request = request
    assert (await api.project_id("otel-demo")) is None


async def test_project_id_without_a_name_or_disabled_client_skips_the_call():
    api = LangfuseAPI()
    assert (await api.project_id("")) is None
    assert (await api.project_id("otel-demo")) is None

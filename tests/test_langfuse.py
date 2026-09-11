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

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

try:
    from opentelemetry.instrumentation.utils import suppress_instrumentation
except ModuleNotFoundError:
    from contextlib import nullcontext as suppress_instrumentation

logger = logging.getLogger(__name__)
PROMPT_NAME = "astronomy-concierge"
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


@dataclass
class Prompt:
    text: str
    version: int
    managed: bool = False


class LangfuseAPI:
    def __init__(self):
        self.url = os.getenv("LANGFUSE_BASE_URL", "").rstrip("/")
        self.public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        self.secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
        self.label = os.getenv("LANGFUSE_PROMPT_LABEL", "production")
        self.cache = None
        self.expires = 0
        self.project_id_cache = None
        self.project_id_expires = 0

    @property
    def enabled(self):
        return bool(self.url and self.public_key and self.secret_key)

    async def request(self, method, path, **kwargs):
        with suppress_instrumentation():
            async with httpx.AsyncClient(
                base_url=self.url,
                auth=(self.public_key, self.secret_key),
                timeout=5,
            ) as client:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()

    async def prompt(self):
        if self.cache and time.monotonic() < self.expires:
            return self.cache
        if self.enabled:
            try:
                result = await self.request(
                    "GET",
                    f"/api/public/v2/prompts/{quote(PROMPT_NAME, safe='')}",
                    params={"label": self.label},
                )
                if result.get("type") != "text" or not isinstance(result.get("prompt"), str):
                    raise ValueError("Expected a text prompt")
                self.cache = Prompt(result["prompt"], int(result["version"]), True)
                self.expires = time.monotonic() + 60
                return self.cache
            except httpx.HTTPError, KeyError, ValueError:
                logger.warning("Managed prompt unavailable; using the bundled prompt")
        self.cache = Prompt((PROMPTS / "concierge-v1.txt").read_text(), 1)
        self.expires = time.monotonic() + 15
        return self.cache

    async def project_id(self, name):
        """Resolve `name` (the human-readable LANGFUSE_PROJECT_ID) to Langfuse's opaque project id.

        Trace URLs are keyed by that opaque id, not the name shown in the UI. `GET
        /api/public/projects` returns every project the API key can see -- exactly one for a
        project-scoped key -- so a mismatched name most likely means the keys point at a
        different project than `name` names. Cached since this sits on the per-reply hot path.
        """
        if not name or not self.enabled:
            return None
        if self.project_id_cache and time.monotonic() < self.project_id_expires:
            return self.project_id_cache
        try:
            projects = (await self.request("GET", "/api/public/projects")).get("data") or []
            match = next((p for p in projects if p.get("name") == name), None)
            if match is None and len(projects) == 1:
                logger.warning(
                    "Langfuse project name is %r but the API key's project is %r; using it anyway",
                    name,
                    projects[0].get("name"),
                )
                match = projects[0]
            if match is None:
                logger.warning("No Langfuse project named %r visible to this API key", name)
                return None
            self.project_id_cache = match["id"]
            self.project_id_expires = time.monotonic() + 300
            return self.project_id_cache
        except (httpx.HTTPError, KeyError, ValueError):
            logger.warning("Could not resolve the Langfuse project id for %r", name)
            return None

    async def score(self, trace_id, name, value, comment=""):
        if not self.enabled:
            return False
        await self.request(
            "POST",
            "/api/public/scores",
            json={
                "id": f"{trace_id}-{name}",
                "traceId": trace_id,
                "name": name,
                "value": value,
                "dataType": "BOOLEAN",
                "comment": comment,
            },
        )
        return True

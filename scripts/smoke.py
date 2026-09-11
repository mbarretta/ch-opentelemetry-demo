#!/usr/bin/env python3
"""Exercise the running demo and restore its catalog fault setting afterwards."""

import json
import sys
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.demo import RUNTIME, environment, scenario  # noqa: E402


def captured_spans(trace_id):
    spans = []
    for path in (RUNTIME / "telemetry").glob("*.jsonl"):
        with path.open() as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                for resource in record.get("resourceSpans", []):
                    attributes = {
                        a["key"]: a["value"] for a in resource["resource"].get("attributes", [])
                    }
                    service = attributes.get("service.name", {}).get("stringValue", "unknown")
                    for scope in resource.get("scopeSpans", []):
                        for span in scope.get("spans", []):
                            if span.get("traceId") == trace_id:
                                spans.append({**span, "service": service})
    return spans


def main():
    env = environment()
    url = f"http://localhost:{env.get('AGENT_HOST_PORT', '8010')}"
    reports = []
    with httpx.Client(base_url=url, timeout=100) as client:
        health = client.get("/healthz").json()
        assert health["mode"] == "scripted", (
            "Smoke scenarios require AGENT_MODE=scripted"
        )

        def prompt(message, session, name="shopping"):
            response = client.post(
                "/prompt", json={"session_id": session, "message": message, "scenario": name}
            )
            response.raise_for_status()
            result = response.json()
            reports.append(result)
            print(
                f"{name}: {result['trace_id']} · tools={[t['name'] for t in result['tools']]} · scores={result['scores']}"
            )
            return result

        flags = RUNTIME / "flagd/demo.flagd.json"
        original = flags.read_text()
        try:
            scenario("shopping")
            session = str(uuid4())
            success = prompt("Find a beginner telescope for the moon under $150", session)
            assert success["scores"] == {"budget_adherence": 1}, success
            prompt("Add it to my cart", session)
            own = prompt("Show my cart", session)
            other = prompt("Show my cart", str(uuid4()))
            assert own["tools"][0]["result"]["items"][0]["productId"] == "OLJCESPC7Z"
            assert other["tools"][0]["result"]["items"] == [], other
            bad = prompt("Recommend a telescope under $150", str(uuid4()), "budget-violation")
            assert bad["scores"] == {"budget_adherence": 0}, bad
            scenario("backend-failure")
            for _ in range(10):
                failed = prompt("Look up Explorascope", str(uuid4()), "backend-failure")
                if failed["tools"][0]["result"].get("error"):
                    break
                time.sleep(1)
            else:
                raise AssertionError("Catalog fault did not activate")
            assert "couldn't" in failed["reply"], failed
        finally:
            temp = flags.with_suffix(".restore")
            temp.write_text(original)
            temp.replace(flags)
        expected_services = {"agent", "frontend", "product-catalog"}
        if health["tools_transport"] == "mcp":
            expected_services.add("mcp")
        for _ in range(15):
            spans = captured_spans(success["trace_id"])
            services = {s["service"] for s in spans}
            if expected_services <= services:
                break
            time.sleep(1)
        assert expected_services <= services, services
        assert any(s["name"] == "model.generate" for s in spans)
        assert any(s["name"] == "get_product" for s in spans)
        span_ids = {s["spanId"] for s in spans}
        assert sum(not s.get("parentSpanId") for s in spans) == 1
        assert all(not s.get("parentSpanId") or s["parentSpanId"] in span_ids for s in spans)
        for span in spans:
            attrs = {a["key"]: a["value"] for a in span.get("attributes", [])}
            if span["service"] in {"agent", "mcp"}:
                assert attrs.get("session.id", {}).get("stringValue") == success["session_id"], (
                    span["service"], span["name"], "missing session context"
                )
            if span["name"] == "POST /prompt":
                assert attrs["langfuse.observation.input"]["stringValue"]
                assert attrs["langfuse.observation.output"]["stringValue"] == success["reply"]
        print("Correlated trace services:", dict(Counter(s["service"] for s in spans)))
        (RUNTIME / "smoke-results.json").write_text(json.dumps(reports, indent=2))
        print("All smoke scenarios passed. Original fault configuration restored.")


if __name__ == "__main__":
    main()

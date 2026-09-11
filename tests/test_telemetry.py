import asyncio

from opentelemetry import baggage, context, propagate

from concierge.telemetry import conversation, tracer


def test_context_crosses_wire_and_only_allowed_baggage_becomes_attributes(spans):
    with conversation("anonymous-session", "shopping", "scripted"):
        carrier = {}
        propagate.inject(carrier)
    assert not baggage.get_all()
    remote_context = propagate.extract(carrier)
    remote_context = baggage.set_baggage("secret", "must-not-be-recorded", remote_context)
    token = context.attach(remote_context)
    try:
        with tracer.start_as_current_span("remote-mcp-tool"):
            pass
    finally:
        context.detach(token)
    attrs = spans.get_finished_spans()[0].attributes
    assert attrs["session.id"] == "anonymous-session"
    assert attrs["gen_ai.conversation.id"] == "anonymous-session"
    assert attrs["langfuse.trace.metadata.scenario"] == "shopping"
    assert "secret" not in attrs
    assert "user.id" not in attrs


async def test_concurrent_conversations_do_not_leak_baggage(spans):
    async def turn(session):
        with conversation(session, "shopping", "scripted"):
            await asyncio.sleep(0)
            with tracer.start_as_current_span(session):
                assert baggage.get_baggage("session.id") == session

    await asyncio.gather(turn("first"), turn("second"))
    assert all(s.attributes["session.id"] == s.name for s in spans.get_finished_spans())
    assert not baggage.get_all()

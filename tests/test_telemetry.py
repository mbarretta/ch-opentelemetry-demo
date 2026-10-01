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
    assert attrs["langfuse.trace.tags"] == ("astronomy-concierge",)
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


async def test_mcp_stream_writer_sends_inject_the_caller_span_and_record_no_span_of_their_own(spans):
    """Drives the released writer: the request carries the caller's context, nothing is recorded."""
    from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCResponse
    from opentelemetry.instrumentation.mcp.instrumentation import InstrumentedStreamWriter

    from concierge.telemetry import drop_mcp_stream_writer_spans

    class Stream:
        def __init__(self):
            self.sent = []

        async def send(self, item):
            self.sent.append(item)

    drop_mcp_stream_writer_spans()
    stream = Stream()
    writer = InstrumentedStreamWriter(stream, tracer)
    request = JSONRPCMessage(JSONRPCRequest(jsonrpc="2.0", id=8, method="tools/list"))
    response = JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=8, result={"tools": []}))
    with tracer.start_as_current_span("tools/list.mcp") as caller:
        await writer.send(request)
        await writer.send(response)

    assert [s.name for s in spans.get_finished_spans()] == ["tools/list.mcp"]
    traceparent = request.root.params["_meta"]["traceparent"]
    assert traceparent.split("-")[2] == format(caller.get_span_context().span_id, "016x")
    assert stream.sent == [request, response]

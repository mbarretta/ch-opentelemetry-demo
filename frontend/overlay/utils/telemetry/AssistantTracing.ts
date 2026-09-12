// Tracing for one assistant turn, shared by the browser gateway and the /api/assistant routes.
//
// Browser: withAssistantTurn wraps a turn's request in an assistant.turn span from the tracer the
// storefront already registers (FrontendTracer.ts: one WebTracerProvider, one fetch
// instrumentation, one exporter); nothing is registered here. The request runs inside the span's
// context, so the fetch instrumentation's span is its child, and the traceparent that
// instrumentation injects makes the proxy, storefront, agent, and shop spans one trace.
//
// Server: recordTurnOnActiveSpan stamps the same identity on the storefront's API route span, so
// every hop of the trace can be grouped by conversation and by storefront (cart) session.

import { Attributes, context, propagation, SpanStatusCode, trace } from '@opentelemetry/api';
import { AssistantResponse } from '../../types/Assistant';
import { AttributeNames } from '../enums/AttributeNames';

export const TURN_SPAN_NAME = 'assistant.turn';
// The conversation is both the gen_ai conversation and the Langfuse session; the storefront
// session (AttributeNames.SESSION_ID) is the cart, the id the released SessionIdProcessor stamps.
const ATTR_CONVERSATION_ID = 'gen_ai.conversation.id';
const ATTR_LANGFUSE_SESSION_ID = 'langfuse.session.id';
const ATTR_REQUEST_ID = 'assistant.request_id';
const ATTR_CONTRACT_VERSION = 'assistant.contract_version';
const ATTR_TURN_KIND = 'assistant.turn.kind';
type AssistantTurnKind = 'message' | 'action';

export interface AssistantTurnIdentity {
  // null on the first turn: the agent assigns the conversation and the answer carries it.
  conversationId?: string | null;
  shopSessionId?: string;
  requestId?: string;
  contractVersion?: string;
}

const turnAttributes = ({ conversationId, shopSessionId, requestId, contractVersion }: AssistantTurnIdentity): Attributes => {
  const attributes: Attributes = {};
  if (conversationId) {
    attributes[ATTR_CONVERSATION_ID] = conversationId;
    attributes[ATTR_LANGFUSE_SESSION_ID] = conversationId;
  }
  if (shopSessionId) attributes[AttributeNames.SESSION_ID] = shopSessionId;
  if (requestId) attributes[ATTR_REQUEST_ID] = requestId;
  if (contractVersion) attributes[ATTR_CONTRACT_VERSION] = contractVersion;
  return attributes;
};

// Server side: the span the request is running in (Next's span for the API route).
export const recordTurnOnActiveSpan = (identity: AssistantTurnIdentity) => {
  trace.getSpan(context.active())?.setAttributes(turnAttributes(identity));
};

type TurnResponse = Pick<AssistantResponse, 'conversation_id' | 'contract_version'>;

// Browser side. The span ends when the request settles; a rejection sets ERROR status and is rethrown.
export const withAssistantTurn = async <T extends TurnResponse>(
  kind: AssistantTurnKind,
  identity: AssistantTurnIdentity & { shopSessionId: string; requestId: string },
  run: () => Promise<T>
): Promise<T> => {
  const span = trace
    .getTracer('astronomy-shop.assistant')
    .startSpan(TURN_SPAN_NAME, { attributes: { [ATTR_TURN_KIND]: kind, ...turnAttributes(identity) } });
  // The same baggage the released Api.gateway sets on shop calls, so the agent's baggage
  // allow-list sees the storefront session on every hop; the API routes read it from the body.
  const baggage = (propagation.getActiveBaggage() ?? propagation.createBaggage()).setEntry(AttributeNames.SESSION_ID, {
    value: identity.shopSessionId,
  });
  const turnContext = propagation.setBaggage(trace.setSpan(context.active(), span), baggage);
  try {
    const response = await context.with(turnContext, run);
    span.setAttributes(turnAttributes({ conversationId: response.conversation_id, contractVersion: response.contract_version }));
    return response;
  } catch (error) {
    if (error instanceof Error) span.recordException(error);
    span.setStatus({ code: SpanStatusCode.ERROR, message: error instanceof Error ? error.message : String(error) });
    throw error;
  } finally {
    span.end();
  }
};

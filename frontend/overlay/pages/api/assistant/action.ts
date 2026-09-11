// POST /api/assistant/action: one add-to-cart action on the shopper's own cart, performed by
// the agent inside the conversation's trace and deduplicated by request_id. The route's span
// carries the conversation and request id, and the storefront session from the browser's baggage.

import AgentGateway from '../../../gateways/http/Agent.gateway';
import { assistantRoute, parseActionRequest } from '../../../services/Assistant.service';
import { baggageSessionId, recordTurnOnActiveSpan } from '../../../utils/telemetry/AssistantTracing';
import InstrumentationMiddleware from '../../../utils/telemetry/InstrumentationMiddleware';

const handler = assistantRoute('POST', async ({ body }) => {
  const action = parseActionRequest(body);
  recordTurnOnActiveSpan({ conversationId: action.conversation_id, shopSessionId: baggageSessionId(), requestId: action.request_id });
  const response = await AgentGateway.addToCart(action);
  recordTurnOnActiveSpan({ contractVersion: response.contract_version });
  return response;
});

export default InstrumentationMiddleware(handler);

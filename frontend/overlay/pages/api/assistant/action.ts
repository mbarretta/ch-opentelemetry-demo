// POST /api/assistant/action: one add-to-cart action on the shopper's own cart, performed by
// the agent inside the conversation's trace and deduplicated by request_id. The agent refuses
// the action (409) unless the conversation is bound to the storefront session the body names.
// The route's span carries the conversation, storefront session, and request id.

import AgentGateway from '../../../gateways/http/Agent.gateway';
import { assistantRoute, parseActionRequest } from '../../../services/Assistant.service';
import { recordTurnOnActiveSpan } from '../../../utils/telemetry/AssistantTracing';
import InstrumentationMiddleware from '../../../utils/telemetry/InstrumentationMiddleware';

const handler = assistantRoute('POST', async ({ body }) => {
  const action = parseActionRequest(body);
  recordTurnOnActiveSpan({ conversationId: action.conversation_id, shopSessionId: action.shop_session_id, requestId: action.request_id });
  const response = await AgentGateway.addToCart(action);
  recordTurnOnActiveSpan({ contractVersion: response.contract_version });
  return response;
});

export default InstrumentationMiddleware(handler);

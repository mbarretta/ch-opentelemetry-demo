// POST /api/assistant/message: one conversation turn, forwarded to the agent server-side.
// The browser never learns the agent address; the response is the agent's AssistantResponse
// or an AssistantErrorPayload with a matching status. The route's span carries the conversation,
// storefront session, and request id (the conversation from the answer on a first turn).

import AgentGateway from '../../../gateways/http/Agent.gateway';
import { assistantRoute, parseMessageRequest } from '../../../services/Assistant.service';
import { recordTurnOnActiveSpan } from '../../../utils/telemetry/AssistantTracing';
import InstrumentationMiddleware from '../../../utils/telemetry/InstrumentationMiddleware';

const handler = assistantRoute('POST', async ({ body }) => {
  const message = parseMessageRequest(body);
  recordTurnOnActiveSpan({ conversationId: message.conversation_id, shopSessionId: message.shop_session_id, requestId: message.request_id });
  const response = await AgentGateway.sendMessage(message);
  recordTurnOnActiveSpan({ conversationId: response.conversation_id, contractVersion: response.contract_version });
  return response;
});

export default InstrumentationMiddleware(handler);

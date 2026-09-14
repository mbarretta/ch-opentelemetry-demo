// GET /api/assistant/conversation/{conversationId}?shop_session_id=: is this conversation still
// alive on the agent and bound to the caller's shop session? 200 with its status, 404 (code
// "expired") when the agent no longer knows it, or 409 (code "conflict", reason "foreign") when
// it belongs to another session. The agent decides ownership; the status never names the session.

import AgentGateway from '../../../../gateways/http/Agent.gateway';
import { assistantRoute, parseConversationQuery } from '../../../../services/Assistant.service';
import InstrumentationMiddleware from '../../../../utils/telemetry/InstrumentationMiddleware';

const handler = assistantRoute('GET', ({ query }) => {
  const { conversationId, shopSessionId } = parseConversationQuery(query);
  return AgentGateway.getConversation(conversationId, shopSessionId);
});

export default InstrumentationMiddleware(handler);

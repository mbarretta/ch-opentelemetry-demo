// GET /api/assistant/conversation/{conversationId}: is this conversation still alive on the
// agent? 200 with its status, or 404 (code "expired") when the agent no longer knows it.

import AgentGateway from '../../../../gateways/http/Agent.gateway';
import { assistantRoute, parseConversationId } from '../../../../services/Assistant.service';
import InstrumentationMiddleware from '../../../../utils/telemetry/InstrumentationMiddleware';

const handler = assistantRoute('GET', ({ query }) => AgentGateway.getConversation(parseConversationId(query.conversationId)));

export default InstrumentationMiddleware(handler);

// POST /api/assistant/message: one conversation turn, forwarded to the agent server-side.
// The browser never learns the agent address; the response is the agent's AssistantResponse
// or an AssistantErrorPayload with a matching status.

import AgentGateway from '../../../gateways/http/Agent.gateway';
import { assistantRoute, parseMessageRequest } from '../../../services/Assistant.service';
import InstrumentationMiddleware from '../../../utils/telemetry/InstrumentationMiddleware';

const handler = assistantRoute('POST', ({ body }) => AgentGateway.sendMessage(parseMessageRequest(body)));

export default InstrumentationMiddleware(handler);

// POST /api/assistant/action: one add-to-cart action on the shopper's own cart, performed by
// the agent inside the conversation's trace and deduplicated by request_id.

import AgentGateway from '../../../gateways/http/Agent.gateway';
import { assistantRoute, parseActionRequest } from '../../../services/Assistant.service';
import InstrumentationMiddleware from '../../../utils/telemetry/InstrumentationMiddleware';

const handler = assistantRoute('POST', ({ body }) => AgentGateway.addToCart(parseActionRequest(body)));

export default InstrumentationMiddleware(handler);

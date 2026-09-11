// POST /api/assistant/feedback: Helpful / Not helpful for one answer, keyed by that answer's
// trace_id and conversation_id. Answers { saved: true } only when the agent stored the score;
// an agent that has no score store configured answers 503, which reaches the browser as a
// not-saved result, never as saved.

import AgentGateway from '../../../gateways/http/Agent.gateway';
import { assistantRoute, parseFeedbackRequest } from '../../../services/Assistant.service';
import InstrumentationMiddleware from '../../../utils/telemetry/InstrumentationMiddleware';

const handler = assistantRoute('POST', ({ body }) => AgentGateway.submitFeedback(parseFeedbackRequest(body)));

export default InstrumentationMiddleware(handler);

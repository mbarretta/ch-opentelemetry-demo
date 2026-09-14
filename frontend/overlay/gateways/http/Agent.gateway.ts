// Server-side HTTP client for the concierge agent, used only by the /api/assistant routes.
//
// The agent address is read from the frontend service's environment on every call, so it is
// never captured at build time and never reaches the browser bundle. Trace context is added to
// the outgoing request by the released Node auto-instrumentation (undici/http), so no headers
// are built here; only a JSON content type is sent.

import {
  ASSISTANT_CONFLICT_REASONS,
  AssistantActionRequest,
  AssistantConflictReason,
  AssistantConversationStatus,
  AssistantFeedbackRequest,
  AssistantMessageRequest,
  AssistantResponse,
} from '../../types/Assistant';
import { AssistantRouteError } from '../../services/Assistant.service';

// Upstream deadline for one agent call. Envoy's /api/assistant/ route allows 120 s, so this is
// long enough for a real agent turn and still answers the browser before the proxy gives up.
export const AGENT_TIMEOUT_MS = 100_000;

const KEEP_MESSAGE = 'Your message is kept; try again.';
const DETAIL_MAX_LENGTH = 300;

const agentBaseUrl = (): string => {
  const configured = (process.env.AGENT_BASE_URL ?? '').trim().replace(/\/+$/, '');
  if (!configured) {
    throw new AssistantRouteError(503, 'unavailable', 'The shopping assistant is not configured for this storefront.', false);
  }
  return configured;
};

interface AgentDetail {
  message?: string;
  reason?: AssistantConflictReason;
}

const isConflictReason = (value: unknown): value is AssistantConflictReason =>
  typeof value === 'string' && (ASSISTANT_CONFLICT_REASONS as readonly string[]).includes(value);

// FastAPI reports HTTPException reasons as { detail: string }; the agent's own turn and cart
// failures carry { detail: { message, trace_id } } and its 409s { detail: { message, reason } };
// validation errors carry a list (ignored).
const agentDetail = (payload: unknown): AgentDetail => {
  const detail = typeof payload === 'object' && payload !== null ? (payload as { detail?: unknown }).detail : undefined;
  const structured = typeof detail === 'object' && detail !== null ? (detail as { message?: unknown; reason?: unknown }) : undefined;
  const message = structured ? structured.message : detail;
  return {
    message: typeof message === 'string' && message.length > 0 && message.length <= DETAIL_MAX_LENGTH ? message : undefined,
    reason: isConflictReason(structured?.reason) ? structured.reason : undefined,
  };
};

const failureFor = (status: number, payload: unknown): AssistantRouteError => {
  const { message: detail, reason } = agentDetail(payload);
  switch (status) {
    case 400:
    case 422:
      return new AssistantRouteError(400, 'invalid', detail ?? 'The assistant rejected this request.', false);
    case 404:
      return new AssistantRouteError(404, 'expired', detail ?? 'This conversation has expired. Start a new one.', false);
    case 409:
      // Only a turn still in flight clears on its own (the agent deduplicates by request_id, so
      // repeating it later is safe); a foreign, exhausted, or rebound conversation never will.
      return new AssistantRouteError(409, 'conflict', detail ?? 'The assistant refused this turn. Start a new conversation.', reason === 'in_flight', reason);
    case 502:
    case 503:
      return new AssistantRouteError(502, 'unavailable', detail ?? `The assistant is unavailable right now. ${KEEP_MESSAGE}`, true);
    default:
      return new AssistantRouteError(502, 'unavailable', `The assistant hit an internal error. ${KEEP_MESSAGE}`, true);
  }
};

const isTimeout = (error: unknown) =>
  error instanceof Error && (error.name === 'TimeoutError' || error.name === 'AbortError');

const callAgent = async <T>(method: 'GET' | 'POST', path: string, body?: object): Promise<T> => {
  const url = `${agentBaseUrl()}${path}`;
  let response: Response;
  let raw: string;
  try {
    response = await fetch(url, {
      method,
      headers: body === undefined ? undefined : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(AGENT_TIMEOUT_MS),
    });
    // The deadline covers the body too: a stalled body after early headers is still a timeout.
    raw = await response.text();
  } catch (error) {
    // The browser gets a generic message; the reason (DNS, refused connection, deadline) goes to the server log.
    console.error(`[assistant] ${method} ${url} failed:`, error instanceof Error ? `${error.name}: ${error.message}` : error);
    if (isTimeout(error)) {
      const seconds = Math.round(AGENT_TIMEOUT_MS / 1000);
      throw new AssistantRouteError(504, 'timeout', `The assistant did not answer within ${seconds} seconds. ${KEEP_MESSAGE}`, true);
    }
    throw new AssistantRouteError(502, 'unavailable', `The assistant could not be reached. ${KEEP_MESSAGE}`, true);
  }

  let payload: unknown;
  try {
    payload = raw ? JSON.parse(raw) : undefined;
  } catch {
    payload = undefined;
  }
  if (!response.ok) throw failureFor(response.status, payload);
  if (typeof payload !== 'object' || payload === null) {
    throw new AssistantRouteError(502, 'unavailable', 'The assistant answered in an unexpected format.', false);
  }
  return payload as T;
};

const AgentGateway = () => ({
  sendMessage(request: AssistantMessageRequest) {
    return callAgent<AssistantResponse>('POST', '/assistant/message', request);
  },
  addToCart(request: AssistantActionRequest) {
    return callAgent<AssistantResponse>('POST', '/assistant/actions/add-to-cart', request);
  },
  // The agent's feedback route predates the storefront contract: it keys on the conversation
  // id as session_id and scores 1 (helpful) or 0 (not helpful).
  submitFeedback({ conversation_id, trace_id, helpful }: AssistantFeedbackRequest) {
    return callAgent<{ saved: boolean }>('POST', '/feedback', { session_id: conversation_id, trace_id, value: helpful ? 1 : 0 });
  },
  getConversation(conversationId: string) {
    return callAgent<AssistantConversationStatus>('GET', `/assistant/conversations/${encodeURIComponent(conversationId)}`);
  },
});

export default AgentGateway();

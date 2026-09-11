// Server-side HTTP client for the concierge agent, used only by the /api/assistant routes.
//
// The agent address is read from the frontend service's environment on every call, so it is
// never captured at build time and never reaches the browser bundle. Trace context is added to
// the outgoing request by the released Node auto-instrumentation (undici/http), so no headers
// are built here; only a JSON content type is sent.

import {
  AssistantActionRequest,
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

// FastAPI reports HTTPException reasons as { detail: string }; validation errors carry a list.
const agentDetail = (payload: unknown): string | undefined => {
  const detail = typeof payload === 'object' && payload !== null ? (payload as { detail?: unknown }).detail : undefined;
  return typeof detail === 'string' && detail.length > 0 && detail.length <= DETAIL_MAX_LENGTH ? detail : undefined;
};

const failureFor = (status: number, payload: unknown): AssistantRouteError => {
  const detail = agentDetail(payload);
  switch (status) {
    case 400:
    case 422:
      return new AssistantRouteError(400, 'invalid', detail ?? 'The assistant rejected this request.', false);
    case 404:
      return new AssistantRouteError(404, 'expired', detail ?? 'This conversation has expired. Start a new one.', false);
    case 409:
      // The agent deduplicates by request_id, so repeating the same turn later is safe.
      return new AssistantRouteError(409, 'conflict', detail ?? 'The assistant is still working on your previous request.', true);
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

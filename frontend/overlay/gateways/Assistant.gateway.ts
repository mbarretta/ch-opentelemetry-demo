// Chooses the assistant transport at runtime from the window.ENV block that pages/_document.tsx
// renders from the frontend service's environment when the server starts, not from a build-time variable.
//
// The live transport (default) talks only to the storefront's own /api/assistant routes, which
// call the agent server-side; the browser never learns the agent address. Each turn runs inside
// an assistant.turn span (utils/telemetry/AssistantTracing.ts) so the storefront, agent, and shop
// spans it causes belong to one trace with the conversation and storefront session on it.

import FixtureTransport from './AssistantFixtures';
import SessionGateway from './Session.gateway';
import {
  AssistantConversationStatus,
  AssistantError,
  AssistantErrorPayload,
  AssistantFeedbackResult,
  AssistantResponse,
  AssistantTransport,
  isAssistantErrorPayload,
} from '../types/Assistant';
import request from '../utils/Request';
import { withAssistantTurn } from '../utils/telemetry/AssistantTracing';

const basePath = '/api/assistant';
export const UNREACHABLE = 'The assistant could not be reached. Your message is kept; try again.';

const assistantEnv = () => (typeof window === 'undefined' ? undefined : window.ENV);

export const isDemoDetailsEnabled = () => /^(1|true|on|yes)$/i.test(assistantEnv()?.ASSISTANT_DEMO_DETAILS ?? '');

// The routes answer every failure with an AssistantErrorPayload; anything else that is not a
// JSON object (an empty body, a proxy error page) means the storefront itself did not answer.
const unwrap = <T extends object>(payload: T | AssistantErrorPayload | undefined): T => {
  if (isAssistantErrorPayload(payload)) {
    throw new AssistantError(payload.error.code, payload.error.message, payload.error.retryable);
  }
  if (typeof payload !== 'object' || payload === null) throw new AssistantError('unavailable', UNREACHABLE);
  return payload;
};

const post = async <T extends object>(path: string, body: object): Promise<T> =>
  unwrap(await request<T | AssistantErrorPayload | undefined>({ url: `${basePath}/${path}`, method: 'POST', body }));

const get = async <T extends object>(path: string): Promise<T> =>
  unwrap(await request<T | AssistantErrorPayload | undefined>({ url: `${basePath}/${path}`, method: 'GET' }));

const LiveTransport: AssistantTransport = {
  name: 'live',
  sendMessage(message) {
    const identity = { conversationId: message.conversation_id, shopSessionId: message.shop_session_id, requestId: message.request_id };
    return withAssistantTurn('message', identity, () => post<AssistantResponse>('message', message));
  },
  addToCart(action) {
    // The action body names the conversation only; the storefront session it is bound to is this browser's.
    const identity = { conversationId: action.conversation_id, shopSessionId: SessionGateway.getSession().userId, requestId: action.request_id };
    return withAssistantTurn('action', identity, () => post<AssistantResponse>('action', action));
  },
  async submitFeedback(feedback) {
    try {
      const result = await post<AssistantFeedbackResult>('feedback', feedback);
      return { saved: result.saved === true };
    } catch (error) {
      // A rejected score (for example, no score store configured) is a not-saved result with its reason.
      if (error instanceof AssistantError) return { saved: false, message: error.message };
      throw error;
    }
  },
  getConversation(conversationId) {
    return get<AssistantConversationStatus>(`conversation/${encodeURIComponent(conversationId)}`);
  },
};

export const getAssistantTransport = (): AssistantTransport => {
  const configured = (assistantEnv()?.ASSISTANT_TRANSPORT ?? '').toLowerCase();
  return configured === 'fixtures' ? FixtureTransport : LiveTransport;
};

// Anything a transport throws that is not already an AssistantError is a transport failure.
export const toAssistantError = (error: unknown): AssistantError =>
  error instanceof AssistantError ? error : new AssistantError('unavailable', UNREACHABLE);

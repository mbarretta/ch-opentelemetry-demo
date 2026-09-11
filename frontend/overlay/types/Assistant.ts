// Astronomy Shop assistant: the browser side of the agent conversation contract.
//
// The agent exports the same version as CONTRACT_VERSION in concierge/contract.py; the two
// constants are compared on every response and a mismatch is a recoverable error. The request
// and response interfaces mirror the Pydantic models in that module field for field
// (tests/test_frontend_overlay.py checks the two stay aligned).

import { Money } from '../protos/demo';

export const ASSISTANT_CONTRACT_VERSION = '1';

// live: the same-origin /api/assistant routes (default). fixtures: canned answers, no agent call.
export const ASSISTANT_TRANSPORTS = ['live', 'fixtures'] as const;
export type AssistantTransportName = (typeof ASSISTANT_TRANSPORTS)[number];

export const ASSISTANT_SCENARIOS = ['shopping', 'backend-failure', 'budget-violation'] as const;
export type AssistantScenario = (typeof ASSISTANT_SCENARIOS)[number];

export interface AssistantProductContext {
  product_id: string;
}

export interface AssistantMessageRequest {
  conversation_id: string | null;
  shop_session_id: string;
  request_id: string;
  message: string;
  currency_code: string;
  product_context?: AssistantProductContext;
  scenario?: AssistantScenario;
  budget?: number;
}

export interface AssistantActionRequest {
  conversation_id: string;
  request_id: string;
  product_id: string;
  quantity: number;
  currency_code: string;
}

export interface AssistantFeedbackRequest {
  conversation_id: string;
  trace_id: string;
  helpful: boolean;
}

export interface AssistantFeedbackResult {
  saved: boolean;
  // Why the score was not stored, in the agent's words (for example, feedback storage is not configured).
  message?: string;
}

export interface AssistantProductRef {
  id: string;
  name: string;
  picture: string;
  price: Money;
}

// One tool call the agent made during the turn, as the model saw it (the injected cart identity is omitted).
export interface AssistantDemoToolCall {
  name: string;
  arguments: Record<string, unknown>;
  ok: boolean;
}

export interface AssistantDemo {
  mode: string;
  scenario: string;
  prompt_version: string | number;
  prompt_source: string;
  tools: AssistantDemoToolCall[];
  links: Record<string, string>;
}

export interface AssistantResponse {
  contract_version: string;
  conversation_id: string;
  request_id: string;
  reply: string;
  product_refs: AssistantProductRef[];
  cart_changed: boolean;
  trace_id: string;
  feedback_enabled: boolean;
  demo: AssistantDemo;
}

export interface AssistantConversationStatus {
  conversation_id: string;
  shop_session_id: string;
  turns: number;
  currency_code: string;
  expires_at: string;
}

export type AssistantErrorCode =
  | 'unavailable' // the agent could not be reached or answered with a server error
  | 'timeout' // the agent did not answer before the storefront's deadline
  | 'conflict' // another turn is in flight or the conversation belongs to another session
  | 'expired' // the conversation is unknown to the agent (idle timeout or agent restart)
  | 'invalid' // the storefront or the agent rejected the request itself
  | 'contract_mismatch';

// The JSON body every /api/assistant route sends with a non-2xx status.
export interface AssistantErrorPayload {
  error: {
    code: AssistantErrorCode;
    message: string;
    retryable: boolean;
  };
}

export const isAssistantErrorPayload = (value: unknown): value is AssistantErrorPayload =>
  typeof value === 'object' &&
  value !== null &&
  typeof (value as AssistantErrorPayload).error === 'object' &&
  (value as AssistantErrorPayload).error !== null &&
  typeof (value as AssistantErrorPayload).error.code === 'string' &&
  typeof (value as AssistantErrorPayload).error.message === 'string';

export class AssistantError extends Error {
  readonly code: AssistantErrorCode;
  readonly retryable: boolean;

  constructor(code: AssistantErrorCode, message: string, retryable = true) {
    super(message);
    this.name = 'AssistantError';
    this.code = code;
    this.retryable = retryable;
  }
}

export interface AssistantTransport {
  readonly name: AssistantTransportName;
  sendMessage(request: AssistantMessageRequest): Promise<AssistantResponse>;
  addToCart(request: AssistantActionRequest): Promise<AssistantResponse>;
  submitFeedback(request: AssistantFeedbackRequest): Promise<AssistantFeedbackResult>;
}

export type FeedbackState = 'saved' | 'not_saved' | 'pending';

// Transcript entries are what the panel renders. Ids are the request_id prefixed by kind
// (u:, a:, c:) because a user message and its answer share one request_id.
export type TranscriptEntry =
  | { kind: 'user'; id: string; text: string; productContext?: AssistantProductContext }
  | { kind: 'assistant'; id: string; response: AssistantResponse; feedback?: FeedbackState; feedbackNote?: string }
  | { kind: 'cart'; id: string; response: AssistantResponse; productName: string; quantity: number };

export type PendingTurn =
  | { kind: 'message'; request: AssistantMessageRequest }
  | { kind: 'action'; request: AssistantActionRequest; productName: string };

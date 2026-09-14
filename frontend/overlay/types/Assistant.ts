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

// Why the agent refused a turn with 409 (mirrors ConflictReason in concierge/contract.py):
// the conversation belongs to another shop session, a turn is still in flight, the turn limit
// is reached, or the turn asked for another scenario or budget. Only in_flight clears on its own.
export const ASSISTANT_CONFLICT_REASONS = ['foreign', 'in_flight', 'turn_limit', 'rebind'] as const;
export type AssistantConflictReason = (typeof ASSISTANT_CONFLICT_REASONS)[number];

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
  shop_session_id: string;
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

// What the agent tells the owning shop session about a live conversation. The bound session is
// deliberately absent: the agent answers 409 (reason 'foreign') instead of naming it.
export interface AssistantConversationStatus {
  conversation_id: string;
  turns: number;
  currency_code: string;
  expires_at: string;
}

export type AssistantErrorCode =
  | 'unavailable' // the agent could not be reached or answered with a server error
  | 'timeout' // the agent did not answer before the storefront's deadline
  | 'conflict' // the agent refused the turn for this conversation; reason says why
  | 'expired' // the conversation is unknown to the agent (idle timeout or agent restart)
  | 'invalid' // the storefront or the agent rejected the request itself
  | 'contract_mismatch';

// The JSON body every /api/assistant route sends with a non-2xx status.
export interface AssistantErrorPayload {
  error: {
    code: AssistantErrorCode;
    message: string;
    retryable: boolean;
    // Set on code 'conflict' when the agent named its reason.
    reason?: AssistantConflictReason;
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
  readonly reason?: AssistantConflictReason;

  constructor(code: AssistantErrorCode, message: string, retryable = true, reason?: AssistantConflictReason) {
    super(message);
    this.name = 'AssistantError';
    this.code = code;
    this.retryable = retryable;
    this.reason = reason;
  }
}

export interface AssistantTransport {
  readonly name: AssistantTransportName;
  sendMessage(request: AssistantMessageRequest): Promise<AssistantResponse>;
  addToCart(request: AssistantActionRequest): Promise<AssistantResponse>;
  submitFeedback(request: AssistantFeedbackRequest): Promise<AssistantFeedbackResult>;
  // Is this conversation still alive on the agent and bound to this shop session? Rejects with
  // code 'expired' when it is gone and code 'conflict', reason 'foreign', when it is another's.
  getConversation(conversationId: string, shopSessionId: string): Promise<AssistantConversationStatus>;
}

export type FeedbackState = 'saved' | 'not_saved' | 'pending';

// Transcript entries are what the panel renders. Ids are the request_id prefixed by kind
// (u:, a:, c:) because a user message and its answer share one request_id; notices (n:) are
// the storefront's own remarks about the conversation (it expired, a cart action was confirmed).
export type TranscriptEntry =
  | { kind: 'user'; id: string; text: string; productContext?: AssistantProductContext }
  | { kind: 'assistant'; id: string; response: AssistantResponse; feedback?: FeedbackState; feedbackNote?: string }
  | { kind: 'cart'; id: string; response: AssistantResponse; productName: string; quantity: number }
  | { kind: 'notice'; id: string; text: string };

export interface MessageTurn {
  kind: 'message';
  request: AssistantMessageRequest;
}

export interface ActionTurn {
  kind: 'action';
  request: AssistantActionRequest;
  productName: string;
  // How many of this product the cart held when the shopper clicked. When the agent does not
  // answer in time, a refetched cart holding at least quantityBefore + quantity confirms the action.
  quantityBefore: number;
}

export type PendingTurn = MessageTurn | ActionTurn;

// What the browser keeps across page loads (gateways/AssistantSession.gateway.ts). A stored
// conversation is a candidate only: the provider resumes it after the agent confirms it is alive
// and bound to this shop session. An unresolved cart action travels with it so a reload cannot
// hide an outcome the shopper still has to check.
export interface StoredConversation {
  version: 1;
  shopSessionId: string;
  conversationId: string | null;
  contractVersion: string | null;
  budget: number;
  transcript: TranscriptEntry[];
  uncertain: ActionTurn | null;
}

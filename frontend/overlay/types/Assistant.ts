// Astronomy Shop assistant: the browser side of the agent conversation contract.
//
// The agent exports the same version as CONTRACT_VERSION in concierge/contract.py; the two
// constants are compared on every response and a mismatch is a recoverable error.

import { Money } from '../protos/demo';

export const ASSISTANT_CONTRACT_VERSION = '1';

export const ASSISTANT_TRANSPORTS = ['fixtures', 'unconfigured'] as const;
export type AssistantTransportName = (typeof ASSISTANT_TRANSPORTS)[number];

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
  scenario?: string;
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

export interface AssistantProductRef {
  id: string;
  name: string;
  picture: string;
  price: Money;
}

export interface AssistantDemo {
  mode: string;
  prompt_version: string;
  prompt_source: string;
  tools: string[];
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

export type AssistantErrorCode =
  | 'unavailable' // the agent could not be reached or answered with a server error
  | 'timeout'
  | 'conflict' // another turn is in flight or the conversation belongs to another session
  | 'expired' // the conversation is unknown to the agent (idle timeout or agent restart)
  | 'contract_mismatch'
  | 'unconfigured'; // no transport is configured for this build

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
  submitFeedback(request: AssistantFeedbackRequest): Promise<{ saved: boolean }>;
}

// Transcript entries are what the panel renders. Ids are the request_id prefixed by kind
// (u:, a:, c:) because a user message and its answer share one request_id.
export type TranscriptEntry =
  | { kind: 'user'; id: string; text: string; productContext?: AssistantProductContext }
  | { kind: 'assistant'; id: string; response: AssistantResponse; feedback?: 'saved' | 'not_saved' | 'pending' }
  | { kind: 'cart'; id: string; response: AssistantResponse; productName: string; quantity: number };

export type PendingTurn =
  | { kind: 'message'; request: AssistantMessageRequest }
  | { kind: 'action'; request: AssistantActionRequest; productName: string };

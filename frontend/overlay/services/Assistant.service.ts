// Server side of the storefront assistant routes (pages/api/assistant/*): validates what the
// browser sent field by field, so only allow-listed fields of the right shape reach the agent,
// and turns every failure into the AssistantErrorPayload envelope the live transport reads.
//
// Validation is hand-written on purpose: the contract is small and adding a schema library
// would add an npm dependency to the released frontend image.

import type { NextApiHandler, NextApiRequest, NextApiResponse } from 'next';
import { context, Exception, trace } from '@opentelemetry/api';
import {
  ASSISTANT_SCENARIOS,
  AssistantActionRequest,
  AssistantConflictReason,
  AssistantErrorCode,
  AssistantErrorPayload,
  AssistantFeedbackRequest,
  AssistantMessageRequest,
  AssistantScenario,
} from '../types/Assistant';

// Same limits as the Pydantic models in concierge/contract.py.
const MESSAGE_MAX_LENGTH = 4000;
const BUDGET_MAX = 100000;
const QUANTITY_MAX = 10;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const CURRENCY_CODE = /^[A-Z]{3}$/;
const PRODUCT_ID = /^[A-Za-z0-9_-]{1,64}$/;
const TRACE_ID = /^[a-f0-9]{32}$/;

// A failure the route reports to the browser: the HTTP status it answers with plus the typed error.
export class AssistantRouteError extends Error {
  readonly status: number;
  readonly code: AssistantErrorCode;
  readonly retryable: boolean;
  readonly reason?: AssistantConflictReason;

  constructor(status: number, code: AssistantErrorCode, message: string, retryable: boolean, reason?: AssistantConflictReason) {
    super(message);
    this.name = 'AssistantRouteError';
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.reason = reason;
  }

  toPayload(): AssistantErrorPayload {
    const error: AssistantErrorPayload['error'] = { code: this.code, message: this.message, retryable: this.retryable };
    if (this.reason) error.reason = this.reason;
    return { error };
  }
}

const invalid = (message: string) => new AssistantRouteError(400, 'invalid', message, false);

type Fields = Record<string, unknown>;

const fields = (value: unknown, allowed: readonly string[], what: string): Fields => {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw invalid(`Send ${what} as a JSON object.`);
  }
  const unknownKeys = Object.keys(value).filter(key => !allowed.includes(key));
  if (unknownKeys.length > 0) {
    throw invalid(`Unexpected field${unknownKeys.length > 1 ? 's' : ''} in ${what}: ${unknownKeys.join(', ')}.`);
  }
  return value as Fields;
};

const uuid = (body: Fields, key: string): string => {
  const value = body[key];
  if (typeof value !== 'string' || !UUID.test(value)) throw invalid(`${key} must be a UUID.`);
  return value;
};

const uuidOrNull = (body: Fields, key: string): string | null => {
  const value = body[key];
  return value === null || value === undefined ? null : uuid(body, key);
};

const text = (body: Fields, key: string, maxLength: number): string => {
  const value = body[key];
  if (typeof value !== 'string' || value.trim().length === 0) throw invalid(`${key} must be a non-empty string.`);
  if (value.length > maxLength) throw invalid(`${key} must be at most ${maxLength} characters.`);
  return value;
};

const pattern = (body: Fields, key: string, expected: RegExp, description: string): string => {
  const value = body[key];
  if (typeof value !== 'string' || !expected.test(value)) throw invalid(`${key} must be ${description}.`);
  return value;
};

const currencyCode = (body: Fields): string =>
  body.currency_code === undefined ? 'USD' : pattern(body, 'currency_code', CURRENCY_CODE, 'a three-letter currency code');

const productId = (body: Fields): string => pattern(body, 'product_id', PRODUCT_ID, 'a catalog product id');

const wholeNumber = (body: Fields, key: string, min: number, max: number): number => {
  const value = body[key];
  if (typeof value !== 'number' || !Number.isInteger(value) || value < min || value > max) {
    throw invalid(`${key} must be a whole number between ${min} and ${max}.`);
  }
  return value;
};

const positiveAmount = (body: Fields, key: string, max: number): number => {
  const value = body[key];
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0 || value > max) {
    throw invalid(`${key} must be a number greater than 0 and at most ${max}.`);
  }
  return value;
};

const productContext = (body: Fields): AssistantMessageRequest['product_context'] => {
  if (body.product_context === undefined || body.product_context === null) return undefined;
  return { product_id: productId(fields(body.product_context, ['product_id'], 'product_context')) };
};

const scenario = (body: Fields): AssistantScenario | undefined => {
  const value = body.scenario;
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'string' || !(ASSISTANT_SCENARIOS as readonly string[]).includes(value)) {
    throw invalid(`scenario must be one of ${ASSISTANT_SCENARIOS.join(', ')}.`);
  }
  return value as AssistantScenario;
};

// Each parser builds a fresh object holding only the contract's fields, so nothing else the
// browser sent can travel to the agent even by accident.

export const parseMessageRequest = (value: unknown): AssistantMessageRequest => {
  const body = fields(
    value,
    ['conversation_id', 'shop_session_id', 'request_id', 'message', 'currency_code', 'product_context', 'scenario', 'budget'],
    'the message'
  );
  const request: AssistantMessageRequest = {
    conversation_id: uuidOrNull(body, 'conversation_id'),
    shop_session_id: uuid(body, 'shop_session_id'),
    request_id: uuid(body, 'request_id'),
    message: text(body, 'message', MESSAGE_MAX_LENGTH),
    currency_code: currencyCode(body),
  };
  const chosenContext = productContext(body);
  if (chosenContext) request.product_context = chosenContext;
  const chosenScenario = scenario(body);
  if (chosenScenario) request.scenario = chosenScenario;
  if (body.budget !== undefined && body.budget !== null) {
    request.budget = positiveAmount(body, 'budget', BUDGET_MAX);
  }
  return request;
};

export const parseActionRequest = (value: unknown): AssistantActionRequest => {
  const body = fields(
    value,
    ['conversation_id', 'shop_session_id', 'request_id', 'product_id', 'quantity', 'currency_code'],
    'the cart action'
  );
  return {
    conversation_id: uuid(body, 'conversation_id'),
    shop_session_id: uuid(body, 'shop_session_id'),
    request_id: uuid(body, 'request_id'),
    product_id: productId(body),
    quantity: body.quantity === undefined ? 1 : wholeNumber(body, 'quantity', 1, QUANTITY_MAX),
    currency_code: currencyCode(body),
  };
};

export const parseFeedbackRequest = (value: unknown): AssistantFeedbackRequest => {
  const body = fields(value, ['conversation_id', 'trace_id', 'helpful'], 'the feedback');
  if (typeof body.helpful !== 'boolean') throw invalid('helpful must be true or false.');
  return {
    conversation_id: uuid(body, 'conversation_id'),
    trace_id: pattern(body, 'trace_id', TRACE_ID, 'a 32-character hex trace id'),
    helpful: body.helpful,
  };
};

export const parseConversationId = (value: string | string[] | undefined): string => {
  if (typeof value !== 'string' || !UUID.test(value)) throw invalid('conversationId must be a UUID.');
  return value;
};

// Builds the handler body for one route: one method, validated input, JSON out, and every
// failure answered as an AssistantErrorPayload with a matching status. Routes wrap the result
// in InstrumentationMiddleware themselves, like the released API routes do.
export const assistantRoute = <T>(method: 'GET' | 'POST', run: (request: NextApiRequest) => Promise<T>): NextApiHandler =>
  async (request: NextApiRequest, response: NextApiResponse) => {
    if (request.method !== method) {
      response.setHeader('Allow', method);
      const failure = new AssistantRouteError(405, 'invalid', `This route accepts ${method} only.`, false);
      return response.status(failure.status).json(failure.toPayload());
    }
    try {
      return response.status(200).json(await run(request));
    } catch (error) {
      if (error instanceof AssistantRouteError) {
        return response.status(error.status).json(error.toPayload());
      }
      // Something other than the agent failed inside the storefront; keep the browser contract.
      trace.getSpan(context.active())?.recordException(error as Exception);
      const failure = new AssistantRouteError(500, 'unavailable', 'The storefront could not process this request. Try again.', true);
      return response.status(failure.status).json(failure.toPayload());
    }
  };

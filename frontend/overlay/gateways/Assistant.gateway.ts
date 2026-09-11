// Chooses the assistant transport at runtime from the window.ENV block that pages/_document.tsx
// renders from the frontend service's environment when the server starts, not from a build-time variable.

import FixtureTransport from './AssistantFixtures';
import { AssistantError, AssistantTransport } from '../types/Assistant';

const assistantEnv = () => (typeof window === 'undefined' ? undefined : window.ENV);

export const isDemoDetailsEnabled = () => /^(1|true|on|yes)$/i.test(assistantEnv()?.ASSISTANT_DEMO_DETAILS ?? '');

// A build without a configured transport answers every turn with a recoverable error instead
// of pretending; the live transport arrives with the same-origin API routes.
const UnconfiguredTransport: AssistantTransport = {
  name: 'unconfigured',
  async sendMessage() {
    throw new AssistantError('unconfigured', 'The shopping assistant is not connected in this deployment.', false);
  },
  async addToCart() {
    throw new AssistantError('unconfigured', 'The shopping assistant is not connected in this deployment.', false);
  },
  async submitFeedback() {
    return { saved: false };
  },
};

export const getAssistantTransport = (): AssistantTransport => {
  const configured = (assistantEnv()?.ASSISTANT_TRANSPORT ?? '').toLowerCase();
  return configured === 'fixtures' ? FixtureTransport : UnconfiguredTransport;
};

// Anything a transport throws that is not already an AssistantError is a transport failure.
export const toAssistantError = (error: unknown): AssistantError =>
  error instanceof AssistantError
    ? error
    : new AssistantError('unavailable', 'The assistant could not be reached. Your message is kept; try again.');

// The assistant conversation this browser keeps across page loads, stored beside the shop
// session (Session.gateway.ts) in localStorage. What is stored is a candidate only:
// providers/Assistant.provider.tsx resumes it after GET /api/assistant/conversation/{id},
// asked with this shop session, confirms the agent still has the conversation and that it is
// bound to that session (a 409 reason 'foreign' otherwise).

import { StoredConversation } from '../types/Assistant';

const storageKey = 'assistant';
const VERSION = 1;

const isStored = (value: unknown): value is StoredConversation => {
  const candidate = value as StoredConversation | null;
  return (
    typeof candidate === 'object' &&
    candidate !== null &&
    candidate.version === VERSION &&
    typeof candidate.shopSessionId === 'string' &&
    (candidate.conversationId === null || typeof candidate.conversationId === 'string') &&
    (candidate.contractVersion === null || typeof candidate.contractVersion === 'string') &&
    typeof candidate.budget === 'number' &&
    Array.isArray(candidate.transcript) &&
    (candidate.uncertain === null || typeof candidate.uncertain === 'object')
  );
};

const AssistantSessionGateway = () => ({
  load(): StoredConversation | null {
    if (typeof window === 'undefined') return null;
    const raw = localStorage.getItem(storageKey);
    if (!raw) return null;
    try {
      const parsed: unknown = JSON.parse(raw);
      if (isStored(parsed)) return parsed;
    } catch (error) {
      console.warn('Failed to parse the stored assistant conversation', error);
    }
    localStorage.removeItem(storageKey);
    return null;
  },
  save(conversation: Omit<StoredConversation, 'version'>) {
    if (typeof window === 'undefined') return;
    localStorage.setItem(storageKey, JSON.stringify({ version: VERSION, ...conversation }));
  },
});

export default AssistantSessionGateway();

// Conversation state for the shopping assistant. It sits above the pages (inside CartProvider in
// pages/_app.tsx) so the transcript survives closing the panel and navigating between pages.

import { createContext, useCallback, useContext, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { v4 } from 'uuid';
import { getAssistantTransport, isDemoDetailsEnabled, toAssistantError } from '../gateways/Assistant.gateway';
import SessionGateway from '../gateways/Session.gateway';
import {
  ASSISTANT_CONTRACT_VERSION,
  AssistantError,
  AssistantProductRef,
  PendingTurn,
  TranscriptEntry,
} from '../types/Assistant';
import { useCurrency } from './Currency.provider';

export const DEFAULT_BUDGET = 150;

export interface AssistantProductContext {
  productId: string;
  name?: string;
}

interface OpenOptions {
  productContext?: AssistantProductContext;
}

interface IContext {
  isOpen: boolean;
  open(options?: OpenOptions): void;
  close(): void;
  conversationId: string | null;
  transcript: TranscriptEntry[];
  pending: PendingTurn | null;
  error: AssistantError | null;
  draft: string;
  setDraft(text: string): void;
  budget: number;
  setBudget(budget: number): void;
  currencyCode: string;
  productContext: AssistantProductContext | null;
  clearProductContext(): void;
  sendMessage(text: string): Promise<void>;
  retry(): Promise<void>;
  addToCart(product: AssistantProductRef, quantity?: number): Promise<void>;
  submitFeedback(entryId: string, helpful: boolean): Promise<void>;
  announcement: string;
  demoDetailsEnabled: boolean;
  // The element that opened the panel; focus returns to it on close.
  openerRef: React.MutableRefObject<HTMLElement | null>;
}

const noop = async () => {};
const subscribeToNothing = () => () => {};

export const Context = createContext<IContext>({
  isOpen: false,
  open: () => {},
  close: () => {},
  conversationId: null,
  transcript: [],
  pending: null,
  error: null,
  draft: '',
  setDraft: () => {},
  budget: DEFAULT_BUDGET,
  setBudget: () => {},
  currencyCode: 'USD',
  productContext: null,
  clearProductContext: () => {},
  sendMessage: noop,
  retry: noop,
  addToCart: noop,
  submitFeedback: noop,
  announcement: '',
  demoDetailsEnabled: false,
  openerRef: { current: null },
});

export const useAssistant = () => useContext(Context);

interface IProps {
  children: React.ReactNode;
}

const AssistantProvider = ({ children }: IProps) => {
  const { selectedCurrency } = useCurrency();
  const queryClient = useQueryClient();
  const [isOpen, setIsOpen] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [pending, setPending] = useState<PendingTurn | null>(null);
  const [failedTurn, setFailedTurn] = useState<PendingTurn | null>(null);
  const [error, setError] = useState<AssistantError | null>(null);
  const [draft, setDraft] = useState('');
  const [budget, setBudget] = useState(DEFAULT_BUDGET);
  const [productContext, setProductContext] = useState<AssistantProductContext | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const openerRef = useRef<HTMLElement | null>(null);
  const currencyCode = selectedCurrency || 'USD';
  // window.ENV exists only in the browser; the server snapshot is "off" and never changes.
  const demoDetailsEnabled = useSyncExternalStore(subscribeToNothing, isDemoDetailsEnabled, () => false);

  const open = useCallback((options?: OpenOptions) => {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (options?.productContext) {
      setProductContext(options.productContext);
    }
    setIsOpen(true);
  }, []);

  const close = useCallback(() => setIsOpen(false), []);
  const clearProductContext = useCallback(() => setProductContext(null), []);

  const run = useCallback(
    async (turn: PendingTurn) => {
      const transport = getAssistantTransport();
      setPending(turn);
      setError(null);
      setFailedTurn(null);
      setAnnouncement(turn.kind === 'message' ? 'Looking up products' : `Adding ${turn.productName} to your cart`);
      try {
        const response =
          turn.kind === 'message' ? await transport.sendMessage(turn.request) : await transport.addToCart(turn.request);
        if (response.contract_version !== ASSISTANT_CONTRACT_VERSION) {
          throw new AssistantError(
            'contract_mismatch',
            `The assistant answered with contract version ${response.contract_version}; this storefront expects ${ASSISTANT_CONTRACT_VERSION}.`,
            false
          );
        }
        setConversationId(response.conversation_id);
        setTranscript(entries => [
          ...entries,
          turn.kind === 'message'
            ? { kind: 'assistant', id: `a:${response.request_id}`, response }
            : {
                kind: 'cart',
                id: `c:${response.request_id}`,
                response,
                productName: turn.productName,
                quantity: turn.request.quantity,
              },
        ]);
        if (response.cart_changed) {
          queryClient.invalidateQueries({ queryKey: ['cart'] });
        }
        setAnnouncement(turn.kind === 'message' ? 'The assistant replied' : `Added ${turn.productName} to your cart`);
      } catch (caught) {
        const failure = toAssistantError(caught);
        setError(failure);
        setFailedTurn(turn);
        setAnnouncement(failure.message);
      } finally {
        setPending(null);
      }
    },
    [queryClient]
  );

  const sendMessage = useCallback(
    async (text: string) => {
      const message = text.trim();
      if (!message || pending) return;
      const requestId = v4();
      setTranscript(entries => [
        ...entries,
        {
          kind: 'user',
          id: `u:${requestId}`,
          text: message,
          productContext: productContext ? { product_id: productContext.productId } : undefined,
        },
      ]);
      setDraft('');
      await run({
        kind: 'message',
        request: {
          conversation_id: conversationId,
          shop_session_id: SessionGateway.getSession().userId,
          request_id: requestId,
          message,
          currency_code: currencyCode,
          product_context: productContext ? { product_id: productContext.productId } : undefined,
          budget,
        },
      });
    },
    [budget, conversationId, currencyCode, pending, productContext, run]
  );

  const retry = useCallback(async () => {
    // Same request_id: the agent deduplicates a repeated turn instead of running it twice.
    if (failedTurn && !pending) await run(failedTurn);
  }, [failedTurn, pending, run]);

  const addToCart = useCallback(
    async (product: AssistantProductRef, quantity = 1) => {
      if (pending || !conversationId) return;
      await run({
        kind: 'action',
        productName: product.name,
        request: {
          conversation_id: conversationId,
          request_id: v4(),
          product_id: product.id,
          quantity,
          currency_code: currencyCode,
        },
      });
    },
    [conversationId, currencyCode, pending, run]
  );

  const submitFeedback = useCallback(
    async (entryId: string, helpful: boolean) => {
      const entry = transcript.find(candidate => candidate.id === entryId);
      if (!entry || entry.kind !== 'assistant' || entry.feedback) return;
      const setFeedback = (feedback: 'saved' | 'not_saved' | 'pending') =>
        setTranscript(entries =>
          entries.map(candidate => (candidate.id === entryId && candidate.kind === 'assistant' ? { ...candidate, feedback } : candidate))
        );
      setFeedback('pending');
      try {
        const { saved } = await getAssistantTransport().submitFeedback({
          conversation_id: entry.response.conversation_id,
          trace_id: entry.response.trace_id,
          helpful,
        });
        setFeedback(saved ? 'saved' : 'not_saved');
        setAnnouncement(saved ? 'Feedback saved' : 'Feedback not saved');
      } catch {
        setFeedback('not_saved');
        setAnnouncement('Feedback not saved');
      }
    },
    [transcript]
  );

  const value = useMemo(
    () => ({
      isOpen,
      open,
      close,
      conversationId,
      transcript,
      pending,
      error,
      draft,
      setDraft,
      budget,
      setBudget,
      currencyCode,
      productContext,
      clearProductContext,
      sendMessage,
      retry,
      addToCart,
      submitFeedback,
      announcement,
      demoDetailsEnabled,
      openerRef,
    }),
    [
      isOpen,
      open,
      close,
      conversationId,
      transcript,
      pending,
      error,
      draft,
      budget,
      currencyCode,
      productContext,
      clearProductContext,
      sendMessage,
      retry,
      addToCart,
      submitFeedback,
      announcement,
      demoDetailsEnabled,
    ]
  );

  return <Context.Provider value={value}>{children}</Context.Provider>;
};

export default AssistantProvider;

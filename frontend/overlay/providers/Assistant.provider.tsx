// Conversation state for the shopping assistant. It sits above the pages (inside CartProvider in
// pages/_app.tsx) so the transcript survives closing the panel and navigating between pages, and
// it is kept in the browser (gateways/AssistantSession.gateway.ts) so a page load can resume it
// once the agent confirms the conversation is still alive.
//
// The cart is the shop's. A card's Add to cart goes through POST /api/assistant/action, which the
// agent performs on the bound shop session; this provider only refetches React Query ['cart'] so
// the header count, dropdown, and cart page follow. It never calls the storefront cart mutation.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { v4 } from 'uuid';
import { getAssistantTransport, isDemoDetailsEnabled, toAssistantError, UNREACHABLE } from '../gateways/Assistant.gateway';
import AssistantSessionGateway from '../gateways/AssistantSession.gateway';
import SessionGateway from '../gateways/Session.gateway';
import {
  ActionTurn,
  ASSISTANT_CONTRACT_VERSION,
  AssistantError,
  AssistantProductRef,
  FeedbackState,
  PendingTurn,
  StoredConversation,
  TranscriptEntry,
} from '../types/Assistant';
import { IProductCartItem } from '../types/Cart';
import { useCart } from './Cart.provider';
import { useCurrency } from './Currency.provider';

export const DEFAULT_BUDGET = 150;

// Notes the transcript opens with when a conversation is replaced.
const EXPIRED_NOTE = 'Your previous conversation expired, so this is a new one. Your cart is unchanged.';
const FOREIGN_NOTE = 'Your previous conversation belonged to another shopper session and was not restored. This is a new one.';
const UNREACHABLE_NOTE = 'The assistant could not be reached to restore your previous conversation, so this is a new one.';
const unresolvedNote = (turn: ActionTurn) =>
  `The assistant did not confirm whether ${turn.productName} was added to your cart. Check your cart before adding it again.`;

export interface AssistantProductContext {
  productId: string;
  name?: string;
}

// A cart action the agent did not answer in time. Its outcome is unknown until the refetched
// cart holds what the click asked for (confirmed) or the shopper retries with the same request_id.
export interface UncertainAction {
  turn: ActionTurn;
  confirmed: boolean;
}

interface OpenOptions {
  productContext?: AssistantProductContext;
}

interface IContext {
  isOpen: boolean;
  open(options?: OpenOptions): void;
  close(): void;
  conversationId: string | null;
  // The contract_version the agent answered with, once a turn has completed.
  contractVersion: string | null;
  transcript: TranscriptEntry[];
  pending: PendingTurn | null;
  // True while a stored conversation is being confirmed with the agent after a page load.
  resuming: boolean;
  uncertain: UncertainAction | null;
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
  retryUncertain(): Promise<void>;
  dismissUncertain(): void;
  // A fresh conversation_id and an empty transcript; the cart is untouched.
  newConversation(): void;
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
  contractVersion: null,
  transcript: [],
  pending: null,
  resuming: false,
  uncertain: null,
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
  retryUncertain: noop,
  dismissUncertain: () => {},
  newConversation: () => {},
  submitFeedback: noop,
  announcement: '',
  demoDetailsEnabled: false,
  openerRef: { current: null },
});

export const useAssistant = () => useContext(Context);

const quantityOf = (items: IProductCartItem[], productId: string) =>
  items.filter(item => item.productId === productId).reduce((total, item) => total + item.quantity, 0);

const notice = (text: string): TranscriptEntry => ({ kind: 'notice', id: `n:${v4()}`, text });

// The transcript a replaced conversation starts with: the reason it was replaced, then the cart
// action the old conversation never confirmed, if any. That action cannot be retried against a
// conversation the agent no longer has (the retry could not be deduplicated), so it becomes a note.
const freshTranscript = (notes: string[], unresolved: ActionTurn | null): TranscriptEntry[] =>
  [...notes, ...(unresolved ? [unresolvedNote(unresolved)] : [])].map(notice);

// What a stored conversation becomes on this page load. Only a conversation the agent confirms
// as alive and bound to this shop session is restored; anything else starts fresh with a note.
// The agent makes the ownership call from the session sent with the request (409 reason
// 'foreign'); a stored conversation another session left in this browser is not even asked about.
const resolveStored = async (
  stored: StoredConversation | null,
  shopSessionId: string
): Promise<{ restore: boolean; notes: string[] }> => {
  if (!stored) return { restore: false, notes: [] };
  if (stored.shopSessionId !== shopSessionId) return { restore: false, notes: [FOREIGN_NOTE] };
  if (stored.conversationId === null) return { restore: true, notes: [] };
  try {
    await getAssistantTransport().getConversation(stored.conversationId, shopSessionId);
    return { restore: true, notes: [] };
  } catch (caught) {
    const failure = toAssistantError(caught);
    const note = failure.reason === 'foreign' ? FOREIGN_NOTE : failure.code === 'expired' ? EXPIRED_NOTE : UNREACHABLE_NOTE;
    return { restore: false, notes: [note] };
  }
};

interface IProps {
  children: React.ReactNode;
}

const AssistantProvider = ({ children }: IProps) => {
  const { selectedCurrency } = useCurrency();
  const {
    cart: { items: cartItems },
  } = useCart();
  const queryClient = useQueryClient();
  const [isOpen, setIsOpen] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [contractVersion, setContractVersion] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [pending, setPending] = useState<PendingTurn | null>(null);
  const [failedTurn, setFailedTurn] = useState<PendingTurn | null>(null);
  const [unresolved, setUnresolved] = useState<ActionTurn | null>(null);
  // True until the stored conversation has been resolved; nothing is stored back before then.
  const [resuming, setResuming] = useState(true);
  const [error, setError] = useState<AssistantError | null>(null);
  const [draft, setDraft] = useState('');
  const [budget, setBudget] = useState(DEFAULT_BUDGET);
  const [productContext, setProductContext] = useState<AssistantProductContext | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const openerRef = useRef<HTMLElement | null>(null);
  // Set synchronously when a turn starts: two clicks in one frame both still see pending === null.
  const inFlightRef = useRef(false);
  const currencyCode = selectedCurrency || 'USD';
  // window.ENV exists only in the browser; the server snapshot is "off" and never changes.
  const demoDetailsEnabled = useSyncExternalStore(subscribeToNothing, isDemoDetailsEnabled, () => false);

  // Page load: resume the stored conversation only once the agent confirms it.
  useEffect(() => {
    let cancelled = false;
    const stored = AssistantSessionGateway.load();
    const shopSessionId = SessionGateway.getSession().userId;
    resolveStored(stored, shopSessionId).then(({ restore, notes }) => {
      if (cancelled) return;
      const ours = stored !== null && stored.shopSessionId === shopSessionId;
      if (ours) setBudget(stored.budget);
      if (restore && ours) {
        setConversationId(stored.conversationId);
        setContractVersion(stored.contractVersion);
        setUnresolved(stored.uncertain);
        // A score that was still being sent when the page unloaded is offered again.
        setTranscript(
          stored.transcript.map(entry =>
            entry.kind === 'assistant' && entry.feedback === 'pending' ? { ...entry, feedback: undefined } : entry
          )
        );
      } else {
        setTranscript(freshTranscript(notes, ours ? stored.uncertain : null));
      }
      setResuming(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (resuming) return;
    AssistantSessionGateway.save({
      shopSessionId: SessionGateway.getSession().userId,
      conversationId,
      contractVersion,
      budget,
      transcript,
      uncertain: unresolved,
    });
  }, [resuming, conversationId, contractVersion, budget, transcript, unresolved]);

  // The refetched cart settles an unresolved action once it holds what the click asked for.
  const uncertain = useMemo<UncertainAction | null>(
    () =>
      unresolved && {
        turn: unresolved,
        confirmed: quantityOf(cartItems, unresolved.request.product_id) >= unresolved.quantityBefore + unresolved.request.quantity,
      },
    [cartItems, unresolved]
  );

  const open = useCallback((options?: OpenOptions) => {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (options?.productContext) {
      setProductContext(options.productContext);
    }
    setIsOpen(true);
  }, []);

  const close = useCallback(() => setIsOpen(false), []);
  const clearProductContext = useCallback(() => setProductContext(null), []);

  const startFresh = useCallback(
    (notes: string[] = []) => {
      setConversationId(null);
      setContractVersion(null);
      setError(null);
      setFailedTurn(null);
      setUnresolved(null);
      setTranscript(freshTranscript(notes, unresolved));
    },
    [unresolved]
  );

  // One failed turn. The failure decides what the shopper sees: a cart action the agent did not
  // answer in time is uncertain (the cart is refetched, nothing is retried until the shopper asks;
  // the agent deduplicates by request_id); an expired conversation, or one the agent says belongs
  // to another shop session (409 reason 'foreign'), is replaced by a fresh one and the message
  // returns to the composer; a message the agent will never accept as sent (a 409 the gateway
  // marked not retryable: turn limit, rebind) returns to the composer too; everything else offers Retry.
  const fail = useCallback(
    (turn: PendingTurn, failure: AssistantError) => {
      setAnnouncement(failure.message);
      if (turn.kind === 'action' && failure.code === 'timeout') {
        setUnresolved(turn);
        queryClient.invalidateQueries({ queryKey: ['cart'] });
        return;
      }
      const message = turn.kind === 'message' ? turn.request.message : null;
      if (failure.code === 'expired' || failure.reason === 'foreign') {
        startFresh([failure.reason === 'foreign' ? FOREIGN_NOTE : EXPIRED_NOTE]);
        if (message !== null) setDraft(message);
        return;
      }
      if (message !== null && !failure.retryable) {
        setTranscript(entries => entries.filter(entry => entry.id !== `u:${turn.request.request_id}`));
        setDraft(message);
        setError(failure);
        return;
      }
      setError(failure);
      if (failure.retryable) setFailedTurn(turn);
    },
    [queryClient, startFresh]
  );

  const run = useCallback(
    async (turn: PendingTurn) => {
      const transport = getAssistantTransport();
      inFlightRef.current = true;
      setPending(turn);
      setError(null);
      setFailedTurn(null);
      setAnnouncement(turn.kind === 'message' ? 'Looking up products' : `Adding ${turn.productName} to your cart`);
      try {
        const response =
          turn.kind === 'message' ? await transport.sendMessage(turn.request) : await transport.addToCart(turn.request);
        setContractVersion(response.contract_version);
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
        // An uncertain action retried with its request_id is settled by whatever the agent answers.
        setUnresolved(current => (current && current.request.request_id === response.request_id ? null : current));
        if (response.cart_changed) {
          queryClient.invalidateQueries({ queryKey: ['cart'] });
        }
        setAnnouncement(turn.kind === 'message' ? 'The assistant replied' : `Added ${turn.productName} to your cart`);
      } catch (caught) {
        fail(turn, toAssistantError(caught));
      } finally {
        inFlightRef.current = false;
        setPending(null);
      }
    },
    [fail, queryClient]
  );

  const sendMessage = useCallback(
    async (text: string) => {
      const message = text.trim();
      if (!message || inFlightRef.current || resuming) return;
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
    [budget, conversationId, currencyCode, productContext, resuming, run]
  );

  const retry = useCallback(async () => {
    // Same request_id: the agent deduplicates a repeated turn instead of running it twice.
    if (failedTurn && !inFlightRef.current) await run(failedTurn);
  }, [failedTurn, run]);

  const addToCart = useCallback(
    async (product: AssistantProductRef, quantity = 1) => {
      if (inFlightRef.current || resuming) return;
      if (!conversationId) {
        // Cards belong to the conversation that produced them; a cart action needs a live one.
        setError(new AssistantError('expired', 'This conversation has ended. Ask the assistant again to add a product.', false));
        return;
      }
      await run({
        kind: 'action',
        productName: product.name,
        quantityBefore: quantityOf(cartItems, product.id),
        request: {
          conversation_id: conversationId,
          shop_session_id: SessionGateway.getSession().userId,
          request_id: v4(),
          product_id: product.id,
          quantity,
          currency_code: currencyCode,
        },
      });
    },
    [cartItems, conversationId, currencyCode, resuming, run]
  );

  const retryUncertain = useCallback(async () => {
    // Same request_id: an action the agent did complete comes back as its stored result. The
    // session is this browser's by construction (a stored conversation is only restored for its
    // own shop session), so an action stored before the field existed is completed here.
    if (unresolved && !inFlightRef.current) {
      await run({ ...unresolved, request: { ...unresolved.request, shop_session_id: SessionGateway.getSession().userId } });
    }
  }, [run, unresolved]);

  const dismissUncertain = useCallback(() => setUnresolved(null), []);

  const newConversation = useCallback(() => {
    if (!inFlightRef.current) startFresh();
  }, [startFresh]);

  const submitFeedback = useCallback(
    async (entryId: string, helpful: boolean) => {
      const entry = transcript.find(candidate => candidate.id === entryId);
      if (!entry || entry.kind !== 'assistant' || entry.feedback) return;
      const setFeedback = (feedback: FeedbackState, feedbackNote?: string) =>
        setTranscript(entries =>
          entries.map(candidate =>
            candidate.id === entryId && candidate.kind === 'assistant' ? { ...candidate, feedback, feedbackNote } : candidate
          )
        );
      setFeedback('pending');
      try {
        const { saved, message } = await getAssistantTransport().submitFeedback({
          conversation_id: entry.response.conversation_id,
          trace_id: entry.response.trace_id,
          helpful,
        });
        setFeedback(saved ? 'saved' : 'not_saved', saved ? undefined : message);
        setAnnouncement(saved ? 'Feedback saved' : 'Feedback not saved');
      } catch {
        setFeedback('not_saved', UNREACHABLE);
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
      contractVersion,
      transcript,
      pending,
      resuming,
      uncertain,
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
      retryUncertain,
      dismissUncertain,
      newConversation,
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
      contractVersion,
      transcript,
      pending,
      resuming,
      uncertain,
      error,
      draft,
      budget,
      currencyCode,
      productContext,
      clearProductContext,
      sendMessage,
      retry,
      addToCart,
      retryUncertain,
      dismissUncertain,
      newConversation,
      submitFeedback,
      announcement,
      demoDetailsEnabled,
    ]
  );

  return <Context.Provider value={value}>{children}</Context.Provider>;
};

export default AssistantProvider;

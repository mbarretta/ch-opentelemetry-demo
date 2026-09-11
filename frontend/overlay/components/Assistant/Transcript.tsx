import { useEffect, useRef } from 'react';
import { useAssistant } from '../../providers/Assistant.provider';
import { TranscriptEntry } from '../../types/Assistant';
import { CypressFields } from '../../utils/enums/CypressFields';
import Input from '../Input';
import AssistantProductCard from './AssistantProductCard';
import * as S from './AssistantPanel.styled';
import DemoDetails from './DemoDetails';
import Feedback from './Feedback';

export const SUGGESTIONS = ['A telescope for a beginner', 'Help me choose under $150', 'Explain this product'];

const EmptyState = () => {
  const { sendMessage, budget, setBudget, currencyCode } = useAssistant();

  return (
    <S.EmptyState>
      <S.EmptyTitle>Find the right telescope</S.EmptyTitle>
      <S.EmptyText>
        Ask for a recommendation, compare options against your budget, or ask about the product you are looking at.
      </S.EmptyText>
      <S.Suggestions aria-label="Suggestions">
        {SUGGESTIONS.map(suggestion => (
          <S.Suggestion
            key={suggestion}
            type="button"
            data-cy={CypressFields.AssistantSuggestion}
            onClick={() => sendMessage(suggestion)}
          >
            {suggestion}
          </S.Suggestion>
        ))}
      </S.Suggestions>
      <S.BudgetRow>
        <Input
          type="number"
          id="assistant-budget"
          label="Budget"
          aria-label="Budget"
          min={1}
          step={1}
          defaultValue={budget}
          onChange={event => {
            // Uncontrolled so the field can be cleared while typing; the budget keeps its last valid value.
            const next = Number(event.target.value);
            if (Number.isFinite(next) && next >= 1) setBudget(next);
          }}
        />
        <S.BudgetSummary>
          Prices in <strong>{currencyCode}</strong>
        </S.BudgetSummary>
      </S.BudgetRow>
    </S.EmptyState>
  );
};

const Entry = ({ entry }: { entry: TranscriptEntry }) => {
  const { demoDetailsEnabled } = useAssistant();

  if (entry.kind === 'user') {
    return (
      <S.Message $from="user" data-cy={CypressFields.AssistantMessage} data-from="user">
        {entry.productContext ? <S.MessageMeta>About product {entry.productContext.product_id}</S.MessageMeta> : null}
        {entry.text}
      </S.Message>
    );
  }

  const { response } = entry;
  return (
    <>
      <S.Message $from="assistant" data-cy={CypressFields.AssistantMessage} data-from="assistant">
        {entry.kind === 'cart' ? <S.MessageMeta>Cart updated</S.MessageMeta> : null}
        {response.reply}
      </S.Message>
      {entry.kind === 'assistant' && response.product_refs.length > 0 ? (
        <S.Cards>
          {response.product_refs.map(product => (
            <AssistantProductCard key={product.id} product={product} />
          ))}
        </S.Cards>
      ) : null}
      {entry.kind === 'assistant' ? <Feedback entryId={entry.id} state={entry.feedback} note={entry.feedbackNote} /> : null}
      {demoDetailsEnabled ? <DemoDetails demo={response.demo} traceId={response.trace_id} /> : null}
    </>
  );
};

const Transcript = () => {
  const { transcript, pending, error, retry } = useAssistant();
  const ref = useRef<HTMLElement>(null);

  // Keep the newest entry in view as the conversation grows.
  useEffect(() => {
    const element = ref.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [transcript.length, pending, error]);

  const showEmptyState = transcript.length === 0 && !pending && !error;

  return (
    <S.Transcript ref={ref} tabIndex={0} aria-label="Conversation" data-cy={CypressFields.AssistantTranscript}>
      {showEmptyState ? <EmptyState /> : null}
      {transcript.map(entry => (
        <Entry key={entry.id} entry={entry} />
      ))}
      {pending ? (
        <S.Status>
          <S.StatusDot aria-hidden="true" />
          {pending.kind === 'message' ? 'Looking up products' : `Adding ${pending.productName} to your cart`}
        </S.Status>
      ) : null}
      {error ? (
        <S.ErrorBox role="alert" data-cy={CypressFields.AssistantError}>
          <p>{error.message}</p>
          {error.retryable ? (
            <div>
              <S.PanelButton type="button" $type="secondary" data-cy={CypressFields.AssistantRetry} onClick={retry}>
                Retry
              </S.PanelButton>
            </div>
          ) : null}
        </S.ErrorBox>
      ) : null}
    </S.Transcript>
  );
};

export default Transcript;

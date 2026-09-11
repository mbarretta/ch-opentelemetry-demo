import { FormEvent } from 'react';
import { useAssistant } from '../../providers/Assistant.provider';
import { CypressFields } from '../../utils/enums/CypressFields';
import Input from '../Input';
import * as S from './AssistantPanel.styled';

export const MESSAGE_MAX_LENGTH = 500;

const Composer = () => {
  const { draft, setDraft, sendMessage, pending, productContext, clearProductContext } = useAssistant();
  const busy = pending !== null;
  // The product id is always shown; the name joins it once the product page has loaded it.
  const contextLabel = productContext
    ? productContext.name
      ? `${productContext.name} (${productContext.productId})`
      : productContext.productId
    : '';

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    sendMessage(draft);
  };

  return (
    <S.Composer onSubmit={onSubmit} data-cy={CypressFields.AssistantComposer} aria-busy={busy}>
      {productContext ? (
        <S.ContextChip data-cy={CypressFields.AssistantContextChip}>
          <span>About: {contextLabel}</span>
          <S.ChipRemove type="button" aria-label={`Remove product context ${contextLabel}`} onClick={clearProductContext}>
            <span aria-hidden="true">×</span>
          </S.ChipRemove>
        </S.ContextChip>
      ) : null}
      <S.ComposerRow>
        <Input
          type="text"
          id="assistant-message"
          label="Message"
          aria-label="Message the assistant"
          placeholder={busy ? 'Waiting for the assistant' : 'Ask about telescopes, budgets, or a product'}
          value={draft}
          onChange={event => setDraft(event.target.value)}
          disabled={busy}
          autoComplete="off"
          maxLength={MESSAGE_MAX_LENGTH}
        />
        <S.PanelButton type="submit" data-cy={CypressFields.AssistantSend} disabled={busy}>
          Send
        </S.PanelButton>
      </S.ComposerRow>
    </S.Composer>
  );
};

export default Composer;

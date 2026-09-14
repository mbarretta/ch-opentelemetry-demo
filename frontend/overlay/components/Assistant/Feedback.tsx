import { useAssistant } from '../../providers/Assistant.provider';
import { FeedbackState } from '../../types/Assistant';
import { CypressFields } from '../../utils/enums/CypressFields';
import * as S from './AssistantPanel.styled';

interface IProps {
  entryId: string;
  // The response's feedback_enabled: whether the agent has somewhere to store the score.
  enabled: boolean;
  state?: FeedbackState;
  // Why the score was not stored, when the agent said.
  note?: string;
}

const NOT_SAVED = 'Feedback not saved.';
// Said up front when the agent reported no score store. The buttons stay: a click still goes to
// the agent and shows its own reason, so the panel never claims an outcome it did not get.
const NOT_CONFIGURED = 'Feedback storage is not configured for this assistant.';

// Helpful / Not helpful for one assistant answer. The result reflects what the transport
// reported: "saved" only when the score was stored.
const Feedback = ({ entryId, enabled, state, note }: IProps) => {
  const { submitFeedback } = useAssistant();

  if (state === 'saved') {
    return (
      <S.FeedbackRow data-cy={CypressFields.AssistantFeedback} data-state="saved">
        Feedback saved. Thank you.
      </S.FeedbackRow>
    );
  }
  if (state === 'not_saved') {
    return (
      <S.FeedbackRow data-cy={CypressFields.AssistantFeedback} data-state="not_saved">
        {NOT_SAVED}
        {note ? ` ${note}` : ''}
      </S.FeedbackRow>
    );
  }

  return (
    <S.FeedbackRow
      role="group"
      aria-label="Was this answer helpful?"
      data-cy={CypressFields.AssistantFeedback}
      data-enabled={enabled ? 'true' : 'false'}
    >
      <span>{enabled ? 'Was this helpful?' : `${NOT_SAVED} ${NOT_CONFIGURED}`}</span>
      <S.FeedbackButton type="button" disabled={state === 'pending'} onClick={() => submitFeedback(entryId, true)}>
        Helpful
      </S.FeedbackButton>
      <S.FeedbackButton type="button" disabled={state === 'pending'} onClick={() => submitFeedback(entryId, false)}>
        Not helpful
      </S.FeedbackButton>
    </S.FeedbackRow>
  );
};

export default Feedback;

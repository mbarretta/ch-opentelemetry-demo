import { useAssistant } from '../../providers/Assistant.provider';
import { FeedbackState } from '../../types/Assistant';
import { CypressFields } from '../../utils/enums/CypressFields';
import * as S from './AssistantPanel.styled';

interface IProps {
  entryId: string;
  state?: FeedbackState;
  // Why the score was not stored, when the agent said.
  note?: string;
}

// Helpful / Not helpful for one assistant answer. The result reflects what the transport
// reported: "saved" only when the score was stored.
const Feedback = ({ entryId, state, note }: IProps) => {
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
        Feedback not saved.{note ? ` ${note}` : ''}
      </S.FeedbackRow>
    );
  }

  return (
    <S.FeedbackRow role="group" aria-label="Was this answer helpful?" data-cy={CypressFields.AssistantFeedback}>
      <span>Was this helpful?</span>
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

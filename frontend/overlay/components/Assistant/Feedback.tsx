import { useAssistant } from '../../providers/Assistant.provider';
import { CypressFields } from '../../utils/enums/CypressFields';
import * as S from './AssistantPanel.styled';

interface IProps {
  entryId: string;
  state?: 'saved' | 'not_saved' | 'pending';
}

// Helpful / Not helpful for one assistant answer. The result reflects what the transport
// reported: "saved" only when the score was stored.
const Feedback = ({ entryId, state }: IProps) => {
  const { submitFeedback } = useAssistant();

  if (state === 'saved') {
    return <S.FeedbackRow data-cy={CypressFields.AssistantFeedback}>Feedback saved. Thank you.</S.FeedbackRow>;
  }
  if (state === 'not_saved') {
    return <S.FeedbackRow data-cy={CypressFields.AssistantFeedback}>Feedback not saved.</S.FeedbackRow>;
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

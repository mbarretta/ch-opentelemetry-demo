import { useAssistant } from '../../providers/Assistant.provider';
import { CypressFields } from '../../utils/enums/CypressFields';
import { ASSISTANT_PANEL_ID } from './AssistantPanel';
import * as S from './AssistantPanel.styled';

// Header control beside the currency switcher and cart. Icon-only below the desktop
// breakpoint (the header has no room for a label there); the accessible name is constant.
const AssistantTrigger = () => {
  const { isOpen, open, close } = useAssistant();

  return (
    <S.Trigger
      type="button"
      data-cy={CypressFields.AssistantTrigger}
      aria-label="Shopping assistant"
      aria-expanded={isOpen}
      aria-controls={ASSISTANT_PANEL_ID}
      title="Shopping assistant"
      onClick={() => (isOpen ? close() : open())}
    >
      <S.TriggerIcon viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M21 12a8 8 0 0 1-8 8H8l-4 3v-3.6A8 8 0 1 1 21 12z" />
        <path d="M8 11h8M8 15h5" />
      </S.TriggerIcon>
      <S.TriggerLabel>Assistant</S.TriggerLabel>
    </S.Trigger>
  );
};

export default AssistantTrigger;

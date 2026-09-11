import { useAssistant } from '../../providers/Assistant.provider';
import { CypressFields } from '../../utils/enums/CypressFields';
import * as S from './AssistantPanel.styled';

interface IProps {
  productId: string;
  name?: string;
}

// Product page control: opens the same panel with this product as the visible context chip.
// The conversation continues; nothing is reset.
const AskAboutProduct = ({ productId, name }: IProps) => {
  const { open } = useAssistant();

  return (
    <S.AskButton
      type="button"
      data-cy={CypressFields.AssistantAskProduct}
      onClick={() => open({ productContext: { productId, name } })}
    >
      Ask about this product
    </S.AskButton>
  );
};

export default AskAboutProduct;

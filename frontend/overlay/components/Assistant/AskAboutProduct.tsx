import { useEffect } from 'react';
import { useAssistant } from '../../providers/Assistant.provider';
import { CypressFields } from '../../utils/enums/CypressFields';
import * as S from './AssistantPanel.styled';

interface IProps {
  productId: string;
  name?: string;
}

// Product page control. While the page is shown its product is the panel's context chip, so a
// suggestion like "Explain this product" has a product however the panel was opened; the button
// opens the same panel (and restores the chip if the shopper removed it). The conversation
// continues; nothing is reset.
const AskAboutProduct = ({ productId, name }: IProps) => {
  const { open, viewProduct, leaveProduct } = useAssistant();

  useEffect(() => {
    viewProduct({ productId, name });
  }, [productId, name, viewProduct]);

  useEffect(() => () => leaveProduct(productId), [productId, leaveProduct]);

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

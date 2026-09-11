import { useAssistant } from '../../providers/Assistant.provider';
import { AssistantProductRef } from '../../types/Assistant';
import { CypressFields } from '../../utils/enums/CypressFields';
import ProductPrice from '../ProductPrice';
import * as S from './AssistantPanel.styled';

interface IProps {
  product: AssistantProductRef;
}

// Compact card for a product the agent referenced. Name and price come from the response's
// product_refs (tool results), never from the reply text.
const AssistantProductCard = ({ product }: IProps) => {
  const { addToCart, pending } = useAssistant();

  return (
    <S.Card data-cy={CypressFields.AssistantProductCard}>
      <S.CardImage src={`/images/products/${product.picture}`} alt="" />
      <S.CardBody>
        <S.CardName>{product.name}</S.CardName>
        <S.CardPrice>
          <ProductPrice price={product.price} />
        </S.CardPrice>
        <S.CardActions>
          <S.CardLink href={`/product/${product.id}`}>View product</S.CardLink>
          <S.CardButton
            type="button"
            data-cy={CypressFields.AssistantCardAddToCart}
            disabled={pending !== null}
            onClick={() => addToCart(product)}
          >
            Add to cart
          </S.CardButton>
        </S.CardActions>
      </S.CardBody>
    </S.Card>
  );
};

export default AssistantProductCard;

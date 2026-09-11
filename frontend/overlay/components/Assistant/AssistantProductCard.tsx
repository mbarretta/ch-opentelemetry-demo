import { useQuery } from '@tanstack/react-query';
import ApiGateway from '../../gateways/Api.gateway';
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
  const { addToCart, pending, resuming, currencyCode } = useAssistant();
  // The agent priced the card in the currency of its turn. When the header currency differs, the
  // catalog is asked for the price in the selected currency through the product page's own query
  // (one cache entry per product and currency). Until it answers, the agent's amount stays labeled
  // with its own currency; an amount is never relabeled.
  const priced = product.price.currencyCode === currencyCode;
  const { data: catalog } = useQuery({
    queryKey: ['product', product.id, 'selectedCurrency', currencyCode],
    queryFn: () => ApiGateway.getProduct(product.id, currencyCode),
    enabled: !priced,
  });
  const price = priced ? product.price : (catalog?.priceUsd ?? product.price);

  return (
    <S.Card data-cy={CypressFields.AssistantProductCard}>
      <S.CardImage src={`/images/products/${product.picture}`} alt="" />
      <S.CardBody>
        <S.CardName>{product.name}</S.CardName>
        <S.CardPrice>
          <ProductPrice price={price} />
        </S.CardPrice>
        <S.CardActions>
          <S.CardLink href={`/product/${product.id}`}>View product</S.CardLink>
          <S.CardButton
            type="button"
            data-cy={CypressFields.AssistantCardAddToCart}
            disabled={pending !== null || resuming}
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

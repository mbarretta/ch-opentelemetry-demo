// Fixture transport: canned assistant responses so every panel state can be reviewed on real
// storefront pages without an agent. Selected by ASSISTANT_TRANSPORT=fixtures (window.ENV).
//
// Product ids, names, pictures, and USD prices are the released Astronomy Shop catalog
// (src/postgresql/init.sql) so "View product" opens a real page. Amounts stay labeled USD:
// fixtures never relabel a price as another currency.
//
// Message keywords that pick a state:
//   "beginner" or "under"   recommendation with product cards
//   "explain" with a product context, or "tell me about"   one card for that product
//   "fail" or "error"       a recoverable error (transcript and composer are kept)
//   "busy"                  the agent's turn-in-flight conflict (Retry is offered)
//   "limit"                 the agent's turn-limit conflict (the message returns to the composer)
//   "unscored"              a text-only reply whose score has nowhere to go (feedback_enabled false)
//   "slow"                  a long pending state
//   anything else           a text-only reply

import { v4 } from 'uuid';
import {
  AssistantActionRequest,
  AssistantDemoToolCall,
  AssistantError,
  AssistantMessageRequest,
  AssistantProductRef,
  AssistantResponse,
  AssistantTransport,
  ASSISTANT_CONTRACT_VERSION,
} from '../types/Assistant';

const RESPONSE_DELAY_MS = 1200;
const SLOW_RESPONSE_DELAY_MS = 8000;

const money = (units: number, nanos: number) => ({ currencyCode: 'USD', units, nanos });

export const FIXTURE_PRODUCTS: Record<string, AssistantProductRef> = {
  OLJCESPC7Z: {
    id: 'OLJCESPC7Z',
    name: 'National Park Foundation Explorascope',
    picture: 'NationalParkFoundationExplorascope.jpg',
    price: money(101, 960000000),
  },
  '1YMWWN1N4O': {
    id: '1YMWWN1N4O',
    name: 'Eclipsmart Travel Refractor Telescope',
    picture: 'EclipsmartTravelRefractorTelescope.jpg',
    price: money(129, 950000000),
  },
  '66VCHSJNUP': {
    id: '66VCHSJNUP',
    name: 'Starsense Explorer Refractor Telescope',
    picture: 'StarsenseExplorer.jpg',
    price: money(349, 950000000),
  },
  L9ECAV7KIM: {
    id: 'L9ECAV7KIM',
    name: 'Lens Cleaning Kit',
    picture: 'LensCleaningKit.jpg',
    price: money(21, 950000000),
  },
  '2ZYFJ3GM2N': {
    id: '2ZYFJ3GM2N',
    name: 'Roof Binoculars',
    picture: 'RoofBinoculars.jpg',
    price: money(209, 950000000),
  },
  '0PUK6V6EV0': {
    id: '0PUK6V6EV0',
    name: 'Solar System Color Imager',
    picture: 'SolarSystemColorImager.jpg',
    price: money(175, 0),
  },
  LS4PSXUNUM: { id: 'LS4PSXUNUM', name: 'Red Flashlight', picture: 'RedFlashlight.jpg', price: money(57, 80000000) },
  '9SIQT8TOJO': {
    id: '9SIQT8TOJO',
    name: 'Optical Tube Assembly',
    picture: 'OpticalTubeAssembly.jpg',
    price: money(3599, 0),
  },
  '6E92ZMYYFZ': { id: '6E92ZMYYFZ', name: 'Solar Filter', picture: 'SolarFilter.jpg', price: money(69, 950000000) },
  HQTGWGPNH4: { id: 'HQTGWGPNH4', name: 'The Comet Book', picture: 'TheCometBook.jpg', price: money(0, 990000000) },
};

const FIXTURE_DEMO = {
  mode: 'fixtures',
  scenario: 'shopping',
  prompt_version: 'fixture',
  prompt_source: 'frontend/overlay/gateways/AssistantFixtures.ts',
  tools: [] as AssistantDemoToolCall[],
  links: {} as Record<string, string>,
};

// Fixture tool calls carry only the name; the live agent reports the model-visible arguments.
const toolCalls = (names: string[]): AssistantDemoToolCall[] => names.map(name => ({ name, arguments: {}, ok: true }));

const wait = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));

const priceText = ({ price }: AssistantProductRef) =>
  `${price.currencyCode} ${(price.units + price.nanos / 1e9).toFixed(2)}`;

// Marks the trace id of an answer issued with feedback_enabled false, so a score sent for it is
// refused, as the live agent refuses a score it has nowhere to store. It lives in the id rather
// than in module memory because the transcript is restored across page loads.
const UNSCORED_TRACE_PREFIX = 'fixture-unscored-';

const respond = (
  request: AssistantMessageRequest | AssistantActionRequest,
  conversationId: string,
  reply: string,
  productRefs: AssistantProductRef[],
  tools: string[],
  { cartChanged = false, feedbackEnabled = true } = {}
): AssistantResponse => ({
  contract_version: ASSISTANT_CONTRACT_VERSION,
  conversation_id: conversationId,
  request_id: request.request_id,
  reply,
  product_refs: productRefs,
  cart_changed: cartChanged,
  trace_id: `${feedbackEnabled ? 'fixture-' : UNSCORED_TRACE_PREFIX}${request.request_id.replace(/-/g, '').slice(0, 32)}`,
  feedback_enabled: feedbackEnabled,
  demo: { ...FIXTURE_DEMO, tools: toolCalls(tools) },
});

// Conversations are minted client-side; the same ids flow through the real transport later.
const newConversationId = () => v4();

const recommendation = (request: AssistantMessageRequest, conversationId: string): AssistantResponse => {
  const budget = request.budget ?? Number.POSITIVE_INFINITY;
  const picks = [FIXTURE_PRODUCTS.OLJCESPC7Z, FIXTURE_PRODUCTS['1YMWWN1N4O'], FIXTURE_PRODUCTS['66VCHSJNUP']].filter(
    product => product.price.units + product.price.nanos / 1e9 <= budget
  );
  const first = picks[0];
  const reply = first
    ? `For a first telescope I would start with the ${first.name} at ${priceText(first)}: a manual alt-azimuth refractor that shows the Moon, planets, and brighter deep-sky objects with no setup beyond pointing it. ${
        picks.length > 2
          ? 'If you want to compare, the other options below stay within your budget too.'
          : picks.length === 2
            ? 'If you want to compare, the other option below stays within your budget too.'
            : ''
      }`.trim()
    : 'Nothing in the catalog fits that budget. The least expensive telescope is the National Park Foundation Explorascope.';
  return respond(request, conversationId, reply, picks, ['list_products', 'get_product']);
};

const explain = (request: AssistantMessageRequest, conversationId: string): AssistantResponse => {
  const product = request.product_context ? FIXTURE_PRODUCTS[request.product_context.product_id] : undefined;
  if (!product) {
    return respond(
      request,
      conversationId,
      'Open a product page and use "Ask about this product", or name the product you mean, and I will explain it.',
      [],
      []
    );
  }
  return respond(
    request,
    conversationId,
    `The ${product.name} is listed at ${priceText(product)}. It suits observers who want a portable, ready-to-use instrument; see the card below for the details page.`,
    [product],
    ['get_product']
  );
};

const FixtureTransport: AssistantTransport = {
  name: 'fixtures',

  async sendMessage(request) {
    const text = request.message.toLowerCase();
    await wait(text.includes('slow') ? SLOW_RESPONSE_DELAY_MS : RESPONSE_DELAY_MS);
    if (text.includes('fail') || text.includes('error')) {
      throw new AssistantError('unavailable', 'The assistant did not answer. Your message is kept; try again.');
    }
    if (text.includes('busy')) {
      throw new AssistantError('conflict', 'A turn is already in flight for this conversation.', true, 'in_flight');
    }
    if (text.includes('limit')) {
      throw new AssistantError('conflict', 'This conversation reached 20 turns. Start a new one.', false, 'turn_limit');
    }
    const conversationId = request.conversation_id ?? newConversationId();
    if (text.includes('unscored')) {
      return respond(
        request,
        conversationId,
        'This answer cannot be scored: the fixture agent has no score store, so the panel says so before you click.',
        [],
        [],
        { feedbackEnabled: false }
      );
    }
    if (text.includes('explain') || text.includes('tell me about')) {
      return explain(request, conversationId);
    }
    if (text.includes('beginner') || text.includes('under') || text.includes('telescope')) {
      return recommendation(request, conversationId);
    }
    return respond(
      request,
      conversationId,
      'I can recommend telescopes and accessories from this shop, compare them against your budget, and add one to your cart. What are you hoping to observe?',
      [],
      []
    );
  },

  async addToCart(request) {
    await wait(RESPONSE_DELAY_MS);
    const product = FIXTURE_PRODUCTS[request.product_id];
    if (!product) {
      throw new AssistantError('unavailable', 'That product is not in the fixture catalog.', false);
    }
    return respond(
      request,
      request.conversation_id,
      `Added ${request.quantity} × ${product.name} to your cart.`,
      [product],
      ['add_to_cart'],
      { cartChanged: true }
    );
  },

  async submitFeedback(request) {
    await wait(300);
    if (request.trace_id.startsWith(UNSCORED_TRACE_PREFIX)) {
      return { saved: false, message: 'The fixture agent has no score store.' };
    }
    return { saved: true };
  },

  // Fixture conversations exist only in this browser, so every id is alive and bound to this shopper.
  async getConversation(conversationId) {
    await wait(300);
    return {
      conversation_id: conversationId,
      turns: 1,
      currency_code: 'USD',
      expires_at: new Date(Date.now() + 3_600_000).toISOString(),
    };
  },
};

export default FixtureTransport;

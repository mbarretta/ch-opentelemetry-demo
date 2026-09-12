// Native shopping assistant, run against the native stack (scripts/demo.py up) at the Cypress
// baseUrl. The agent runs in scripted mode: "A telescope for a beginner" is answered with a real
// catalog product, so every card, price, and cart count below comes from the live shop services.
// Each test starts with a fresh storefront session (Cypress clears localStorage between tests),
// so carts and conversations never leak from one test into the next.

import { getElementByField } from '../../utils/Cypress';
import { CypressFields } from '../../utils/enums/CypressFields';

// A scripted turn runs real tool calls through the agent; the storefront route allows 100 s.
const AGENT_TIMEOUT = 60000;
const LIVE_REGION = '[role="status"][aria-live="polite"]';
const REQUEST = 'A telescope for a beginner';

const field = (name: CypressFields) => `[data-cy="${name}"]`;
const composer = () => getElementByField(CypressFields.AssistantComposer).find('input');
const assistantMessages = () => getElementByField(CypressFields.AssistantMessage).filter('[data-from="assistant"]');
const userMessages = () => getElementByField(CypressFields.AssistantMessage).filter('[data-from="user"]');

const openPanel = () => {
  getElementByField(CypressFields.AssistantTrigger).click();
  getElementByField(CypressFields.AssistantPanel).should('exist');
};

describe('Shopping assistant', () => {
  beforeEach(() => {
    cy.visit('/');
    getElementByField(CypressFields.HomePage).should('exist');
  });

  it('recommends a product, adds it to the shared cart, and keeps the transcript and cart apart', () => {
    cy.intercept('POST', '/api/assistant/message*').as('message');
    cy.intercept('POST', '/api/assistant/action*').as('action');
    cy.intercept('GET', '/api/cart*').as('getCart');

    // Open from the header and ask for a recommendation.
    openPanel();
    composer().type(REQUEST);
    getElementByField(CypressFields.AssistantSend).click();
    cy.wait('@message', { timeout: AGENT_TIMEOUT }).its('response.statusCode').should('eq', 200);
    userMessages().should('have.length', 1).and('contain', REQUEST);
    assistantMessages().should('have.length', 1);

    // The card is built from product_refs: a catalog name, a formatted price, and a product link.
    getElementByField(CypressFields.AssistantProductCard).should('have.length.at.least', 1);
    getElementByField(CypressFields.AssistantProductCard).first().as('card');
    cy.get('@card').find(field(CypressFields.ProductPrice)).invoke('text').should('match', /\d/);
    cy.get('@card').find('p').first().invoke('text').should('not.be.empty').as('productName');
    cy.get('@card').find('a[href^="/product/"]').invoke('attr', 'href').as('productHref');

    // Add to cart goes through the agent action; the header count follows the shared cart.
    getElementByField(CypressFields.CartItemCount).should('not.exist');
    cy.get('@card').find(field(CypressFields.AssistantCardAddToCart)).click();
    cy.wait('@action', { timeout: AGENT_TIMEOUT }).its('response.body.cart_changed').should('eq', true);
    cy.wait('@getCart', { timeout: 10000 });
    cy.get(field(CypressFields.CartItemCount), { timeout: 10000 }).should('contain', '1');

    // The cart action is confirmed as its own assistant entry.
    assistantMessages().should('have.length', 2).last().should('contain', 'Cart updated');

    // Closing and reopening keeps the transcript: conversation state lives above the pages.
    getElementByField(CypressFields.AssistantClose).click();
    getElementByField(CypressFields.AssistantPanel).should('not.exist');
    openPanel();
    userMessages().should('have.length', 1);
    assistantMessages().should('have.length', 2);
    getElementByField(CypressFields.AssistantProductCard).should('exist');

    // The cart page lists the same item, reached the way the released Checkout spec does.
    getElementByField(CypressFields.CartIcon).click({ force: true });
    getElementByField(CypressFields.CartGoToShopping).click();
    cy.location('pathname').should('eq', '/cart');
    cy.get<string>('@productHref').then(href => cy.get(`a[href="${href}"]`).should('exist'));
    cy.get<string>('@productName').then(name => cy.contains('p', name).should('exist'));

    // Client-side navigation kept the panel open; a new conversation clears the transcript only.
    getElementByField(CypressFields.AssistantPanel).should('exist');
    getElementByField(CypressFields.AssistantNewConversation).click();
    getElementByField(CypressFields.AssistantMessage).should('not.exist');
    getElementByField(CypressFields.AssistantSuggestion).should('have.length', 3);
    getElementByField(CypressFields.CartItemCount).should('contain', '1');
    cy.get<string>('@productName').then(name => cy.contains('p', name).should('exist'));
  });

  // Keyboard and screen-reader behavior on the desktop panel (non-modal) and the mobile dialog
  // (modal, focus trapped). Cypress has no real key-press API: Enter on a focused button is the
  // browser's own click default action, so the focused trigger is activated with click(); Enter
  // and Escape are typed into the composer, and the focus trap is exercised by dispatching the
  // Tab keydown its handler listens for.
  const viewports: Array<{ label: string; width: number; height: number; modal: boolean }> = [
    { label: 'desktop panel', width: 1440, height: 900, modal: false },
    { label: 'mobile dialog', width: 390, height: 844, modal: true },
  ];

  viewports.forEach(({ label, width, height, modal }) => {
    it(`is keyboard operable as a ${label} at ${width} px`, () => {
      cy.viewport(width, height);
      // Hold the agent's real answer back long enough for the pending announcement to be read.
      cy.intercept('POST', '/api/assistant/message*', request => {
        request.on('response', response => {
          response.setDelay(1500);
        });
      }).as('message');

      // Activating the focused header control opens the panel and moves focus into it.
      getElementByField(CypressFields.AssistantTrigger).focus();
      cy.focused().should('have.attr', 'data-cy', CypressFields.AssistantTrigger).click();
      getElementByField(CypressFields.AssistantPanel).should('exist').and('have.attr', 'aria-modal', String(modal));
      cy.focused().should('have.attr', 'data-cy', CypressFields.AssistantClose);

      // Enter in the composer submits; the live region announces pending, then completion.
      composer().type(`${REQUEST}{enter}`);
      cy.get(LIVE_REGION).should('contain', 'Looking up products');
      getElementByField(CypressFields.AssistantSend).should('be.disabled');
      cy.wait('@message', { timeout: AGENT_TIMEOUT });
      cy.get(LIVE_REGION).should('contain', 'The assistant replied');
      assistantMessages().should('have.length', 1);

      if (modal) {
        // Tab from the last control wraps to the first one, and Shift+Tab wraps back.
        getElementByField(CypressFields.AssistantSend).focus().trigger('keydown', { key: 'Tab' });
        cy.focused().should('have.attr', 'data-cy', CypressFields.AssistantNewConversation);
        cy.focused().trigger('keydown', { key: 'Tab', shiftKey: true });
        cy.focused().should('have.attr', 'data-cy', CypressFields.AssistantSend);
      }

      // Escape closes the panel and returns focus to the control that opened it.
      composer().type('{esc}');
      getElementByField(CypressFields.AssistantPanel).should('not.exist');
      cy.focused().should('have.attr', 'data-cy', CypressFields.AssistantTrigger);
    });
  });
});

export {};

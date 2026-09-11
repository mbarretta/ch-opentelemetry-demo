import { KeyboardEvent, useCallback, useEffect, useRef, useSyncExternalStore } from 'react';
import { useAssistant } from '../../providers/Assistant.provider';
import Theme from '../../styles/Theme';
import { CypressFields } from '../../utils/enums/CypressFields';
import * as S from './AssistantPanel.styled';
import Composer from './Composer';
import Transcript from './Transcript';

export const ASSISTANT_PANEL_ID = 'assistant-panel';
const TITLE_ID = 'assistant-panel-title';

// The same breakpoint the theme uses for every other layout switch, as a matchMedia query.
const DESKTOP_QUERY = Theme.breakpoints.desktop.replace(/^@media\s*/, '');

const subscribeToViewport = (onChange: () => void) => {
  const media = window.matchMedia(DESKTOP_QUERY);
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
};

const useIsDesktop = () =>
  useSyncExternalStore(
    subscribeToViewport,
    () => window.matchMedia(DESKTOP_QUERY).matches,
    () => true
  );

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

// The storefront header rendered by Layout (the panel has its own <header> inside).
const storefrontHeader = () => document.querySelector<HTMLElement>('#__next > header') ?? document.querySelector('header');

const focusableWithin = (root: HTMLElement) =>
  Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(element => element.getClientRects().length > 0);

interface IPanelProps {
  isDesktop: boolean;
  onClose(): void;
}

const OpenPanel = ({ isDesktop, onClose }: IPanelProps) => {
  const panelRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  // Opening moves focus into the panel; the close control is the first stop in the Tab order.
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  // Start below the storefront header while it is on screen so the logo and cart stay visible;
  // once the header scrolls away the panel takes the full height.
  useEffect(() => {
    const panel = panelRef.current;
    const header = storefrontHeader();
    if (!panel || !header) return;
    const place = () => {
      panel.style.top = `${Math.max(0, header.getBoundingClientRect().bottom)}px`;
    };
    place();
    window.addEventListener('scroll', place, { passive: true });
    window.addEventListener('resize', place);
    return () => {
      window.removeEventListener('scroll', place);
      window.removeEventListener('resize', place);
    };
  }, []);

  // As a modal dialog the page behind must not scroll.
  useEffect(() => {
    if (isDesktop) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, [isDesktop]);

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }
    // The desktop panel is non-modal: Tab leaves it into the page. The mobile dialog traps focus.
    if (event.key !== 'Tab' || isDesktop || !panelRef.current) return;
    const focusable = focusableWithin(panelRef.current);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <S.Panel
      ref={panelRef}
      id={ASSISTANT_PANEL_ID}
      role="dialog"
      aria-modal={!isDesktop}
      aria-labelledby={TITLE_ID}
      data-cy={CypressFields.AssistantPanel}
      onKeyDown={onKeyDown}
    >
      <S.PanelHeader>
        <S.Title id={TITLE_ID}>Shopping assistant</S.Title>
        <S.CloseButton
          ref={closeRef}
          type="button"
          aria-label="Close assistant"
          data-cy={CypressFields.AssistantClose}
          onClick={onClose}
        >
          <span aria-hidden="true">×</span>
        </S.CloseButton>
      </S.PanelHeader>
      <Transcript />
      <Composer />
    </S.Panel>
  );
};

// Rendered once by Layout. The live region stays mounted while the panel is closed so a status
// change right after opening is still announced.
const AssistantPanel = () => {
  const { isOpen, close, openerRef, announcement } = useAssistant();
  const isDesktop = useIsDesktop();

  const onClose = useCallback(() => {
    const opener = openerRef.current;
    close();
    opener?.focus();
  }, [close, openerRef]);

  return (
    <>
      {isOpen ? <OpenPanel isDesktop={isDesktop} onClose={onClose} /> : null}
      <S.VisuallyHidden role="status" aria-live="polite" aria-atomic="true">
        {announcement}
      </S.VisuallyHidden>
    </>
  );
};

export default AssistantPanel;

// Styles for the shopping assistant. Colors, breakpoints, sizes, and weights come from the shop
// theme (styles/Theme.ts); the only literal colors are shadows and translucent overlays.

import RouterLink from 'next/link';
import Image from 'next/image';
import styled, { css } from 'styled-components';
import Button from '../Button';

export const PANEL_WIDTH = '420px';

// Visible on white (yellow ring) and on the blue buttons (blue halo around the ring).
export const focusRing = css`
  &:focus-visible {
    outline: 3px solid ${({ theme }) => theme.colors.otelYellow};
    outline-offset: 2px;
    box-shadow: 0 0 0 6px ${({ theme }) => theme.colors.otelBlue};
  }
`;

export const visuallyHidden = css`
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
`;

export const VisuallyHidden = styled.span`
  ${visuallyHidden}
`;

// Compact variant of the shop's Button for use inside the panel.
export const PanelButton = styled(Button)`
  height: 40px;
  padding: 0 14px;
  font-size: ${({ theme }) => theme.sizes.dSmall};
  line-height: 1;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  white-space: nowrap;
  ${focusRing}

  &:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
`;

/* Header trigger ------------------------------------------------------------------------- */

export const Trigger = styled.button`
  display: inline-flex;
  align-items: center;
  gap: 8px;
  height: 40px;
  margin-left: 12px;
  padding: 0 10px;
  align-self: center;
  border: 1px solid ${({ theme }) => theme.colors.otelBlue};
  border-radius: 10px;
  background: ${({ theme }) => theme.colors.white};
  color: ${({ theme }) => theme.colors.otelBlue};
  font-size: ${({ theme }) => theme.sizes.dSmall};
  font-weight: ${({ theme }) => theme.fonts.semiBold};
  cursor: pointer;
  flex-shrink: 0;
  ${focusRing}

  &[aria-expanded='true'] {
    background: ${({ theme }) => theme.colors.otelBlue};
    color: ${({ theme }) => theme.colors.white};
  }

  ${({ theme }) => theme.breakpoints.desktop} {
    margin-left: 16px;
    padding: 0 14px;
  }
`;

export const TriggerIcon = styled.svg`
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
`;

// The text label only fits beside the logo, currency switcher, and cart at desktop widths.
export const TriggerLabel = styled.span`
  ${visuallyHidden}

  ${({ theme }) => theme.breakpoints.desktop} {
    position: static;
    width: auto;
    height: auto;
    margin: 0;
    overflow: visible;
    clip: auto;
    color: inherit;
  }
`;

/* Panel shell ----------------------------------------------------------------------------- */

// Full-width dialog below the desktop breakpoint; a right-side panel above it. Fixed
// positioning keeps it out of the document flow so the page never scrolls horizontally. The
// top edge is set inline to the storefront header's bottom (see AssistantPanel.tsx) so the
// logo and cart stay visible while the header is on screen.
export const Panel = styled.section`
  position: fixed;
  inset: 0;
  z-index: 999;
  display: flex;
  flex-direction: column;
  width: 100%;
  background: ${({ theme }) => theme.colors.white};
  color: ${({ theme }) => theme.colors.textGray};
  font-weight: ${({ theme }) => theme.fonts.regular};

  ${({ theme }) => theme.breakpoints.desktop} {
    inset: 0 0 0 auto;
    width: min(${PANEL_WIDTH}, 100vw);
    border-left: 1px solid ${({ theme }) => theme.colors.lightBorderGray};
    box-shadow: -8px 0 24px rgba(0, 0, 0, 0.12);
  }
`;

export const PanelHeader = styled.header`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 20px;
  border-bottom: 1px solid ${({ theme }) => theme.colors.lightBorderGray};
  flex-shrink: 0;
`;

export const Title = styled.h2`
  margin: 0;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: ${({ theme }) => theme.sizes.dMedium};
  font-weight: ${({ theme }) => theme.fonts.bold};
  color: ${({ theme }) => theme.colors.textGray};
`;

export const HeaderActions = styled.div`
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
`;

// Compact so the title, this control, and Close share the panel width without truncation.
export const HeaderButton = styled(PanelButton).attrs({ $type: 'secondary' })`
  height: 32px;
  padding: 0 10px;
  font-size: ${({ theme }) => theme.sizes.mSmall};
  border-color: ${({ theme }) => theme.colors.lightBorderGray};
`;

export const CloseButton = styled.button`
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border: 1px solid ${({ theme }) => theme.colors.lightBorderGray};
  border-radius: 8px;
  background: ${({ theme }) => theme.colors.white};
  color: ${({ theme }) => theme.colors.textGray};
  font-size: ${({ theme }) => theme.sizes.mLarge};
  line-height: 1;
  cursor: pointer;
  ${focusRing}
`;

/* Transcript ------------------------------------------------------------------------------ */

export const Transcript = styled.section`
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  ${focusRing}
`;

export const EmptyState = styled.div`
  display: flex;
  flex-direction: column;
  gap: 14px;
`;

export const EmptyTitle = styled.h3`
  margin: 8px 0 0;
  font-size: ${({ theme }) => theme.sizes.mLarge};
  font-weight: ${({ theme }) => theme.fonts.bold};
  color: ${({ theme }) => theme.colors.textGray};
`;

export const EmptyText = styled.p`
  margin: 0;
  font-size: ${({ theme }) => theme.sizes.mMedium};
  font-weight: ${({ theme }) => theme.fonts.regular};
  color: ${({ theme }) => theme.colors.textGray};
`;

export const Suggestions = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
`;

export const Suggestion = styled(PanelButton).attrs({ $type: 'secondary' })`
  height: 36px;
  padding: 0 12px;
  font-size: ${({ theme }) => theme.sizes.mMedium};
  font-weight: ${({ theme }) => theme.fonts.semiBold};
  border-color: ${({ theme }) => theme.colors.lightBorderGray};
  border-radius: 18px;
  white-space: normal;
  text-align: left;
`;

// The shop's Input renders a label row with a 24px bottom margin meant for forms; the panel
// needs a tighter row.
export const BudgetRow = styled.div`
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: end;
  gap: 12px;

  > div {
    margin-bottom: 0;
  }

  p {
    margin-bottom: 6px;
    font-size: ${({ theme }) => theme.sizes.mMedium};
    font-weight: ${({ theme }) => theme.fonts.semiBold};
  }

  input {
    width: 100%;
    padding: 10px 12px;
    font-size: ${({ theme }) => theme.sizes.dSmall};
    ${focusRing}
  }
`;

export const BudgetSummary = styled.p`
  margin: 0;
  padding-bottom: 12px;
  font-size: ${({ theme }) => theme.sizes.mMedium};
  font-weight: ${({ theme }) => theme.fonts.regular};
  color: ${({ theme }) => theme.colors.textGray};

  strong {
    font-weight: ${({ theme }) => theme.fonts.bold};
  }
`;

export const Message = styled.article<{ $from: 'user' | 'assistant' }>`
  max-width: 92%;
  padding: 10px 14px;
  border-radius: 12px;
  font-size: ${({ theme }) => theme.sizes.dSmall};
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;

  ${({ $from, theme }) =>
    $from === 'user'
      ? css`
          align-self: flex-end;
          background: ${theme.colors.otelBlue};
          color: ${theme.colors.white};
          border-bottom-right-radius: 4px;

          * {
            color: ${theme.colors.white};
          }
        `
      : css`
          align-self: flex-start;
          background: ${theme.colors.backgroundGray};
          color: ${theme.colors.textGray};
          border-bottom-left-radius: 4px;
        `}
`;

export const MessageMeta = styled.p`
  margin: 0 0 4px;
  font-size: ${({ theme }) => theme.sizes.mSmall};
  font-weight: ${({ theme }) => theme.fonts.semiBold};
`;

export const Cards = styled.div`
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
`;

export const Status = styled.p`
  margin: 0;
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-radius: 12px;
  background: ${({ theme }) => theme.colors.backgroundGray};
  font-size: ${({ theme }) => theme.sizes.dSmall};
  font-weight: ${({ theme }) => theme.fonts.semiBold};
  color: ${({ theme }) => theme.colors.textGray};
`;

export const StatusDot = styled.span`
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: ${({ theme }) => theme.colors.otelBlue};
  animation: assistant-pulse 1.2s ease-in-out infinite;

  @keyframes assistant-pulse {
    0%,
    100% {
      opacity: 0.35;
    }
    50% {
      opacity: 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    animation: none;
  }
`;

export const ErrorBox = styled.div`
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid ${({ theme }) => theme.colors.lightBorderGray};
  border-left: 4px solid ${({ theme }) => theme.colors.otelRed};
  border-radius: 8px;
  font-size: ${({ theme }) => theme.sizes.dSmall};
  color: ${({ theme }) => theme.colors.textGray};

  p {
    margin: 0;
  }
`;

// The panel's own remark about the conversation (it expired, an action was confirmed).
export const Notice = styled.p`
  margin: 0;
  padding: 10px 14px;
  border: 1px dashed ${({ theme }) => theme.colors.lightBorderGray};
  border-radius: 8px;
  font-size: ${({ theme }) => theme.sizes.mMedium};
  font-weight: ${({ theme }) => theme.fonts.regular};
  color: ${({ theme }) => theme.colors.textGray};
`;

// A cart action whose outcome is not known yet: a warning, not a failure.
export const UncertainBox = styled(ErrorBox)`
  border-left-color: ${({ theme }) => theme.colors.otelYellow};
`;

export const ButtonRow = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
`;

/* Product card ---------------------------------------------------------------------------- */

export const Card = styled.div`
  display: grid;
  grid-template-columns: 72px 1fr;
  gap: 12px;
  padding: 10px;
  border: 1px solid ${({ theme }) => theme.colors.lightBorderGray};
  border-radius: 10px;
  background: ${({ theme }) => theme.colors.white};
`;

export const CardImage = styled(Image).attrs({ width: 72, height: 72 })`
  width: 72px;
  height: 72px;
  border-radius: 6px;
  object-fit: contain;
  background: ${({ theme }) => theme.colors.backgroundGray};
`;

export const CardBody = styled.div`
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
`;

export const CardName = styled.p`
  margin: 0;
  font-size: ${({ theme }) => theme.sizes.dSmall};
  font-weight: ${({ theme }) => theme.fonts.semiBold};
  color: ${({ theme }) => theme.colors.textGray};
`;

export const CardPrice = styled.p`
  margin: 0;
  font-size: ${({ theme }) => theme.sizes.dSmall};
  font-weight: ${({ theme }) => theme.fonts.bold};
`;

export const CardActions = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
`;

export const CardLink = styled(RouterLink)`
  display: inline-flex;
  align-items: center;
  height: 36px;
  padding: 0 12px;
  border: 1px solid ${({ theme }) => theme.colors.otelBlue};
  border-radius: 8px;
  color: ${({ theme }) => theme.colors.otelBlue};
  font-size: ${({ theme }) => theme.sizes.mMedium};
  font-weight: ${({ theme }) => theme.fonts.semiBold};
  text-decoration: none;
  ${focusRing}
`;

export const CardButton = styled(PanelButton)`
  height: 36px;
  padding: 0 12px;
  font-size: ${({ theme }) => theme.sizes.mMedium};
`;

/* Feedback and demo details --------------------------------------------------------------- */

export const FeedbackRow = styled.div`
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 8px;
  font-size: ${({ theme }) => theme.sizes.mSmall};
  font-weight: ${({ theme }) => theme.fonts.regular};
  color: ${({ theme }) => theme.colors.textGray};
`;

export const FeedbackButton = styled.button`
  height: 28px;
  padding: 0 10px;
  border: 1px solid ${({ theme }) => theme.colors.lightBorderGray};
  border-radius: 14px;
  background: ${({ theme }) => theme.colors.white};
  color: ${({ theme }) => theme.colors.otelBlue};
  font-size: ${({ theme }) => theme.sizes.mSmall};
  font-weight: ${({ theme }) => theme.fonts.semiBold};
  cursor: pointer;
  ${focusRing}

  &:disabled {
    cursor: default;
  }
`;

export const Details = styled.details`
  border: 1px solid ${({ theme }) => theme.colors.lightBorderGray};
  border-radius: 8px;
  padding: 8px 12px;
  font-size: ${({ theme }) => theme.sizes.mMedium};

  summary {
    cursor: pointer;
    font-weight: ${({ theme }) => theme.fonts.semiBold};
    color: ${({ theme }) => theme.colors.otelBlue};
    ${focusRing}
  }

  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 12px;
    margin: 10px 0 0;
    font-weight: ${({ theme }) => theme.fonts.regular};
  }

  dt {
    font-weight: ${({ theme }) => theme.fonts.semiBold};
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  a {
    color: ${({ theme }) => theme.colors.otelBlue};
  }
`;

/* Composer -------------------------------------------------------------------------------- */

// Pinned to the bottom; the safe-area padding keeps it above the home indicator on phones.
export const Composer = styled.form`
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 20px calc(12px + env(safe-area-inset-bottom));
  border-top: 1px solid ${({ theme }) => theme.colors.lightBorderGray};
  background: ${({ theme }) => theme.colors.white};

  // The shop Input's row spacing and label sizing, tightened for a chat composer.
  > div > div {
    margin-bottom: 0;
    flex: 1 1 auto;
  }

  > div > div > p {
    ${visuallyHidden}
  }

  input {
    width: 100%;
    padding: 11px 14px;
    font-size: ${({ theme }) => theme.sizes.dSmall};
    ${focusRing}

    &:disabled {
      opacity: 0.6;
    }
  }
`;

export const ComposerRow = styled.div`
  display: flex;
  align-items: stretch;
  gap: 10px;
`;

export const ContextChip = styled.span`
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  height: 32px;
  padding: 0 4px 0 12px;
  border: 1px solid ${({ theme }) => theme.colors.otelYellow};
  border-radius: 16px;
  background: ${({ theme }) => theme.colors.white};
  font-size: ${({ theme }) => theme.sizes.mSmall};
  font-weight: ${({ theme }) => theme.fonts.semiBold};
  color: ${({ theme }) => theme.colors.textGray};
  align-self: flex-start;

  span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
`;

export const ChipRemove = styled.button`
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: ${({ theme }) => theme.colors.textGray};
  font-size: ${({ theme }) => theme.sizes.dSmall};
  line-height: 1;
  cursor: pointer;
  ${focusRing}
`;

/* Product page control -------------------------------------------------------------------- */

export const AskButton = styled(Button).attrs({ $type: 'secondary' })`
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  width: 100%;
  font-size: ${({ theme }) => theme.sizes.dSmall};
  font-weight: ${({ theme }) => theme.fonts.regular};
  ${focusRing}

  ${({ theme }) => theme.breakpoints.desktop} {
    font-size: ${({ theme }) => theme.sizes.dMedium};
    width: 220px;
  }
`;

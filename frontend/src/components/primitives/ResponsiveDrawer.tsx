import { JSX, ComponentChildren } from 'preact';
import { useEffect, useRef } from 'preact/hooks';
import { createPortal } from 'preact/compat';

export interface ResponsiveDrawerProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: ComponentChildren;
  isOverlay: boolean;
  triggerId?: string;
  triggerRef?: { current: HTMLElement | null };
  width?: string;
  drawerId: string;
  overlayClassName?: string;
  inertTargetSelector?: string;
}

export function ResponsiveDrawer({
  isOpen,
  onClose,
  title,
  children,
  isOverlay,
  triggerId,
  triggerRef,
  width = '280px',
  drawerId,
  overlayClassName = '',
  inertTargetSelector = '.app-layout',
}: ResponsiveDrawerProps): JSX.Element | null {
  const drawerRef = useRef<HTMLDivElement>(null);
  const activeElBeforeOpenRef = useRef<HTMLElement | null>(null);

  const removeInert = () => {
    if (inertTargetSelector) {
      Array.from(document.querySelectorAll<HTMLElement>(inertTargetSelector)).forEach((el) =>
        el.removeAttribute('inert')
      );
    }
  };

  const restoreFocus = () => {
    removeInert();
    setTimeout(() => {
      // Tier 1: triggerId in DOM
      if (triggerId) {
        const btn = document.getElementById(triggerId);
        if (btn && document.body.contains(btn)) {
          btn.focus();
          return;
        }
      }
      // Tier 2: triggerRef in DOM
      if (triggerRef?.current && document.body.contains(triggerRef.current)) {
        triggerRef.current.focus();
        return;
      }
      // Tier 3: saved activeElement before open in DOM
      const savedActive = activeElBeforeOpenRef.current;
      if (savedActive && document.body.contains(savedActive)) {
        savedActive.focus();
        return;
      }
      // Tier 4: main workspace or main content container
      const mainContainer = document.querySelector<HTMLElement>('.app-workspace, main, #main-content');
      if (mainContainer && typeof mainContainer.focus === 'function') {
        if (!mainContainer.hasAttribute('tabindex')) {
          mainContainer.setAttribute('tabindex', '-1');
        }
        mainContainer.focus();
      }
    }, 20);
  };

  useEffect(() => {
    if (!isOpen || !isOverlay) return;

    if (!activeElBeforeOpenRef.current && document.activeElement instanceof HTMLElement) {
      activeElBeforeOpenRef.current = document.activeElement;
    }

    // 1. Lock Body Scroll on Overlay Mode
    const origOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    // 2. Apply inert to background target if provided
    let inertElements: HTMLElement[] = [];
    if (inertTargetSelector) {
      inertElements = Array.from(document.querySelectorAll<HTMLElement>(inertTargetSelector));
      inertElements.forEach((el) => el.setAttribute('inert', ''));
    }

    // 3. Focus Management & Keyboard Trap
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
        restoreFocus();
      } else if (e.key === 'Tab' && drawerRef.current) {
        // Focus Trap Inside Drawer
        const focusables = drawerRef.current.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];

        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };

    window.addEventListener('keydown', handleKeyDown);

    // Initial Focus
    const focusTimer = setTimeout(() => {
      if (drawerRef.current) {
        const closeBtn = drawerRef.current.querySelector<HTMLElement>('.drawer-close-btn');
        if (closeBtn) closeBtn.focus();
      }
    }, 0);

    return () => {
      clearTimeout(focusTimer);
      document.body.style.overflow = origOverflow;
      window.removeEventListener('keydown', handleKeyDown);
      inertElements.forEach((el) => el.removeAttribute('inert'));
      restoreFocus();
      setTimeout(() => {
        activeElBeforeOpenRef.current = null;
      }, 50);
    };
  }, [isOpen, isOverlay, onClose, triggerId, triggerRef, inertTargetSelector]);

  // Desktop Inline Mode - always renders children directly without dialog or open state dependency
  if (!isOverlay) {
    return <>{children}</>;
  }

  if (!isOpen) return null;

  // Overlay Mode - Portaled directly to document.body outside .app-layout
  const overlayNode = (
    <div
      class={`drawer-overlay ${overlayClassName}`}
      onClick={() => {
        onClose();
        restoreFocus();
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby={`${drawerId}-title`}
      id={drawerId}
    >
      <div
        class="drawer-card"
        style={{ width, maxWidth: '80vw' }}
        onClick={(e) => e.stopPropagation()}
        ref={drawerRef}
      >
        <div class="drawer-header">
          <h3 id={`${drawerId}-title`} class="drawer-title">
            {title}
          </h3>
          <button
            class="drawer-close-btn"
            onClick={() => {
              onClose();
              restoreFocus();
            }}
            aria-label="Close drawer"
          >
            ✕
          </button>
        </div>
        <div class="drawer-body">{children}</div>
      </div>
    </div>
  );

  return typeof document !== 'undefined' && document.body
    ? createPortal(overlayNode, document.body)
    : overlayNode;
}

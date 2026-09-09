import { JSX, ComponentChildren } from 'preact';
import { useEffect, useRef } from 'preact/hooks';

interface DrawerProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: ComponentChildren;
  width?: string;
}

export function Drawer({ isOpen, onClose, title, children, width = '420px' }: DrawerProps): JSX.Element | null {
  const drawerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };

    window.addEventListener('keydown', handleKeyDown);

    return () => {
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div class="drawer-overlay" onClick={onClose} role="dialog" aria-modal="true" aria-labelledby="drawer-title">
      <div
        class="drawer-card"
        style={{ width }}
        onClick={(e) => e.stopPropagation()}
        ref={drawerRef}
      >
        <div class="drawer-header">
          <h3 id="drawer-title" class="drawer-title">{title}</h3>
          <button class="drawer-close-btn" onClick={onClose} aria-label="Close inspector">
            ✕
          </button>
        </div>
        <div class="drawer-body">{children}</div>
      </div>
    </div>
  );
}

import { describe, it, expect } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/preact';
import { Modal } from '../src/components/primitives/Modal';
import { Drawer } from '../src/components/primitives/Drawer';
import { ConfirmDialog, promptConfirm, confirmSignal } from '../src/components/primitives/ConfirmDialog';

describe('Accessibility & Keyboard Navigation (a11y)', () => {
  it('Modal has correct role, aria-modal, and triggers close on Escape key', () => {
    let closed = false;
    const { getByRole } = render(
      <Modal isOpen={true} onClose={() => { closed = true; }} title="Accessible Dialog">
        <div>Content</div>
      </Modal>
    );

    const dialog = getByRole('dialog');
    expect(dialog).toBeDefined();
    expect(dialog.getAttribute('aria-modal')).toBe('true');

    // Simulate Escape key press
    fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' });
    expect(closed).toBe(true);
  });

  it('Drawer has correct role, aria-modal, and responds to Escape key', () => {
    let closed = false;
    const { getByRole } = render(
      <Drawer isOpen={true} onClose={() => { closed = true; }} title="Inspector">
        <div>Drawer Body</div>
      </Drawer>
    );

    const drawer = getByRole('dialog');
    expect(drawer).toBeDefined();
    expect(drawer.getAttribute('aria-modal')).toBe('true');

    fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' });
    expect(closed).toBe(true);
  });

  it('ConfirmDialog safely cancels without errors', async () => {
    const { getByText } = render(<ConfirmDialog />);

    // Trigger prompt
    let promiseResult: any = null;
    promptConfirm({
      title: 'Delete Asset',
      message: 'Confirm asset destruction?',
      confirmLabel: 'Destroy',
      cancelLabel: 'Abort',
      isDestructive: true,
    }).then((res) => {
      promiseResult = res;
    });

    await waitFor(() => expect(confirmSignal.value.isOpen).toBe(true));

    const cancelBtn = getByText('Abort');
    fireEvent.click(cancelBtn);

    await waitFor(() => expect(promiseResult).not.toBeNull());
    expect(promiseResult.confirmed).toBe(false);
  });
});

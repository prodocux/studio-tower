import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/preact';
import { ToastContainer } from '../src/components/primitives/ToastContainer';
import { showToast, toastsSignal } from '../src/services/toast';
import { Modal } from '../src/components/primitives/Modal';
import { Drawer } from '../src/components/primitives/Drawer';

describe('UI Primitives Components', () => {
  it('renders ToastContainer correctly when toasts are present', () => {
    toastsSignal.value = [];
    const { queryByText } = render(<ToastContainer />);
    expect(queryByText('Script uploaded successfully')).toBeNull();

    showToast('Script uploaded successfully', 'success', 0);
    const { getByText } = render(<ToastContainer />);
    expect(getByText('Script uploaded successfully')).toBeDefined();
  });

  it('renders Modal and responds to close trigger', () => {
    let closed = false;
    const { getByText } = render(
      <Modal isOpen={true} onClose={() => { closed = true; }} title="Test Dialog">
        <div>Modal Content Body</div>
      </Modal>
    );

    expect(getByText('Test Dialog')).toBeDefined();
    expect(getByText('Modal Content Body')).toBeDefined();

    const closeBtn = getByText('✕');
    fireEvent.click(closeBtn);
    expect(closed).toBe(true);
  });

  it('renders Drawer when open', () => {
    let closed = false;
    const { getByText } = render(
      <Drawer isOpen={true} onClose={() => { closed = true; }} title="Inspector Panel">
        <div>Inspector Details</div>
      </Drawer>
    );

    expect(getByText('Inspector Panel')).toBeDefined();
    expect(getByText('Inspector Details')).toBeDefined();
  });
});

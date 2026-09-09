import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/preact';
import { ActionProposalCard } from '../src/components/ActionProposalCard';
import { ActionProposal, Message } from '../src/types';
import { api } from '../src/services/api';
import { messagesSignal, activeViewSignal } from '../src/services/store';

describe('ActionProposalCard Component', () => {
  it('renders ActionProposalCard and executes confirmation flow', async () => {
    const proposal: ActionProposal = {
      action_id: 'act_12345_v1_9999999999_abcdef',
      action_type: 'create_deliverable',
      title: 'Generate Production Call Sheet',
      description: 'Auto-generate call sheet from Day 1 script scenes',
      space_id: 'sp_test_act',
      project_tag: 'general',
      user_id: 'u_test',
      source_file_ids: ['file_01'],
      generation_version: 1,
      expires_at: new Date(Date.now() + 120000).toISOString(),
    };

    const message: Message = {
      message_id: 'msg_prop_01',
      space_id: 'sp_test_act',
      sender_uid: 'agent_studiotower',
      sender_name: 'StudioTower Agent',
      role: 'agent',
      content: 'Here is your action proposal.',
      project_tag: 'general',
      proposed_action: proposal,
      created_at: new Date().toISOString(),
    };

    messagesSignal.value = [message];

    const confirmSpy = vi.spyOn(api, 'confirmAction').mockResolvedValue({
      status: 'confirmed',
      action_id: proposal.action_id,
      run_id: 'run_confirmed_999',
      message: {
        message_id: 'msg_ack_01',
        space_id: 'sp_test_act',
        sender_uid: 'agent_studiotower',
        sender_name: 'StudioTower Agent',
        role: 'agent',
        content: 'Action confirmed!',
        created_at: new Date().toISOString(),
      },
    });

    const { getByText } = render(
      <ActionProposalCard
        proposal={proposal}
        message={message}
        spaceId="sp_test_act"
      />
    );

    expect(getByText(/Action Proposal: Generate Production Call Sheet/)).toBeDefined();
    expect(getByText(/Auto-generate call sheet from Day 1 script scenes/)).toBeDefined();

    const confirmBtn = getByText('✓ Confirm & Execute');
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(confirmSpy).toHaveBeenCalledWith('sp_test_act', proposal.action_id);
      expect(getByText(/Action Confirmed/)).toBeDefined();
    });

    const viewRunsBtn = getByText('View Runs →');
    fireEvent.click(viewRunsBtn);
    expect(activeViewSignal.value).toBe('runs');
  });

  it('renders expired state when proposal is past expiration date', () => {
    const expiredProposal: ActionProposal = {
      action_id: 'act_expired_v1_0000_abcdef',
      action_type: 'create_deliverable',
      title: 'Expired Call Sheet',
      description: 'Expired proposal',
      space_id: 'sp_test_act',
      project_tag: 'general',
      user_id: 'u_test',
      source_file_ids: [],
      generation_version: 1,
      expires_at: new Date(Date.now() - 10000).toISOString(),
    };

    const message: Message = {
      message_id: 'msg_prop_02',
      space_id: 'sp_test_act',
      sender_uid: 'agent_studiotower',
      sender_name: 'StudioTower Agent',
      role: 'agent',
      content: 'Expired proposal message',
      project_tag: 'general',
      proposed_action: expiredProposal,
      created_at: new Date().toISOString(),
    };

    const { getByText, queryByText } = render(
      <ActionProposalCard
        proposal={expiredProposal}
        message={message}
        spaceId="sp_test_act"
      />
    );

    expect(getByText(/Action Proposal: Expired Call Sheet/)).toBeDefined();
    expect(getByText(/Proposal Expired/)).toBeDefined();
    expect(queryByText('✓ Confirm & Execute')).toBeNull();
  });
});

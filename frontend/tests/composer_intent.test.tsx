import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/preact';
import { ChatArea } from '../src/components/ChatArea';
import {
  composerIntentSignal,
  composerDraftSignal,
  composerContextFileIdsSignal,
  spaceContextSignal,
  messagesSignal,
  filesSignal,
  activeTagSignal,
} from '../src/services/store';
import { api } from '../src/services/api';

describe('ChatArea Intent and File Attachment Integration', () => {
  beforeEach(() => {
    composerIntentSignal.value = 'conversation';
    composerDraftSignal.value = '';
    composerContextFileIdsSignal.value = [];
    messagesSignal.value = [];
    filesSignal.value = [];
    activeTagSignal.value = 'all';
    vi.spyOn(api, 'getFileIngestionStatus').mockResolvedValue({
      file_id: 'file_doc_123',
      space_id: 'spc_test_intent_001',
      ingestion_status: 'ready',
      chunk_count: 5,
      extracted_pages: 2,
      has_ocr_gaps: false,
      ocr_gap_pages: [],
    } as any);
    spaceContextSignal.value = {
      space: {
        space_id: 'spc_test_intent_001',
        name: 'Intent Test Space',
        kind: 'agent_dm',
        created_by: 'alice_01',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      members: [],
      membership: {
        space_id: 'spc_test_intent_001',
        uid: 'alice_01',
        role: 'owner',
        joined_at: new Date().toISOString(),
      },
      capabilities: {
        can_manage_members: true,
        can_approve_runs: true,
        can_modify_tags: true,
        can_create_files: true,
      },
    } as any;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('automatically switches composerIntentSignal to document_qa when selecting a file', async () => {
    const { container } = render(<ChatArea />);
    expect(composerIntentSignal.value).toBe('conversation');

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(fileInput).toBeDefined();

    const dummyFile = new File(['Scene 1: Ext. Ocean'], 'screenplay.pdf', { type: 'application/pdf' });
    Object.defineProperty(fileInput, 'files', { value: [dummyFile] });
    fireEvent.change(fileInput);

    expect(composerIntentSignal.value).toBe('document_qa');
  });

  it('allows user to manually switch AI mode buttons', async () => {
    const { getByText } = render(<ChatArea />);
    expect(composerIntentSignal.value).toBe('conversation');

    const breakdownBtn = getByText('🎬 Run Breakdown');
    fireEvent.click(breakdownBtn);
    expect(composerIntentSignal.value).toBe('create_breakdown');

    const askDocBtn = getByText('📄 Ask Document');
    fireEvent.click(askDocBtn);
    expect(composerIntentSignal.value).toBe('document_qa');

    const askAiBtn = getByText('💬 Ask AI');
    fireEvent.click(askAiBtn);
    expect(composerIntentSignal.value).toBe('conversation');
  });

  it('passes resolved document_qa intent to chatWithAgent on send with context file', async () => {
    const chatSpy = vi.spyOn(api, 'chatWithAgent').mockResolvedValue({
      user_message: {
        message_id: 'msg_user_1',
        space_id: 'spc_test_intent_001',
        sender_uid: 'alice_01',
        sender_name: 'Alice',
        role: 'user',
        content: 'What happens in Act 1?',
        created_at: new Date().toISOString(),
      },
      agent_message: {
        message_id: 'msg_agent_1',
        space_id: 'spc_test_intent_001',
        sender_uid: 'agent_studiotower',
        sender_name: 'StudioTower Agent',
        role: 'agent',
        content: 'Act 1 introduces the rescue team.',
        created_at: new Date().toISOString(),
      },
    });

    composerContextFileIdsSignal.value = ['file_doc_123'];
    const { container } = render(<ChatArea />);

    const textInput = container.querySelector('#chat-input-field') as HTMLInputElement;
    fireEvent.input(textInput, { target: { value: 'What happens in Act 1?' } });

    const sendBtn = container.querySelector('.btn-send') as HTMLButtonElement;
    fireEvent.click(sendBtn);

    await new Promise((r) => setTimeout(r, 20));

    expect(chatSpy).toHaveBeenCalledWith(
      'spc_test_intent_001',
      'What happens in Act 1?',
      'general',
      [],
      expect.any(String),
      'conversation',
      ['file_doc_123'],
      null,
      expect.any(Object)
    );

    // Verify context file chip is preserved for follow-up questions
    expect(composerContextFileIdsSignal.value).toEqual(['file_doc_123']);

    chatSpy.mockRestore();
  });

  it('preserves draft and retains uploaded file ID on polling timeout without re-uploading on second send', async () => {
    vi.useFakeTimers();

    const uploadSpy = vi.spyOn(api, 'uploadFileWithProgress').mockResolvedValue({
      file_id: 'f_upload_timeout',
      space_id: 'spc_test_intent_001',
      filename: 'huge.pdf',
      uploaded_by: 'alice_01',
      upload_status: 'committed',
      ingestion_status: 'extracting',
      created_at: new Date().toISOString(),
    } as any);

    // Status API continuously returns extracting
    let statusCalls = 0;
    const statusSpy = vi.spyOn(api, 'getFileIngestionStatus').mockImplementation(async () => {
      statusCalls++;
      return {
        file_id: 'f_upload_timeout',
        space_id: 'spc_test_intent_001',
        ingestion_status: 'extracting',
        chunk_count: 0,
        extracted_pages: 0,
        has_ocr_gaps: false,
        ocr_gap_pages: [],
      } as any;
    });

    const chatSpy = vi.spyOn(api, 'chatWithAgent').mockResolvedValue({} as any);

    const { container } = render(<ChatArea />);
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const dummyFile = new File(['Scene 1'], 'huge.pdf', { type: 'application/pdf' });
    Object.defineProperty(fileInput, 'files', { value: [dummyFile] });
    fireEvent.change(fileInput);

    const textInput = container.querySelector('#chat-input-field') as HTMLInputElement;
    fireEvent.input(textInput, { target: { value: 'Analyze huge screenplay' } });

    const sendBtn = container.querySelector('.btn-send') as HTMLButtonElement;
    fireEvent.click(sendBtn);

    // Advance through poll ticks until timeout (90s cap)
    for (let i = 0; i < 91; i++) {
      await vi.advanceTimersByTimeAsync(1000);
    }

    // Chat must NOT be called on timeout
    expect(chatSpy).not.toHaveBeenCalled();
    expect(uploadSpy).toHaveBeenCalledTimes(1);

    // Verify context file IDs retained the uploaded file ID
    expect(composerContextFileIdsSignal.value).toContain('f_upload_timeout');

    // Simulate second send: status API now returns ready when queried by composer!
    statusSpy.mockResolvedValueOnce({
      file_id: 'f_upload_timeout',
      space_id: 'spc_test_intent_001',
      ingestion_status: 'ready',
      chunk_count: 10,
      extracted_pages: 5,
      has_ocr_gaps: false,
      ocr_gap_pages: [],
    } as any);

    fireEvent.input(textInput, { target: { value: 'Analyze huge screenplay again' } });
    fireEvent.click(sendBtn);

    await vi.advanceTimersByTimeAsync(50);

    // Second send should NOT call uploadFileWithProgress again, and chatWithAgent MUST be called!
    expect(uploadSpy).toHaveBeenCalledTimes(1);
    expect(chatSpy).toHaveBeenCalledTimes(1);

    uploadSpy.mockRestore();
    statusSpy.mockRestore();
    chatSpy.mockRestore();
    vi.useRealTimers();
  });

  it('cancels state mutations if user switches space during polling', async () => {
    vi.useFakeTimers();

    const uploadSpy = vi.spyOn(api, 'uploadFileWithProgress').mockResolvedValue({
      file_id: 'f_upload_nav',
      space_id: 'spc_test_intent_001',
      filename: 'screenplay.pdf',
      uploaded_by: 'alice_01',
      upload_status: 'committed',
      ingestion_status: 'extracting',
      created_at: new Date().toISOString(),
    } as any);

    const statusSpy = vi.spyOn(api, 'getFileIngestionStatus').mockResolvedValue({
      file_id: 'f_upload_nav',
      space_id: 'spc_test_intent_001',
      ingestion_status: 'extracting',
      chunk_count: 0,
      extracted_pages: 0,
      has_ocr_gaps: false,
      ocr_gap_pages: [],
    } as any);

    const chatSpy = vi.spyOn(api, 'chatWithAgent').mockResolvedValue({} as any);

    const { container } = render(<ChatArea />);
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const dummyFile = new File(['Scene 1'], 'screenplay.pdf', { type: 'application/pdf' });
    Object.defineProperty(fileInput, 'files', { value: [dummyFile] });
    fireEvent.change(fileInput);

    const sendBtn = container.querySelector('.btn-send') as HTMLButtonElement;
    fireEvent.click(sendBtn);

    // Switch Space while polling
    spaceContextSignal.value = {
      space: {
        space_id: 'spc_OTHER_space',
        name: 'Other Space',
        kind: 'agent_dm',
        created_by: 'alice_01',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      members: [],
      membership: { role: 'owner' },
      capabilities: {},
    } as any;

    // Advance timers
    await vi.advanceTimersByTimeAsync(3000);

    // Chat must NOT be called for old space
    expect(chatSpy).not.toHaveBeenCalled();

    uploadSpy.mockRestore();
    statusSpy.mockRestore();
    chatSpy.mockRestore();
    vi.useRealTimers();
  });

  it('invalidates deferred chat response when navigating A -> B -> A', async () => {
    let resolveChatPromise: (value: any) => void = () => {};
    const deferredChatPromise = new Promise((resolve) => {
      resolveChatPromise = resolve;
    });

    const chatSpy = vi.spyOn(api, 'chatWithAgent').mockReturnValue(deferredChatPromise as any);

    const { container } = render(<ChatArea />);
    const textInput = container.querySelector('#chat-input-field') as HTMLInputElement;
    fireEvent.input(textInput, { target: { value: 'Deferred question' } });

    const sendBtn = container.querySelector('.btn-send') as HTMLButtonElement;
    fireEvent.click(sendBtn);

    expect(messagesSignal.value.length).toBe(1);
    expect(messagesSignal.value[0].isSending).toBe(true);

    // Navigate to Space B
    spaceContextSignal.value = {
      space: {
        space_id: 'spc_SPACE_B',
        name: 'Space B',
        kind: 'agent_dm',
        created_by: 'alice_01',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      members: [],
      membership: { role: 'owner' },
      capabilities: {},
    } as any;

    // Navigate back to Space A
    spaceContextSignal.value = {
      space: {
        space_id: 'spc_test_intent_001',
        name: 'Intent Test Space',
        kind: 'agent_dm',
        created_by: 'alice_01',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      members: [],
      membership: { role: 'owner' },
      capabilities: {},
    } as any;
    messagesSignal.value = []; // Space A fresh messages list

    // Now resolve the deferred response from the old request
    resolveChatPromise({
      user_message: {
        message_id: 'msg_stale_user',
        space_id: 'spc_test_intent_001',
        sender_uid: 'alice_01',
        role: 'user',
        content: 'Deferred question',
      },
      agent_message: {
        message_id: 'msg_stale_agent',
        space_id: 'spc_test_intent_001',
        sender_uid: 'agent',
        role: 'agent',
        content: 'Stale answer from previous session',
      },
    });

    await new Promise((r) => setTimeout(r, 50));

    // Stale response must NOT be accepted into messagesSignal!
    expect(messagesSignal.value.length).toBe(0);

    chatSpy.mockRestore();
  });

  it('invalidates deferred chat response when switching tag within same space', async () => {
    activeTagSignal.value = 'general';

    let resolveChatPromise: (value: any) => void = () => {};
    const deferredChatPromise = new Promise((resolve) => {
      resolveChatPromise = resolve;
    });

    const chatSpy = vi.spyOn(api, 'chatWithAgent').mockReturnValue(deferredChatPromise as any);

    const { container } = render(<ChatArea />);
    const textInput = container.querySelector('#chat-input-field') as HTMLInputElement;
    fireEvent.input(textInput, { target: { value: 'Tag question' } });

    const sendBtn = container.querySelector('.btn-send') as HTMLButtonElement;
    fireEvent.click(sendBtn);

    // Switch tag from general to vfx
    activeTagSignal.value = 'vfx';
    messagesSignal.value = [];

    // Resolve deferred response for old general tag
    resolveChatPromise({
      user_message: {
        message_id: 'msg_stale_tag_user',
        space_id: 'spc_test_intent_001',
        sender_uid: 'alice_01',
        role: 'user',
        content: 'Tag question',
      },
      agent_message: {
        message_id: 'msg_stale_tag_agent',
        space_id: 'spc_test_intent_001',
        sender_uid: 'agent',
        role: 'agent',
        content: 'Stale general answer',
      },
    });

    await new Promise((r) => setTimeout(r, 50));

    // Stale response must NOT be accepted into vfx tag view!
    expect(messagesSignal.value.length).toBe(0);

    chatSpy.mockRestore();
  });

  it('immediately unlocks UI and allows sending in new Space while old chat promise is still pending', async () => {
    // Old chat promise never resolves during navigation
    const pendingChatPromise = new Promise(() => {});
    const chatSpy = vi.spyOn(api, 'chatWithAgent').mockReturnValue(pendingChatPromise as any);

    const { container } = render(<ChatArea />);
    const textInput = container.querySelector('#chat-input-field') as HTMLInputElement;
    fireEvent.input(textInput, { target: { value: 'Old space message' } });

    const sendBtn = container.querySelector('.btn-send') as HTMLButtonElement;
    fireEvent.click(sendBtn);

    // Send button was clicked, operation is active
    expect(messagesSignal.value.length).toBe(1);

    // Switch to Space B while chat is pending
    spaceContextSignal.value = {
      space: {
        space_id: 'spc_NEW_SPACE_B',
        name: 'New Space B',
        kind: 'agent_dm',
        created_by: 'alice_01',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      members: [],
      membership: { role: 'owner' },
      capabilities: {},
    } as any;
    messagesSignal.value = [];

    // The text input and send button in Space B MUST NOT be disabled by the old pending chat!
    fireEvent.input(textInput, { target: { value: 'New space message' } });

    // Now mock second chat call to resolve
    chatSpy.mockResolvedValueOnce({
      user_message: {
        message_id: 'msg_new_user',
        space_id: 'spc_NEW_SPACE_B',
        sender_uid: 'alice_01',
        role: 'user',
        content: 'New space message',
      },
      agent_message: {
        message_id: 'msg_new_agent',
        space_id: 'spc_NEW_SPACE_B',
        sender_uid: 'agent',
        role: 'agent',
        content: 'New space agent response',
      },
    });

    fireEvent.click(sendBtn);
    await new Promise((r) => setTimeout(r, 50));

    // Must have sent to New Space B successfully!
    expect(chatSpy).toHaveBeenCalledWith(
      'spc_NEW_SPACE_B',
      'New space message',
      'general',
      [],
      expect.any(String),
      'conversation',
      [],
      null,
      expect.any(Object)
    );

    chatSpy.mockRestore();
  });

  it('protects new AbortController from late-running finally block of previous request', async () => {
    let capturedSignalA: AbortSignal | undefined;
    let capturedSignalB: AbortSignal | undefined;

    let resolveChatA: (value: any) => void = () => {};
    const chatPromiseA = new Promise((resolve) => {
      resolveChatA = resolve;
    });

    const chatSpy = vi.spyOn(api, 'chatWithAgent').mockImplementation((...args: any[]) => {
      const signal = args[8] as AbortSignal;
      if (!capturedSignalA) {
        capturedSignalA = signal;
        return chatPromiseA as any;
      } else {
        capturedSignalB = signal;
        return new Promise(() => {}) as any;
      }
    });

    const { container } = render(<ChatArea />);
    const textInput = container.querySelector('#chat-input-field') as HTMLInputElement;
    const sendBtn = container.querySelector('.btn-send') as HTMLButtonElement;

    // 1. Send Request A in Space A
    fireEvent.input(textInput, { target: { value: 'Request A in Space A' } });
    fireEvent.click(sendBtn);
    expect(capturedSignalA).toBeDefined();
    expect(capturedSignalA!.aborted).toBe(false);

    // 2. Switch to Space B (aborts Request A)
    spaceContextSignal.value = {
      space: {
        space_id: 'spc_SPACE_B',
        name: 'Space B',
        kind: 'agent_dm',
        created_by: 'alice_01',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      members: [],
      membership: { role: 'owner' },
      capabilities: {},
    } as any;
    messagesSignal.value = [];
    expect(capturedSignalA!.aborted).toBe(true);

    // 3. Start Request B in Space B
    fireEvent.input(textInput, { target: { value: 'Request B in Space B' } });
    fireEvent.click(sendBtn);
    expect(capturedSignalB).toBeDefined();
    expect(capturedSignalB!.aborted).toBe(false);

    // 4. Now let Request A resolve and its finally block execute
    resolveChatA({
      user_message: { message_id: 'm1', content: 'A' },
    });
    await new Promise((r) => setTimeout(r, 50));

    // Request B's signal must NOT be aborted yet
    expect(capturedSignalB!.aborted).toBe(false);

    // 5. Now switch to Space C -> Request B's controller MUST receive the abort!
    spaceContextSignal.value = {
      space: {
        space_id: 'spc_SPACE_C',
        name: 'Space C',
        kind: 'agent_dm',
        created_by: 'alice_01',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      members: [],
      membership: { role: 'owner' },
      capabilities: {},
    } as any;

    expect(capturedSignalB!.aborted).toBe(true);

    chatSpy.mockRestore();
  });
});



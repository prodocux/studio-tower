import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/preact';
import { CitationBadge } from '../src/components/CitationBadge';
import { DocumentPreviewDrawer } from '../src/components/DocumentPreviewDrawer';
import { Citation } from '../src/types';
import { api } from '../src/services/api';

describe('Slice B2: Citations and Document Preview Drawer', () => {
  const mockCitation: Citation = {
    citation_id: 'cit_12345',
    index: 1,
    file_id: 'f_test_01',
    filename: 'test_script.pdf',
    generation: 1,
    content_hash: 'sha256_mock_hash_123',
    chunk_id: 'chk_001',
    source_locator: 'page:3',
    page_number: 3,
    char_start: 13,
    char_end: 51,
    snippet: 'This is the cited snippet from script.',
    score: 0.95,
  };

  it('renders CitationBadge correctly and calls onSelect on click', () => {
    let selectedCitation: Citation | null = null;
    const { getByRole, getByText } = render(
      <CitationBadge
        citation={mockCitation}
        onSelect={(c) => {
          selectedCitation = c;
        }}
      />
    );

    const badgeBtn = getByRole('button', { name: /檢視引用來源/ });
    expect(badgeBtn).toBeDefined();
    expect(getByText('[1]')).toBeDefined();

    // Trigger hover tooltip
    fireEvent.mouseEnter(badgeBtn);
    expect(getByText('test_script.pdf')).toBeDefined();
    expect(getByText('p.3')).toBeDefined();

    // Trigger click
    fireEvent.click(badgeBtn);
    expect(selectedCitation).toEqual(mockCitation);
  });

  it('renders DocumentPreviewDrawer with loaded chunk and highlights snippet', async () => {
    let closed = false;
    vi.spyOn(api, 'getCitationChunk').mockResolvedValueOnce({
      chunk_id: 'chk_001',
      file_id: 'f_test_01',
      space_id: 'sp_test',
      ingestion_version: 1,
      ordinal: 0,
      page_number: 3,
      section_heading: 'SCENE 1',
      source_locator: 'page:3',
      normalized_text: 'Header text. This is the cited snippet from script. Footer text.',
      char_start: 0,
      char_end: 65,
      token_count: 10,
      content_hash: 'sha256_mock_hash_123',
      contains_formula_like_content: false,
      extraction_method: 'plain_text',
      extractor_version: 'v1.0',
    });

    const { getByText, getByRole } = render(
      <DocumentPreviewDrawer
        citation={mockCitation}
        spaceId="sp_test"
        onClose={() => {
          closed = true;
        }}
      />
    );

    // Initial drawer header
    expect(getByText('test_script.pdf')).toBeDefined();
    expect(getByText('Gen #1')).toBeDefined();

    // Wait for chunk data to resolve
    await waitFor(() => {
      expect(getByText('This is the cited snippet from script.')).toBeDefined();
    });

    // Close button
    const closeBtn = getByRole('button', { name: /關閉預覽抽屜|Close preview drawer/ });
    fireEvent.click(closeBtn);
    expect(closed).toBe(true);
  });

  it('handles 404 / SOURCE_GENERATION_UNAVAILABLE gracefully in DocumentPreviewDrawer', async () => {
    vi.spyOn(api, 'getCitationChunk').mockRejectedValueOnce(
      new Error('SOURCE_GENERATION_UNAVAILABLE (404)')
    );

    const { getByText } = render(
      <DocumentPreviewDrawer
        citation={mockCitation}
        spaceId="sp_test"
        onClose={() => {}}
      />
    );

    await waitFor(() => {
      expect(getByText(/Source Version Unavailable|來源版本已不可用/)).toBeDefined();
    });
  });

  it('closes DocumentPreviewDrawer when Escape key is pressed', () => {
    let closed = false;
    render(
      <DocumentPreviewDrawer
        citation={mockCitation}
        spaceId="sp_test"
        onClose={() => {
          closed = true;
        }}
      />
    );

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(closed).toBe(true);
  });

  it('prevents stale citation responses from overwriting newer citations', async () => {
    let resolveCitationA: (val: any) => void = () => {};
    let resolveCitationB: (val: any) => void = () => {};

    vi.spyOn(api, 'getCitationChunk').mockImplementation((_spaceId, _fileId, chunkId) => {
      if (chunkId === 'chk_001') {
        return new Promise((res) => {
          resolveCitationA = res;
        });
      }
      return new Promise((res) => {
        resolveCitationB = res;
      });
    });

    const citationB: Citation = {
      ...mockCitation,
      citation_id: 'cit_B',
      chunk_id: 'chk_002',
      filename: 'second_doc.pdf',
      char_start: 0,
      char_end: 33,
      snippet: 'Second snippet content from doc 2',
    };

    const { getByText, getAllByText, rerender } = render(
      <DocumentPreviewDrawer
        citation={mockCitation}
        spaceId="sp_test"
        onClose={() => {}}
      />
    );

    // Switch rapidly to Citation B
    rerender(
      <DocumentPreviewDrawer
        citation={citationB}
        spaceId="sp_test"
        onClose={() => {}}
      />
    );

    // Resolve B first
    resolveCitationB({
      chunk_id: 'chk_002',
      file_id: 'f_test_01',
      space_id: 'sp_test',
      ingestion_version: 1,
      ordinal: 1,
      page_number: 1,
      source_locator: 'page:1',
      normalized_text: 'Second snippet content from doc 2',
      char_start: 0,
      char_end: 33,
      token_count: 5,
      content_hash: 'hash_b',
      extraction_method: 'plain_text',
      extractor_version: 'v1.0',
    });

    await waitFor(() => {
      expect(getAllByText('second_doc.pdf').length).toBeGreaterThan(0);
      expect(getByText('Second snippet content from doc 2')).toBeDefined();
    });

    // Now resolve old Citation A late
    resolveCitationA({
      chunk_id: 'chk_001',
      file_id: 'f_test_01',
      space_id: 'sp_test',
      ingestion_version: 1,
      ordinal: 0,
      page_number: 3,
      source_locator: 'page:3',
      normalized_text: 'Old first doc content should not overwrite',
      char_start: 0,
      char_end: 20,
      token_count: 5,
      content_hash: 'hash_a',
      extraction_method: 'plain_text',
      extractor_version: 'v1.0',
    });

    // Verify Citation B content remains and old Citation A was discarded
    await new Promise((r) => setTimeout(r, 50));
    expect(getAllByText('second_doc.pdf').length).toBeGreaterThan(0);
    expect(getByText('Second snippet content from doc 2')).toBeDefined();
  });

  it('displays locator warning when snippet does not match chunk text instead of highlighting wrong text', async () => {
    vi.spyOn(api, 'getCitationChunk').mockResolvedValueOnce({
      chunk_id: 'chk_mismatch',
      file_id: 'f_test_01',
      space_id: 'sp_test',
      ingestion_version: 1,
      ordinal: 0,
      page_number: 1,
      source_locator: 'page:1',
      normalized_text: 'Completely different text with no overlap.',
      char_start: 0,
      char_end: 42,
      token_count: 5,
      content_hash: 'hash_mismatch',
      extraction_method: 'plain_text',
      extractor_version: 'v1.0',
    });

    const mismatchCitation: Citation = {
      ...mockCitation,
      citation_id: 'cit_mismatch',
      char_start: 100,
      char_end: 150,
      snippet: 'Non-existent snippet from different section',
    };

    const { getByText } = render(
      <DocumentPreviewDrawer
        citation={mismatchCitation}
        spaceId="sp_test"
        onClose={() => {}}
      />
    );

    await waitFor(() => {
      expect(getByText(/Exact citation location mismatch|引用片段精確定位不一致/)).toBeDefined();
      expect(getByText('Completely different text with no overlap.')).toBeDefined();
    });
  });

  it('displays locator warning on ambiguous duplicate snippet occurrences without verified offset', async () => {
    vi.spyOn(api, 'getCitationChunk').mockResolvedValueOnce({
      chunk_id: 'chk_dup',
      file_id: 'f_test_01',
      space_id: 'sp_test',
      ingestion_version: 1,
      ordinal: 0,
      page_number: 1,
      source_locator: 'page:1',
      normalized_text: 'Approved budget is 100 dollars. Later revision: Approved budget is 100 dollars.',
      char_start: 0,
      char_end: 80,
      token_count: 15,
      content_hash: 'hash_dup',
      extraction_method: 'plain_text',
      extractor_version: 'v1.0',
    });

    const dupCitation: Citation = {
      ...mockCitation,
      citation_id: 'cit_dup',
      // Offset intentionally mismatched/unverified
      char_start: 999,
      char_end: 1030,
      snippet: 'Approved budget is 100 dollars.',
    };

    const { getByText, container } = render(
      <DocumentPreviewDrawer
        citation={dupCitation}
        spaceId="sp_test"
        onClose={() => {}}
      />
    );

    await waitFor(() => {
      expect(getByText(/Exact citation location mismatch|引用片段精確定位不一致/)).toBeDefined();
      // Verifies no <mark> was erroneously attached to the first duplicate
      expect(container.querySelector('mark')).toBeNull();
    });
  });

  it('does NOT highlight entire chunk when 500-char chunk has mismatched offset with small snippet', async () => {
    const longChunkText = 'A'.repeat(500);
    vi.spyOn(api, 'getCitationChunk').mockResolvedValueOnce({
      chunk_id: 'chk_large',
      file_id: 'f_test_01',
      space_id: 'sp_test',
      ingestion_version: 1,
      ordinal: 0,
      page_number: 1,
      source_locator: 'page:1',
      normalized_text: longChunkText,
      char_start: 0,
      char_end: 500,
      token_count: 100,
      content_hash: 'hash_large',
      extraction_method: 'plain_text',
      extractor_version: 'v1.0',
    });

    const smallCitation: Citation = {
      ...mockCitation,
      citation_id: 'cit_small',
      char_start: 0,
      char_end: 500, // Misaligned range claiming the whole 500 chars
      snippet: 'Small snippet', // But snippet is actually small
    };

    const { getByText, container } = render(
      <DocumentPreviewDrawer
        citation={smallCitation}
        spaceId="sp_test"
        onClose={() => {}}
      />
    );

    await waitFor(() => {
      // Must not highlight the entire 500-character chunk
      expect(getByText(/Exact citation location mismatch|引用片段精確定位不一致/)).toBeDefined();
      expect(container.querySelector('mark')).toBeNull();
    });
  });

  it('verifies interactive multi-citation switching and exact <mark> highlight in same/different docs', async () => {
    const cit1: Citation = {
      citation_id: 'cit_doc1',
      index: 1,
      file_id: 'f_doc1',
      filename: 'treatment_v1.pdf',
      generation: 1,
      content_hash: 'hash_1',
      chunk_id: 'chk_d1',
      source_locator: 'page:1',
      page_number: 1,
      char_start: 10,
      char_end: 22,
      snippet: '預算為 100 萬美元',
      score: 0.9,
    };

    const cit2: Citation = {
      citation_id: 'cit_doc2',
      index: 2,
      file_id: 'f_doc2',
      filename: 'treatment_v2.pdf',
      generation: 1,
      content_hash: 'hash_2',
      chunk_id: 'chk_d2',
      source_locator: 'page:1',
      page_number: 1,
      char_start: 10,
      char_end: 24,
      snippet: '總預算調整為 150 萬美元',
      score: 0.9,
    };

    vi.spyOn(api, 'getCitationChunk').mockImplementation(async (_sp, fileId) => {
      if (fileId === 'f_doc1') {
        return {
          chunk_id: 'chk_d1',
          file_id: 'f_doc1',
          space_id: 'sp_test',
          ingestion_version: 1,
          ordinal: 0,
          page_number: 1,
          source_locator: 'page:1',
          normalized_text: '企劃版本一：總預算為 100 萬美元，拍攝期為 30 天。',
          char_start: 0,
          char_end: 28,
          token_count: 10,
          content_hash: 'hash_1',
          extraction_method: 'plain_text',
          extractor_version: 'v1.0',
        };
      }
      return {
        chunk_id: 'chk_d2',
        file_id: 'f_doc2',
        space_id: 'sp_test',
        ingestion_version: 1,
        ordinal: 0,
        page_number: 1,
        source_locator: 'page:1',
        normalized_text: '企劃版本二：總預算調整為 150 萬美元。主要角色為林晨。',
        char_start: 0,
        char_end: 28,
        token_count: 10,
        content_hash: 'hash_2',
        extraction_method: 'plain_text',
        extractor_version: 'v1.0',
      };
    });

    const { getAllByText, rerender, container } = render(
      <DocumentPreviewDrawer
        citation={cit1}
        spaceId="sp_test"
        onClose={() => {}}
      />
    );

    // Initial render for Citation 1
    await waitFor(() => {
      expect(getAllByText('treatment_v1.pdf').length).toBeGreaterThan(0);
      const mark = container.querySelector('mark');
      expect(mark).not.toBeNull();
      expect(mark?.textContent).toBe('預算為 100 萬美元');
    });

    // User switches to Citation 2
    rerender(
      <DocumentPreviewDrawer
        citation={cit2}
        spaceId="sp_test"
        onClose={() => {}}
      />
    );

    // Render for Citation 2
    await waitFor(() => {
      expect(getAllByText('treatment_v2.pdf').length).toBeGreaterThan(0);
      const mark = container.querySelector('mark');
      expect(mark).not.toBeNull();
      expect(mark?.textContent).toBe('總預算調整為 150 萬美元');
    });
  });

  it('renders correctly under mobile (375px), tablet (768px), and desktop (1280px) viewports with data-theme dark and light', async () => {
    vi.spyOn(api, 'getCitationChunk').mockResolvedValue({
      chunk_id: 'chk_001',
      file_id: 'f_test_01',
      space_id: 'sp_test',
      ingestion_version: 1,
      ordinal: 0,
      page_number: 3,
      source_locator: 'page:3',
      normalized_text: 'This is the cited snippet from script.',
      char_start: 0,
      char_end: 38,
      token_count: 8,
      content_hash: 'hash_vp',
      extraction_method: 'plain_text',
      extractor_version: 'v1.0',
    });

    // Test across viewports and data-theme attributes
    for (const width of [375, 768, 1280]) {
      window.innerWidth = width;
      for (const theme of ['dark', 'light']) {
        document.documentElement.setAttribute('data-theme', theme);
        const { getAllByText, getByText, unmount } = render(
          <DocumentPreviewDrawer
            citation={mockCitation}
            spaceId="sp_test"
            onClose={() => {}}
          />
        );

        await waitFor(() => {
          expect(getAllByText('test_script.pdf').length).toBeGreaterThan(0);
          expect(getByText('This is the cited snippet from script.')).toBeDefined();
        });
        unmount();
      }
    }
  });

  it('verifies ChatArea message citation badge click opens drawer, highlights snippet, and closes on Escape', async () => {
    const { ChatArea } = await import('../src/components/ChatArea');
    const {
      spaceContextSignal,
      messagesSignal,
      activeTagSignal,
    } = await import('../src/services/store');
    const { currentUserSignal } = await import('../src/services/auth');

    currentUserSignal.value = {
      uid: 'u_user_1',
      email: 'user@test.com',
      display_name: 'Test User',
      photo_url: null,
      created_at: new Date().toISOString(),
    };

    spaceContextSignal.value = {
      space: {
        space_id: 'sp_test_chat',
        name: 'Test Space',
        description: 'Test Description',
        kind: 'project',
        created_by: 'u_user_1',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        tags: ['general'],
      },
      members: [],
      my_role: 'owner',
    };
    activeTagSignal.value = 'general';

    const cit1: Citation = {
      citation_id: 'cit_chat_01',
      index: 1,
      file_id: 'f_doc1',
      filename: 'treatment_v1.pdf',
      generation: 1,
      content_hash: 'hash_1',
      chunk_id: 'chk_c1',
      source_locator: 'page:1',
      page_number: 1,
      char_start: 0,
      char_end: 12,
      snippet: '預算為 100 萬美元',
      score: 0.95,
    };

    messagesSignal.value = [
      {
        message_id: 'msg_agent_01',
        space_id: 'sp_test_chat',
        sender_uid: 'agent',
        sender_name: 'StudioTower AI',
        role: 'agent',
        content: '版本一的總預算已核定 [1]。',
        project_tag: 'general',
        attachment_file_ids: ['f_doc1'],
        citations: [cit1],
        created_at: new Date().toISOString(),
      },
    ];

    vi.spyOn(api, 'getCitationChunk').mockResolvedValueOnce({
      chunk_id: 'chk_c1',
      file_id: 'f_doc1',
      space_id: 'sp_test_chat',
      ingestion_version: 1,
      ordinal: 0,
      page_number: 1,
      source_locator: 'page:1',
      normalized_text: '預算為 100 萬美元，拍攝期為 30 天。',
      char_start: 0,
      char_end: 20,
      token_count: 5,
      content_hash: 'hash_1',
      extraction_method: 'plain_text',
      extractor_version: 'v1.0',
    });

    const { getByRole, getAllByText, queryByRole, container } = render(<ChatArea />);

    // 1. Find and click badge [1] in ChatArea
    const badgeBtn = getByRole('button', { name: /檢視引用來源/ });
    expect(badgeBtn).toBeDefined();
    badgeBtn.focus();
    expect(document.activeElement).toBe(badgeBtn);

    fireEvent.click(badgeBtn);

    // 2. Assert DocumentPreviewDrawer opens, role="dialog" is present, and <mark> snippet exactly matches
    await waitFor(() => {
      expect(getAllByText('treatment_v1.pdf').length).toBeGreaterThan(0);
      expect(getByRole('dialog')).toBeDefined();
      const mark = container.querySelector('mark');
      expect(mark).not.toBeNull();
      expect(mark?.textContent).toBe('預算為 100 萬美元');
    });

    // 3. Press Escape to close preview drawer
    fireEvent.keyDown(window, { key: 'Escape' });

    // 4. Assert role="dialog" unmounts and closes, and focus returns to badge button
    await waitFor(() => {
      expect(queryByRole('dialog')).toBeNull();
      expect(document.activeElement).toBe(badgeBtn);
    });
  });
});




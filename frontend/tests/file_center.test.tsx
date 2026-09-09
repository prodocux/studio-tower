import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/preact';
import { FileCenter } from '../src/components/FileCenter';
import { RunsAndApprovalsView } from '../src/components/RunsAndApprovalsView';
import { DocumentViewerModal } from '../src/components/DocumentViewerModal';
import { RightPanel } from '../src/components/RightPanel';
import {
  filesSignal,
  spaceContextSignal,
  activeTagSignal,
  activeSpaceIdSignal,
  fileSearchQuerySignal,
  fileStatusFilterSignal,
  fileSortSignal,
  selectedFileIdsSignal,
  activeViewSignal,
  composerContextFileIdsSignal,
  composerContextRunIdSignal,
  selectedRunIdSignal,
  composerIntentSignal,
  runsSignal,
  rightPanelOpenSignal,
  activeRightTabSignal,
} from '../src/services/store';
import { currentUserSignal } from '../src/services/auth';
import { api } from '../src/services/api';
import { FileRecord, Run } from '../src/types';

describe('FileCenter & Runs Components (Contract Aligned)', () => {
  const mockFiles: FileRecord[] = [
    {
      file_id: 'f_doc_01',
      space_id: 'sp_test',
      storage_path: 'uploads/f_doc_01',
      filename: 'script_v1.pdf',
      content_type: 'application/pdf',
      size_bytes: 1048576, // 1MB
      uploaded_by: 'u_user_1',
      source_type: 'user_upload',
      sha256: 'sha256_mock_01',
      ingestion_status: 'ready',
      active_generation: 1,
      chunk_count: 15,
      project_tags: ['general'],
      created_at: '2026-08-30T10:00:00Z',
    },
    {
      file_id: 'f_doc_02',
      space_id: 'sp_test',
      storage_path: 'uploads/f_doc_02',
      filename: 'treatment_action.txt',
      content_type: 'text/plain',
      size_bytes: 51200, // 50KB
      uploaded_by: 'u_user_2',
      source_type: 'user_upload',
      sha256: 'sha256_mock_02',
      ingestion_status: 'ready_partial',
      active_generation: 1,
      chunk_count: 5,
      ocr_gap_pages: [2, 3],
      project_tags: ['block-a'],
      created_at: '2026-08-30T11:00:00Z',
    },
    {
      file_id: 'f_doc_03',
      space_id: 'sp_test',
      storage_path: 'uploads/f_doc_03',
      filename: 'corrupted_scan.pdf',
      content_type: 'application/pdf',
      size_bytes: 204800,
      uploaded_by: 'u_user_1',
      source_type: 'user_upload',
      sha256: 'sha256_mock_03',
      ingestion_status: 'failed',
      ingestion_error_message: 'Password protected or invalid PDF stream',
      active_generation: 0,
      project_tags: ['general'],
      created_at: '2026-08-30T12:00:00Z',
    },
    {
      file_id: 'f_doc_04',
      space_id: 'sp_test',
      storage_path: 'uploads/f_doc_04',
      filename: 'raw_scans.pdf',
      content_type: 'application/pdf',
      size_bytes: 307200,
      uploaded_by: 'u_user_1',
      source_type: 'user_upload',
      sha256: 'sha256_mock_04',
      ingestion_status: 'needs_ocr',
      active_generation: 1,
      chunk_count: 0,
      project_tags: ['general'],
      created_at: '2026-08-30T13:00:00Z',
    },
  ];

  beforeEach(() => {
    currentUserSignal.value = {
      uid: 'u_user_1',
      email: 'user1@studiotower.ai',
      display_name: 'User One',
    };
    activeSpaceIdSignal.value = 'sp_test';
    filesSignal.value = [...mockFiles];
    spaceContextSignal.value = {
      space: {
        space_id: 'sp_test',
        name: 'Film Test Space',
        kind: 'shared_space',
        created_by: 'u_user_1',
        created_at: '2026-08-30T00:00:00Z',
        tags: [
          { name: 'General', slug: 'general', color: '#64748B' },
          { name: 'Block A', slug: 'block-a', color: '#3B82F6' },
        ],
      },
      current_user_role: 'owner',
      member_count: 3,
      capabilities: {
        can_invite: true,
        can_manage_members: true,
        can_change_role: true,
        can_remove_member: true,
        can_transfer_ownership: true,
        can_approve_runs: true,
        can_manage_tags: true,
        can_leave_space: false,
      },
    };
    activeTagSignal.value = 'all';
    fileSearchQuerySignal.value = '';
    fileStatusFilterSignal.value = 'all';
    fileSortSignal.value = 'uploaded_at_desc';
    selectedFileIdsSignal.value = [];
    activeViewSignal.value = 'files';
    composerContextFileIdsSignal.value = [];
    composerContextRunIdSignal.value = null;
    selectedRunIdSignal.value = null;
    composerIntentSignal.value = 'conversation';
  });

  it('renders all files with their respective status badges including needs_ocr', () => {
    const { getByText } = render(<FileCenter />);

    expect(getByText('script_v1.pdf')).toBeDefined();
    expect(getByText('treatment_action.txt')).toBeDefined();
    expect(getByText('corrupted_scan.pdf')).toBeDefined();
    expect(getByText('raw_scans.pdf')).toBeDefined();

    // Verify badges
    expect(getByText(/Ready \(Gen #1\)/)).toBeDefined();
    expect(getByText(/⚠ Partial/)).toBeDefined();
    expect(getByText(/📷 Needs OCR/)).toBeDefined();
    expect(getByText(/✕ Failed/)).toBeDefined();
  });

  it('filters files by search keyword', () => {
    const { getByText, queryByText, getByPlaceholderText } = render(<FileCenter />);

    const searchInput = getByPlaceholderText(/(搜尋文件名稱或上傳者\.\.\.|Search filename or uploader\.\.\.)/);
    fireEvent.input(searchInput, { target: { value: 'action' } });

    expect(getByText('treatment_action.txt')).toBeDefined();
    expect(queryByText('script_v1.pdf')).toBeNull();
    expect(queryByText('corrupted_scan.pdf')).toBeNull();
  });

  it('filters files by ingestion status dropdown', () => {
    const { getByText, queryByText, container } = render(<FileCenter />);

    const select = container.querySelector('#status-filter-select') as HTMLSelectElement;
    expect(select).not.toBeNull();

    // Filter to 'failed'
    fireEvent.change(select, { target: { value: 'failed' } });

    expect(getByText('corrupted_scan.pdf')).toBeDefined();
    expect(queryByText('script_v1.pdf')).toBeNull();
    expect(queryByText('treatment_action.txt')).toBeNull();
  });

  it('handles multi-file selection and transfers context chips to Chat when clicking "問 AI"', () => {
    const { getByLabelText, getByRole } = render(<FileCenter />);

    // Select file 1 and file 2
    const check1 = getByLabelText(/(選取|Select) script_v1\.pdf/);
    const check2 = getByLabelText(/(選取|Select) treatment_action\.txt/);

    fireEvent.click(check1);
    fireEvent.click(check2);

    expect(selectedFileIdsSignal.value).toEqual(['f_doc_01', 'f_doc_02']);

    // Click "Ask AI (2)"
    const askAiBtn = getByRole('button', { name: /(問 AI|Ask AI) \(2\)/ });
    expect(askAiBtn).toBeDefined();
    fireEvent.click(askAiBtn);

    // Verify composer state transfer
    expect(composerContextFileIdsSignal.value).toEqual(['f_doc_01', 'f_doc_02']);
    expect(composerIntentSignal.value).toBe('document_qa');
    expect(activeViewSignal.value).toBe('chat');
  });

  it('allows loading 51st chunk when first 50 chunks do not match search query', async () => {
    // Initial fetch: 50 chunks with no matching text
    const getChunksSpy = vi.spyOn(api, 'getChunks').mockImplementation(async (_sp, _fid, _gen, cursor) => {
      if (cursor === 0) {
        return {
          items: Array.from({ length: 50 }, (_, i) => ({
            chunk_id: `chk_${i + 1}`,
            file_id: 'f_doc_01',
            space_id: 'sp_test',
            ordinal: i + 1,
            page_number: 1,
            source_locator: `page:1:chunk:${i + 1}`,
            normalized_text: `Standard script dialogue paragraph line ${i + 1}.`,
            token_count: 10,
            char_start: i * 50,
            char_end: (i + 1) * 50,
            content_hash: `hash_${i + 1}`,
            contains_formula_like_content: false,
          })),
          cursor: 0,
          limit: 50,
          total: 100,
          has_more: true,
          next_cursor: 50,
          active_generation: 1,
        };
      }
      // 2nd page fetch: chunk 51 has the target keyword "explosive"
      return {
        items: [
          {
            chunk_id: 'chk_51',
            file_id: 'f_doc_01',
            space_id: 'sp_test',
            ordinal: 51,
            page_number: 3,
            source_locator: 'page:3:scene:9',
            normalized_text: 'An explosive revelation occurs here.',
            token_count: 8,
            char_start: 2500,
            char_end: 2540,
            content_hash: 'hash_51',
            contains_formula_like_content: false,
          },
        ],
        cursor: 50,
        limit: 50,
        total: 100,
        has_more: false,
        next_cursor: null,
        active_generation: 1,
      };
    });

    const { getByText, getByPlaceholderText, queryByText, container } = render(
      <DocumentViewerModal
        file={mockFiles[0]}
        spaceId="sp_test"
        onClose={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(getChunksSpy).toHaveBeenCalledWith('sp_test', 'f_doc_01', 1, 0, 50);
    });

    // Search for "explosive" which is in chunk 51
    const searchInput = getByPlaceholderText(/(在文件內容中搜尋關鍵字\.\.\.|Search within document\.\.\.)/);
    fireEvent.input(searchInput, { target: { value: 'explosive' } });

    // Verify hint shows no matches in loaded 50 chunks, but load more button remains available
    expect(getByText(/(未找到符合「explosive」的內容|No matches found for "explosive")/)).toBeDefined();
    const loadMoreBtn = getByText(/(載入更多片段|Load More Chunks)/);
    expect(loadMoreBtn).toBeDefined();

    // Click load more to fetch chunk 51
    fireEvent.click(loadMoreBtn);

    await waitFor(() => {
      expect(container.querySelector('mark')?.textContent).toBe('explosive');
      expect(getByText(/revelation occurs here/)).toBeDefined();
      expect(queryByText(/(未找到符合「explosive」的內容|No matches found for "explosive")/)).toBeNull();
    });
  });

  it('renders RunsAndApprovalsView and handles strict retry eligibility, telemetry targeting, and scrollIntoView', async () => {
    const mockRuns: Run[] = [
      {
        run_id: 'run_gate_01',
        space_id: 'sp_test',
        status: 'awaiting_approval',
        prompt: 'Execute scene breakdown and cast list',
        project_tag: 'general',
        created_by: 'u_user_1',
        is_retryable: false,
        approval_gate: {
          gate_id: 'gate_b1',
          title: '預算審批關卡 (VFX Expense Gate)',
          description: '預計產生 12,000 Token 消耗，請空間管理員確認',
          required_role: 'admin',
          status: 'pending',
        },
      },
      {
        run_id: 'run_failed_non_retryable',
        space_id: 'sp_test',
        status: 'failed',
        prompt: 'Rejected unauthorized extraction',
        project_tag: 'general',
        created_by: 'u_user_1',
        is_retryable: false, // Must NOT show retry
      },
      {
        run_id: 'run_failed_retryable',
        space_id: 'sp_test',
        status: 'failed',
        prompt: 'Transient network failure during OCR',
        project_tag: 'general',
        created_by: 'u_user_1',
        is_retryable: true, // Must show retry
      },
    ];
    runsSignal.value = mockRuns;

    const retrySpy = vi.spyOn(api, 'retryRun').mockResolvedValue(mockRuns[2]);
    vi.spyOn(api, 'listRuns').mockResolvedValue([...mockRuns]);

    const { queryAllByText, getAllByText } = render(<RunsAndApprovalsView />);

    // 1. Only 1 retry button should exist (for run_failed_retryable)
    const retryButtons = queryAllByText(/(重試|Retry)/);
    expect(retryButtons.length).toBe(1);

    // 2. Click retry
    fireEvent.click(retryButtons[0]);
    await waitFor(() => {
      expect(retrySpy).toHaveBeenCalledWith('sp_test', 'run_failed_retryable');
    });

    // 3. Click Telemetry button and verify navigation targeting without polluting chat composer
    const telemetryButtons = getAllByText(/(📊 遙測|📊 Telemetry)/);
    fireEvent.click(telemetryButtons[0]);
    expect(selectedRunIdSignal.value).toBe('run_gate_01');
    expect(composerContextRunIdSignal.value).toBeNull(); // Chat Composer clean!
    expect(activeRightTabSignal.value).toBe('grafana');
    expect(rightPanelOpenSignal.value).toBe(true);

    // 4. Verify RightPanel highlights the targeted run card with visual class and scrollIntoView
    const scrollMock = vi.fn();
    Element.prototype.scrollIntoView = scrollMock;

    vi.spyOn(api, 'getGrafanaVisual').mockResolvedValue({
      connected: false,
      error: 'test',
      dashboards: [],
    });
    const { container: rightContainer } = render(<RightPanel />);
    const targetedCard = rightContainer.querySelector('.selected-run-target');
    expect(targetedCard).not.toBeNull();
    expect(targetedCard?.getAttribute('data-selected')).toBe('true');
    expect(scrollMock).toHaveBeenCalled();
  });

  it('resets upload loading state immediately when switching Tag mid-flight and prevents locked upload button', async () => {
    let resolveUpload: (val: any) => void;
    vi.spyOn(api, 'uploadFileWithProgress').mockImplementation(() => {
      return new Promise((resolve) => {
        resolveUpload = resolve;
      });
    });

    const { container, queryByText, rerender } = render(<FileCenter />);

    // Trigger file upload
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['mock screenplay content'], 'sample.pdf', { type: 'application/pdf' });
    fireEvent.change(input, { target: { files: [file] } });

    // Verify loading indicator is present
    expect(queryByText(/(正在上傳|Uploading\.\.\.)/)).not.toBeNull();

    // Switch Tag mid-flight
    activeTagSignal.value = 'block-a';
    rerender(<FileCenter />);

    // Verify upload state reset on tag switch
    expect(queryByText(/(正在上傳|Uploading\.\.\.)/)).toBeNull();

    // Resolve old upload promise
    resolveUpload!({
      file_id: 'f_new_01',
      space_id: 'sp_test',
      storage_path: 'uploads/f_new_01',
      filename: 'sample.pdf',
      content_type: 'application/pdf',
      size_bytes: 100,
      uploaded_by: 'u_user_1',
      source_type: 'user_upload',
      sha256: 'sha256_mock_new',
      ingestion_status: 'ready',
      active_generation: 1,
      project_tags: ['general'],
      created_at: '2026-08-30T14:00:00Z',
    });

    // Verify upload button is never locked
    rerender(<FileCenter />);
    expect(queryByText(/(正在上傳|Uploading\.\.\.)/)).toBeNull();
  });

  it('handles interleaved uploads across tag switches without clearing the second upload loading state', async () => {
    let resolveUpload1: (val: any) => void;
    let resolveUpload2: (val: any) => void;

    let uploadCount = 0;
    vi.spyOn(api, 'uploadFileWithProgress').mockImplementation(() => {
      uploadCount++;
      if (uploadCount === 1) {
        return new Promise((resolve) => {
          resolveUpload1 = resolve;
        });
      }
      return new Promise((resolve) => {
        resolveUpload2 = resolve;
      });
    });

    const { container, queryByText, rerender } = render(<FileCenter />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;

    // 1. Upload 1 on Tag 'all'
    fireEvent.change(input, { target: { files: [new File(['f1'], 'f1.pdf', { type: 'application/pdf' })] } });
    expect(queryByText(/(正在上傳|Uploading\.\.\.)/)).not.toBeNull();

    // 2. Switch to Tag 'block-a' and start Upload 2
    activeTagSignal.value = 'block-a';
    rerender(<FileCenter />);

    fireEvent.change(input, { target: { files: [new File(['f2'], 'f2.pdf', { type: 'application/pdf' })] } });
    expect(queryByText(/(正在上傳|Uploading\.\.\.)/)).not.toBeNull();

    // 3. Upload 1 completes while Upload 2 is still running
    resolveUpload1!({
      file_id: 'f_1',
      space_id: 'sp_test',
      storage_path: 'uploads/f_1',
      filename: 'f1.pdf',
      content_type: 'application/pdf',
      size_bytes: 100,
      uploaded_by: 'u_user_1',
      source_type: 'user_upload',
      sha256: 'sha256_1',
      ingestion_status: 'ready',
      active_generation: 1,
      project_tags: ['general'],
      created_at: '2026-08-30T14:00:00Z',
    });

    await Promise.resolve();
    rerender(<FileCenter />);

    // 4. Verify Upload 2 STILL shows "Uploading..." (Upload 1's finally did NOT prematurely clear Upload 2's state)
    expect(queryByText(/(正在上傳|Uploading\.\.\.)/)).not.toBeNull();

    // 5. Upload 2 completes
    resolveUpload2!({
      file_id: 'f_2',
      space_id: 'sp_test',
      storage_path: 'uploads/f_2',
      filename: 'f2.pdf',
      content_type: 'application/pdf',
      size_bytes: 200,
      uploaded_by: 'u_user_1',
      source_type: 'user_upload',
      sha256: 'sha256_2',
      ingestion_status: 'ready',
      active_generation: 1,
      project_tags: ['block-a'],
      created_at: '2026-08-30T14:05:00Z',
    });

    await waitFor(() => {
      expect(queryByText(/(正在上傳|Uploading\.\.\.)/)).toBeNull();
    });
  });

  it('ensures in-flight uploads and reindexes in the same space succeed even when true extracting background polling interval fires and refreshes filesSignal', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });

    // 1. Initial state contains an 'extracting' file so hasProcessing is true
    const processingFile: FileRecord = {
      file_id: 'f_extracting_01',
      space_id: 'sp_test',
      storage_path: 'uploads/f_extracting_01',
      filename: 'processing_draft.pdf',
      content_type: 'application/pdf',
      size_bytes: 1024,
      uploaded_by: 'u_user_1',
      source_type: 'user_upload',
      sha256: 'sha_proc',
      ingestion_status: 'extracting',
      active_generation: 1,
      project_tags: ['general'],
      created_at: '2026-08-30T10:00:00Z',
    };
    filesSignal.value = [...mockFiles, processingFile];

    let resolveUpload: (val: any) => void;
    vi.spyOn(api, 'uploadFileWithProgress').mockImplementation(() => {
      return new Promise((resolve) => {
        resolveUpload = resolve;
      });
    });

    let resolveReindex: (val: any) => void;
    vi.spyOn(api, 'reindexFile').mockImplementation(() => {
      return new Promise((resolve) => {
        resolveReindex = resolve;
      });
    });

    let listFilesCallCount = 0;
    const uploadedRecord: FileRecord = {
      file_id: 'f_uploaded_now',
      space_id: 'sp_test',
      storage_path: 'uploads/f_uploaded_now',
      filename: 'async_script.pdf',
      content_type: 'application/pdf',
      size_bytes: 2048,
      uploaded_by: 'u_user_1',
      source_type: 'user_upload',
      sha256: 'sha_uploaded',
      ingestion_status: 'ready',
      active_generation: 1,
      project_tags: ['general'],
      created_at: '2026-08-30T14:10:00Z',
    };

    // Mock listFiles:
    // Call 1 (from background polling): extracting file becomes ready
    // Call 2 (from reindex completion): doc_01 advances to active_generation: 2
    const listFilesSpy = vi.spyOn(api, 'listFiles').mockImplementation(async () => {
      listFilesCallCount++;
      if (listFilesCallCount === 1) {
        return [
          ...mockFiles,
          {
            ...processingFile,
            ingestion_status: 'ready',
          },
        ];
      }
      return [
        {
          ...mockFiles[0],
          active_generation: 2, // Successfully updated generation
        },
        ...mockFiles.slice(1),
        {
          ...processingFile,
          ingestion_status: 'ready',
        },
        uploadedRecord,
      ];
    });

    const { container, getAllByText } = render(<FileCenter />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;

    // 2. Start file upload (in-flight)
    fireEvent.change(input, { target: { files: [new File(['f_async'], 'async_script.pdf', { type: 'application/pdf' })] } });

    // 3. Start file reindex (in-flight) on mockFiles[0]
    const reindexButtons = getAllByText(/(重新索引|Reindex)/);
    fireEvent.click(reindexButtons[0]);

    // 4. [Stage 1] Advance fake timer by 2500ms to trigger the background polling interval
    await vi.advanceTimersByTimeAsync(2600);

    // Verify polling ran and updated the extracting file to ready
    expect(listFilesSpy).toHaveBeenCalledTimes(1);
    expect(filesSignal.value.find((f) => f.file_id === 'f_extracting_01')?.ingestion_status).toBe('ready');

    // 5. [Stage 2] Complete the in-flight upload only (reindex still pending)
    resolveUpload!(uploadedRecord);
    await vi.advanceTimersByTimeAsync(100);

    // Verify upload directly wrote back without waiting for reindex
    expect(filesSignal.value.some((f) => f.file_id === 'f_uploaded_now')).toBe(true);
    // doc_01 is still generation 1 (reindex hasn't finished yet)
    expect(filesSignal.value.find((f) => f.file_id === 'f_doc_01')?.active_generation).toBe(1);

    // 6. [Stage 3] Complete the in-flight reindex
    resolveReindex!({ ok: true });
    await vi.advanceTimersByTimeAsync(100);

    // Verify reindex triggered an additional listFiles call and wrote back generation 2
    expect(listFilesSpy).toHaveBeenCalledTimes(2);
    expect(filesSignal.value.find((f) => f.file_id === 'f_doc_01')?.active_generation).toBe(2);
    expect(filesSignal.value.some((f) => f.file_id === 'f_uploaded_now')).toBe(true);

    vi.useRealTimers();
  });
});

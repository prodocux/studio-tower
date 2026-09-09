import { JSX } from 'preact';
import { useState, useRef, useEffect, useLayoutEffect, useMemo } from 'preact/hooks';
import {
  messagesSignal,
  messagesNextCursorSignal,
  messagesHasMoreSignal,
  activeTagSignal,
  spaceContextSignal,
  filesSignal,
  runsSignal,
  messagesStateSignal,
  filesStateSignal,
  composerDraftSignal,
  composerContextFileIdsSignal,
  composerContextRunIdSignal,
  composerIntentSignal,
} from '../services/store';
import { currentUserSignal } from '../services/auth';
import { api } from '../services/api';
import { showToast } from '../services/toast';
import { Message, FileRecord, Citation } from '../types';
import { escapeHtml } from '../utils';
import { CitationBadge } from './CitationBadge';
import { DocumentPreviewDrawer } from './DocumentPreviewDrawer';
import { ActionProposalCard } from './ActionProposalCard';
import { MarkdownRenderer } from './MarkdownRenderer';

export function ChatArea(): JSX.Element {
  const [inputText, setInputText] = useState('');
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [activeSendingOpGen, setActiveSendingOpGen] = useState<number | null>(null);
  const [showUnreadPill, setShowUnreadPill] = useState(false);
  const [isDraggingOver, setIsDraggingOver] = useState(false);
  const [activeCitation, setActiveCitation] = useState<Citation | null>(null);
  const [isLoadingEarlier, setIsLoadingEarlier] = useState(false);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const chatAbortRef = useRef<AbortController | null>(null);
  const isNearBottomRef = useRef(true);
  const prevMsgCountRef = useRef(messagesSignal.value.length);
  const prevSpaceTagRef = useRef({ spaceId: spaceContextSignal.value?.space.space_id, tag: activeTagSignal.value });
  const opGenerationRef = useRef(0);
  const anchorRef = useRef<{ msgId: string; top: number } | null>(null);
  const resizeObserverRef = useRef<ResizeObserver | null>(null);

  const isSending = activeSendingOpGen !== null && activeSendingOpGen === opGenerationRef.current;

  const rawMessages = messagesSignal.value;
  const activeTag = activeTagSignal.value;
  const context = spaceContextSignal.value;
  const currentUser = currentUserSignal.value;
  const messagesState = messagesStateSignal.value;
  const hasMoreMessages = messagesHasMoreSignal.value;

  // Stably sort messages chronologically (oldest to newest)
  const sortedMessages = useMemo(() => {
    return [...rawMessages].sort((a, b) => {
      const tA = a.created_at ? new Date(a.created_at).getTime() : 0;
      const tB = b.created_at ? new Date(b.created_at).getTime() : 0;
      return tA - tB;
    });
  }, [rawMessages]);

  const scrollToBottom = (smooth = true) => {
    if (scrollContainerRef.current) {
      if (typeof scrollContainerRef.current.scrollTo === 'function') {
        scrollContainerRef.current.scrollTo({
          top: scrollContainerRef.current.scrollHeight,
          behavior: smooth ? 'smooth' : 'auto',
        });
      } else {
        scrollContainerRef.current.scrollTop = scrollContainerRef.current.scrollHeight;
      }
      setShowUnreadPill(false);
      isNearBottomRef.current = true;
    }
  };

  const handleScroll = () => {
    if (!scrollContainerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollContainerRef.current;
    const distanceToBottom = scrollHeight - scrollTop - clientHeight;
    const nearBottom = distanceToBottom < 120;
    isNearBottomRef.current = nearBottom;
    if (nearBottom) {
      setShowUnreadPill(false);
    }
  };

  const handleLoadEarlier = async () => {
    const cursor = messagesNextCursorSignal.value;
    const spaceId = context?.space.space_id;
    if (isLoadingEarlier || !cursor || !spaceId) return;

    setIsLoadingEarlier(true);

    // 1. Capture anchor element offset before prepending
    const firstMsgEl = scrollContainerRef.current?.querySelector('.message-item') as HTMLElement | null;
    if (firstMsgEl && firstMsgEl.id) {
      anchorRef.current = {
        msgId: firstMsgEl.id,
        top: firstMsgEl.getBoundingClientRect().top,
      };
    }

    try {
      const res = await api.listMessagesPage(spaceId, activeTag, 30, cursor);
      const existingIds = new Set(messagesSignal.value.map((m) => m.message_id));
      const newItems = res.items.filter((m) => !existingIds.has(m.message_id));

      messagesSignal.value = [...newItems, ...messagesSignal.value];
      messagesNextCursorSignal.value = res.next_cursor || null;
      messagesHasMoreSignal.value = res.has_more;
    } catch (err: any) {
      showToast(`Failed to load earlier messages: ${err.message}`, 'error');
    } finally {
      setIsLoadingEarlier(false);
    }
  };

  // Synchronous Layout Scroll Anchoring: Pins to bottom on new message or compensates scroll delta on earlier message prepend
  useLayoutEffect(() => {
    const currentSpaceId = context?.space.space_id;
    const isSpaceOrTagChanged =
      prevSpaceTagRef.current.spaceId !== currentSpaceId || prevSpaceTagRef.current.tag !== activeTag;

    if (isSpaceOrTagChanged) {
      setActiveCitation(null);
      opGenerationRef.current++;
      setActiveSendingOpGen(null);
      uploadAbortRef.current?.abort();
      chatAbortRef.current?.abort();
      prevSpaceTagRef.current = { spaceId: currentSpaceId, tag: activeTag };
      prevMsgCountRef.current = sortedMessages.length;
      anchorRef.current = null;
      scrollToBottom(false);
      return;
    }

    // If we have a pending anchor compensation from loading earlier messages:
    if (anchorRef.current) {
      const anchorEl = document.getElementById(anchorRef.current.msgId);
      if (anchorEl && scrollContainerRef.current) {
        const newTop = anchorEl.getBoundingClientRect().top;
        const delta = newTop - anchorRef.current.top;
        if (delta !== 0) {
          scrollContainerRef.current.scrollTop += delta;
        }

        // Attach short-lived ResizeObserver to stabilize delayed fonts/cards
        resizeObserverRef.current?.disconnect();
        const obs = new ResizeObserver(() => {
          if (anchorEl && scrollContainerRef.current && anchorRef.current) {
            const curTop = anchorEl.getBoundingClientRect().top;
            const d = curTop - anchorRef.current.top;
            if (Math.abs(d) > 1) {
              scrollContainerRef.current.scrollTop += d;
            }
          }
        });
        obs.observe(anchorEl);
        resizeObserverRef.current = obs;

        setTimeout(() => {
          obs.disconnect();
          anchorRef.current = null;
        }, 1500);
      }
    } else if (sortedMessages.length > prevMsgCountRef.current) {
      const lastMsg = sortedMessages[sortedMessages.length - 1];
      const isSentByMe = Boolean(lastMsg?.sender_uid && currentUser?.uid && lastMsg.sender_uid === currentUser.uid);

      if (isSentByMe || isNearBottomRef.current) {
        scrollToBottom(false);
      } else {
        setShowUnreadPill(true);
      }
    }

    prevMsgCountRef.current = sortedMessages.length;
  }, [sortedMessages.length, context?.space.space_id, activeTag]);

  // Synchronous invalidation of in-flight operations on Space or Tag change and unmount
  useEffect(() => {
    const unsubSpace = spaceContextSignal.subscribe(() => {
      opGenerationRef.current++;
      setActiveSendingOpGen(null);
      uploadAbortRef.current?.abort();
      chatAbortRef.current?.abort();
    });
    const unsubTag = activeTagSignal.subscribe(() => {
      opGenerationRef.current++;
      setActiveSendingOpGen(null);
      uploadAbortRef.current?.abort();
      chatAbortRef.current?.abort();
    });
    return () => {
      unsubSpace();
      unsubTag();
      opGenerationRef.current++;
      setActiveSendingOpGen(null);
      uploadAbortRef.current?.abort();
      chatAbortRef.current?.abort();
    };
  }, []);

  // Listen to composerDraftSignal set by Discuss buttons across panels
  useEffect(() => {
    if (composerDraftSignal.value) {
      setInputText(composerDraftSignal.value);
      composerDraftSignal.value = '';
    }
  }, [composerDraftSignal.value]);

  // Drag & Drop Handlers
  const handleDragOver = (e: JSX.TargetedDragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    if (!isDraggingOver) setIsDraggingOver(true);
  };

  const handleDragLeave = (e: JSX.TargetedDragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingOver(false);
  };

  const handleDrop = (e: JSX.TargetedDragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingOver(false);
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) {
      const dropped = files[0];
      setSelectedFile(dropped);
      showToast(`Attached ${dropped.name} (Drag & Drop)`, 'info');
    }
  };

  const handleInsertShortcut = (textToInsert: string) => {
    setInputText((prev) => (prev.includes(textToInsert) ? prev : prev ? `${prev} ${textToInsert}` : `${textToInsert} `));
  };

  const handleSendMessage = async (e?: JSX.TargetedEvent) => {
    if (e) e.preventDefault();
    if ((!inputText.trim() && !selectedFile) || isSending || !context) return;

    const spaceId = context.space.space_id;
    const tag = activeTag === 'all' ? 'general' : activeTag;
    const content = inputText.trim();
    const fileToUpload = selectedFile;
    let intentToSend = composerIntentSignal.value;
    const latestReadyFileId = [...filesSignal.value]
      .filter((f) => f.ingestion_status === 'ready' || f.ingestion_status === 'ready_partial')
      .sort((a, b) => new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime())
      .at(-1)?.file_id;
    const contextFileIdsToSend = Array.from(
      new Set(
        [
          ...composerContextFileIdsSignal.value,
          ...(latestReadyFileId && intentToSend === 'document_qa' ? [latestReadyFileId] : []),
        ].filter(Boolean)
      )
    );
    const contextRunIdToSend =
      runsSignal.value.length > 0 && composerContextRunIdSignal.value
        ? (runsSignal.value.some((r) => r.run_id === composerContextRunIdSignal.value) ? composerContextRunIdSignal.value : null)
        : composerContextRunIdSignal.value;

    const currentOpGen = ++opGenerationRef.current;
    setActiveSendingOpGen(currentOpGen);
    const initialSpaceId = spaceId;
    const initialTag = tag;
    const isOpValid = () =>
      opGenerationRef.current === currentOpGen &&
      spaceContextSignal.value?.space?.space_id === initialSpaceId &&
      (activeTagSignal.value === 'all' ? 'general' : activeTagSignal.value) === initialTag;

    // Send-time intent resolution: Do not forcibly downgrade conversation to single-fact document_qa
    // Preserve conversation mode when discussing context files to enable multi-turn dialogue and summarization
    if (fileToUpload && intentToSend === 'conversation' && !contextFileIdsToSend.length) {
      // If user uploaded a new standalone file without text prompt, default to document_qa
      if (!content) {
        intentToSend = 'document_qa';
      }
    }

    // Ingestion quality gate: Proactively query backend if local signal is not ready yet
    if (intentToSend === 'document_qa' && contextFileIdsToSend.length > 0) {
      for (const fId of contextFileIdsToSend) {
        let fRec = filesSignal.value.find((f) => f.file_id === fId);
        if (!fRec || (fRec.ingestion_status !== 'ready' && fRec.ingestion_status !== 'ready_partial')) {
          try {
            const liveStatus = await api.getFileIngestionStatus(spaceId, fId);
            if (!isOpValid()) return;
            filesSignal.value = filesSignal.value.map((f) =>
              f.file_id === fId
                ? {
                    ...f,
                    ingestion_status: liveStatus.ingestion_status,
                    chunk_count: liveStatus.chunk_count,
                    extracted_pages: liveStatus.extracted_pages,
                    has_ocr_gaps: liveStatus.has_ocr_gaps,
                    ocr_gap_pages: liveStatus.ocr_gap_pages,
                  }
                : f
            );
            fRec = filesSignal.value.find((f) => f.file_id === fId);
          } catch {
            // Transient status check failure, proceed with local record
          }
        }

        if (fRec && fRec.ingestion_status && fRec.ingestion_status !== 'ready' && fRec.ingestion_status !== 'ready_partial') {
          const reason =
            fRec.ingestion_status === 'needs_ocr'
              ? 'requires OCR processing'
              : fRec.ingestion_status === 'failed'
              ? 'failed ingestion'
              : 'is still processing (please wait until indexing completes)';
          showToast(`Cannot ask document: "${fRec.filename}" ${reason}.`, 'error');
          if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
          return;
        }
      }
    }

    if (intentToSend === 'document_qa' && !fileToUpload) {
      for (const fId of contextFileIdsToSend) {
        let fRec = filesSignal.value.find((f) => f.file_id === fId);
        let currentStatus = fRec?.ingestion_status || '';
        let chunkCount = Number(fRec?.chunk_count || 0);
        if ((currentStatus === 'ready' || currentStatus === 'ready_partial') && chunkCount >= 1) {
          continue;
        }
        showToast(`Waiting for indexing of "${fRec?.filename || 'document'}" before sending the question…`, 'info');
        let pollAttempts = 0;
        const stillIndexing = () => {
          const ready = currentStatus === 'ready' || currentStatus === 'ready_partial';
          return currentStatus !== 'failed' && currentStatus !== 'needs_ocr' && (!ready || chunkCount < 1);
        };
        while (stillIndexing() && pollAttempts < 90) {
          try {
            const statusRes = await api.getFileIngestionStatus(spaceId, fId);
            if (!isOpValid()) {
              if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
              return;
            }
            currentStatus = statusRes.ingestion_status;
            chunkCount = Number(statusRes.chunk_count || 0);
            filesSignal.value = filesSignal.value.map((f) =>
              f.file_id === fId
                ? {
                    ...f,
                    ingestion_status: statusRes.ingestion_status,
                    chunk_count: statusRes.chunk_count,
                    extracted_pages: statusRes.extracted_pages,
                    has_ocr_gaps: statusRes.has_ocr_gaps,
                    ocr_gap_pages: statusRes.ocr_gap_pages,
                  }
                : f
            );
          } catch {
            // Transient status error: continue polling until timeout
          }
          if (!stillIndexing()) break;
          pollAttempts++;
          await new Promise((r) => setTimeout(r, 1000));
          if (!isOpValid()) {
            if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
            return;
          }
        }
        fRec = filesSignal.value.find((f) => f.file_id === fId);
        if (currentStatus === 'needs_ocr') {
          showToast(`Cannot ask document: "${fRec?.filename || 'document'}" requires OCR processing.`, 'error');
          if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
          return;
        }
        if (currentStatus === 'failed') {
          showToast(`Cannot ask document: "${fRec?.filename || 'document'}" failed ingestion.`, 'error');
          if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
          return;
        }
        if (currentStatus !== 'ready' && currentStatus !== 'ready_partial') {
          showToast(`"${fRec?.filename || 'document'}" is still processing. Please send once indexing completes.`, 'error');
          if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
          return;
        }
        if (chunkCount < 1) {
          showToast(`"${fRec?.filename || 'document'}" indexed with no readable text yet. Please retry shortly.`, 'error');
          if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
          return;
        }
      }
    }

        setInputText('');
    setSelectedFile(null);
    composerContextRunIdSignal.value = null;

    const clientMessageId = `cmsg_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
    const tempMsgId = clientMessageId;
    const messageContent = content || `[Uploaded file: ${fileToUpload?.name}]`;

    let attachedFileIds: string[] = [];
    let uploadedSuccessfully = false;
    let uploadedFileId: string | null = null;

    // Step 1: Upload file if selected
    if (fileToUpload) {
      const abortCtrl = new AbortController();
      uploadAbortRef.current = abortCtrl;
      setUploadProgress(0);

      try {
        const uploadedRec: FileRecord = await api.uploadFileWithProgress(
          fileToUpload,
          spaceId,
          tag,
          (percent) => {
            if (isOpValid() && uploadAbortRef.current === abortCtrl) setUploadProgress(percent);
          },
          abortCtrl.signal
        );

        if (!isOpValid()) {
          if (uploadAbortRef.current === abortCtrl) setUploadProgress(null);
          if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
          return;
        }

        uploadedSuccessfully = true;
        uploadedFileId = uploadedRec.file_id;
        filesSignal.value = [...filesSignal.value, uploadedRec];
        attachedFileIds.push(uploadedRec.file_id);
        showToast(`Uploaded ${fileToUpload.name}. Waiting for indexing before sending the question…`, 'info');

        let currentStatus = uploadedRec.ingestion_status || 'pending';
        let chunkCount = Number(uploadedRec.chunk_count || 0);
        let pollAttempts = 0;
        const stillIndexing = () => {
          const ready = currentStatus === 'ready' || currentStatus === 'ready_partial';
          return currentStatus !== 'failed' && currentStatus !== 'needs_ocr' && (!ready || chunkCount < 1);
        };
        while (stillIndexing() && pollAttempts < 90) {
          try {
            const statusRes = await api.getFileIngestionStatus(spaceId, uploadedRec.file_id);
            if (!isOpValid()) {
              if (uploadAbortRef.current === abortCtrl) setUploadProgress(null);
              if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
              return;
            }
            currentStatus = statusRes.ingestion_status;
            chunkCount = Number(statusRes.chunk_count || 0);
            filesSignal.value = filesSignal.value.map((f) =>
              f.file_id === uploadedRec.file_id
                ? {
                    ...f,
                    ingestion_status: statusRes.ingestion_status,
                    chunk_count: statusRes.chunk_count,
                    extracted_pages: statusRes.extracted_pages,
                    has_ocr_gaps: statusRes.has_ocr_gaps,
                    ocr_gap_pages: statusRes.ocr_gap_pages,
                  }
                : f
            );
          } catch {
            // Transient status error: continue polling until timeout
          }
          if (!stillIndexing()) break;
          pollAttempts++;
          await new Promise((r) => setTimeout(r, 1000));
          if (!isOpValid()) {
            if (uploadAbortRef.current === abortCtrl) setUploadProgress(null);
            if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
            return;
          }
        }

          if (!isOpValid()) {
            if (uploadAbortRef.current === abortCtrl) setUploadProgress(null);
            if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
            return;
          }

          if (currentStatus === 'needs_ocr') {
            throw new Error(`"${fileToUpload.name}" requires OCR processing before AI discussion.`);
          }
          if (currentStatus === 'failed') {
            throw new Error(`"${fileToUpload.name}" failed ingestion.`);
          }
          if (currentStatus !== 'ready' && currentStatus !== 'ready_partial') {
            throw new Error(`"${fileToUpload.name}" is still processing. Your draft is preserved; please send once indexing completes.`);
          }
          if (chunkCount < 1) {
            throw new Error(`"${fileToUpload.name}" indexed with no readable text yet. Your draft is preserved; please retry shortly.`);
          }
          composerContextFileIdsSignal.value = Array.from(
            new Set([...contextFileIdsToSend, uploadedRec.file_id])
          );
      } catch (err: any) {
        if (!isOpValid()) {
          if (uploadAbortRef.current === abortCtrl) setUploadProgress(null);
          if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
          return;
        }

        if (uploadAbortRef.current === abortCtrl) setUploadProgress(null);
        if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);

        // Restore composer draft
        setInputText(content);

        // If file was already uploaded to space but timed out indexing, retain file ID as context file and do NOT re-upload
        if (uploadedSuccessfully && uploadedFileId) {
          setSelectedFile(null);
          composerContextFileIdsSignal.value = Array.from(new Set([...contextFileIdsToSend, uploadedFileId]));
        } else {
          // If upload itself failed, retain selectedFile so user can re-upload
          setSelectedFile(fileToUpload);
          composerContextFileIdsSignal.value = contextFileIdsToSend;
        }

        composerContextRunIdSignal.value = contextRunIdToSend;
        composerIntentSignal.value = intentToSend;
        messagesSignal.value = messagesSignal.value.filter((m) => m.message_id !== tempMsgId);

        if (err.message !== 'Upload canceled') {
          showToast(`Document preparation error: ${err.message}`, 'error');
        }
        return;
      } finally {
        if (uploadAbortRef.current === abortCtrl) {
          setUploadProgress(null);
          uploadAbortRef.current = null;
        }
      }
    }

    if (!isOpValid()) {
      if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
      return;
    }

    const qaFileIds = Array.from(
      new Set([...attachedFileIds, ...contextFileIdsToSend, ...(uploadedFileId ? [uploadedFileId] : [])])
    );
    if (intentToSend === 'document_qa' && qaFileIds.length === 0) {
      showToast('Ask Document needs a file. Attach one with the paperclip, or wait until a Space file finishes indexing.', 'error');
      setInputText(content);
      setSelectedFile(fileToUpload);
      composerIntentSignal.value = intentToSend;
      if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
      return;
    }

    const chatAbort = new AbortController();
    chatAbortRef.current = chatAbort;

    const optimisticMsg: Message = {
      message_id: tempMsgId,
      client_message_id: clientMessageId,
      space_id: spaceId,
      sender_uid: currentUser?.uid || 'me',
      sender_name: currentUser?.display_name || currentUser?.email || 'Me',
      role: 'user',
      content: messageContent,
      project_tag: tag,
      attachment_file_ids: attachedFileIds,
      created_at: new Date().toISOString(),
      isSending: true,
    };
    messagesSignal.value = [...messagesSignal.value, optimisticMsg];

    try {
      const result = await api.chatWithAgent(
        spaceId,
        optimisticMsg.content,
        tag,
        qaFileIds,
        clientMessageId,
        intentToSend,
        qaFileIds,
        contextRunIdToSend,
        chatAbort.signal
      );

      if (!isOpValid()) {
        if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
        return;
      }

      // Replace optimistic message with confirmed user message + agent response
      const updatedList = messagesSignal.value.filter((m) => m.message_id !== tempMsgId);
      if (result.user_message) updatedList.push(result.user_message);
      if (result.agent_message) updatedList.push(result.agent_message);
      messagesSignal.value = updatedList;

      if (result.run) {
        runsSignal.value = [...runsSignal.value, result.run];
      }
    } catch (err: any) {
      if (!isOpValid()) {
        if (opGenerationRef.current === currentOpGen) setActiveSendingOpGen(null);
        return;
      }

      // Restore composer state on chat failure
      setInputText(content);
      if (attachedFileIds.length > 0) {
        composerContextFileIdsSignal.value = attachedFileIds;
      } else {
        composerContextFileIdsSignal.value = contextFileIdsToSend;
      }
      composerContextRunIdSignal.value = contextRunIdToSend;
      composerIntentSignal.value = intentToSend;

      messagesSignal.value = messagesSignal.value.map((m) =>
        m.message_id === tempMsgId
          ? {
              ...m,
              isSending: false,
              isFailed: true,
              attachment_file_ids: qaFileIds,
            }
          : m
      );
      if (err.name !== 'AbortError' && err.message !== 'The user aborted a request.') {
        showToast(`Failed to send message: ${err.message}`, 'error');
      }
    } finally {
      if (opGenerationRef.current === currentOpGen) {
        setActiveSendingOpGen(null);
      }
      if (chatAbortRef.current === chatAbort) {
        chatAbortRef.current = null;
      }
    }
  };

  const handleCancelUpload = () => {
    if (uploadAbortRef.current) {
      uploadAbortRef.current.abort();
    }
  };

  const handleRetryMessage = (failedMsg: Message) => {
    messagesSignal.value = messagesSignal.value.filter((m) => m.message_id !== failedMsg.message_id);
    setInputText(failedMsg.content);
    composerIntentSignal.value = 'document_qa';
    if (failedMsg.attachment_file_ids && failedMsg.attachment_file_ids.length > 0) {
      composerContextFileIdsSignal.value = [...failedMsg.attachment_file_ids];
      return;
    }
    const ready = [...filesSignal.value]
      .filter((f) => f.ingestion_status === 'ready' || f.ingestion_status === 'ready_partial')
      .sort((a, b) => new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime());
    if (ready.length > 0) {
      composerContextFileIdsSignal.value = [ready[ready.length - 1].file_id];
    }
  };

  if (messagesState === 'error') {
    return (
      <div class="chat-area error-state">
        <div class="error-card">
          <span class="error-icon">⚠️</span>
          <h3>Failed to load Messages</h3>
          <p>Could not retrieve message history for project tag "#{activeTag}".</p>
          <button
            class="btn-primary"
            onClick={() => {
              if (context?.space.space_id) {
                messagesStateSignal.value = 'loading';
                api
                  .listMessages(context.space.space_id, activeTag)
                  .then((msgs) => {
                    messagesSignal.value = msgs;
                    messagesStateSignal.value = 'ready';
                  })
                  .catch(() => (messagesStateSignal.value = 'error'));
              }
            }}
          >
            ↻ Retry Loading Messages
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      class={`chat-area ${isDraggingOver ? 'dragging-over' : ''}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* Drag & Drop Visual Overlay */}
      {isDraggingOver && (
        <div class="drag-drop-overlay">
          <div class="drag-drop-box">
            <span class="drag-icon">📄</span>
            <h3>Drop Script, Screenplay, or Production Document Here</h3>
            <p>Release to attach to your chat message</p>
          </div>
        </div>
      )}

      {/* Unread Messages Floating Pill */}
      {showUnreadPill && (
        <button class="unread-messages-pill" onClick={() => scrollToBottom(true)}>
          ↓ New messages
        </button>
      )}

      {/* Chat Messages Stream */}
      <div class="chat-stream" ref={scrollContainerRef} onScroll={handleScroll}>
        {hasMoreMessages && (
          <div class="load-earlier-container" style={{ textAlign: 'center', margin: '8px 0 12px' }}>
            <button
              type="button"
              class="btn-compact btn-secondary"
              disabled={isLoadingEarlier}
              onClick={handleLoadEarlier}
              style={{ fontSize: '0.85rem', padding: '4px 14px', borderRadius: '16px' }}
            >
              {isLoadingEarlier ? '⏳ Loading earlier messages...' : '↑ Load earlier messages'}
            </button>
          </div>
        )}

        {sortedMessages.length === 0 ? (
          <div class="empty-chat-placeholder">
            <div class="placeholder-content">
              <span class="placeholder-icon">🎬</span>
              <h3>Welcome to #{activeTag === 'all' ? 'general' : activeTag} track</h3>
              <p>
                {context?.space.kind === 'agent_dm'
                  ? 'Chat directly with your StudioTower Film Prep Assistant. Drop a screenplay (PDF, FDX, Fountain, text) or type a scene outline.'
                  : 'Collaborate with your film production crew and AI Assistant. Type @agent or attach a screenplay (PDF, FDX, Fountain, text) to generate scene breakdowns.'}
              </p>
            </div>
          </div>
        ) : (
          sortedMessages.map((msg) => (
            <div
              key={msg.message_id}
              id={`msg-${msg.message_id}`}
              data-message-id={msg.message_id}
              data-message-role={msg.role}
              class={`message-item message-${msg.role} ${msg.isSending ? 'sending' : ''} ${msg.isFailed ? 'failed' : ''}`}
            >
              <div class="message-avatar">
                {msg.role === 'agent' ? '🤖' : (msg.sender_name || 'U')[0].toUpperCase()}
              </div>
              <div class="message-bubble-wrapper">
                <div class="message-sender-line">
                  <span class="message-sender-name">{msg.sender_name || 'User'}</span>
                  <span class="message-time">
                    {new Date(msg.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                  </span>
                  {msg.project_tag && msg.project_tag !== activeTag && msg.project_tag !== 'general' && (
                    <span class="message-tag-badge">#{msg.project_tag}</span>
                  )}
                </div>

                {renderMessageContentWithCitations(msg, setActiveCitation)}

                {msg.proposed_action && (
                  <ActionProposalCard
                    proposal={msg.proposed_action}
                    message={msg}
                    spaceId={context?.space.space_id || ''}
                  />
                )}

                {msg.isFailed && (
                  <div class="message-failed-tag">
                    <span>⚠️ Failed to deliver</span>
                    <button
                      class="btn-text-retry"
                      onClick={() => handleRetryMessage(msg)}
                    >
                      Retry
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))
        )}
      </div>

      {/* Upload Progress Bar */}
      {uploadProgress !== null && (
        <div class="upload-progress-bar-container">
          <div class="upload-progress-info">
            <span>Uploading {selectedFile?.name}... ({uploadProgress}%)</span>
            <button class="btn-cancel-upload" onClick={handleCancelUpload}>✕ Cancel</button>
          </div>
          <div class="upload-progress-track">
            <div class="upload-progress-fill" style={{ width: `${uploadProgress}%` }} />
          </div>
        </div>
      )}

      {/* Context Chips (e.g. Discussed Files) */}
      {composerContextFileIdsSignal.value.length > 0 && (
        <div class="composer-context-chips">
          <span class="shortcuts-label">Context:</span>
          {composerContextFileIdsSignal.value.map((fid) => {
            const fRec = filesSignal.value.find((f) => f.file_id === fid);
            const fname = fRec ? fRec.filename : fid;
            return (
              <span key={fid} class="context-chip" data-file-id={fid}>
                📄 {fname}
                {fRec?.ingestion_status === 'ready_partial' && (
                  <span style={{ color: '#facc15', fontSize: '0.8em', marginLeft: '4px' }} title="Partial OCR: some pages omitted">
                    (⚠️ Partial OCR)
                  </span>
                )}
                {fRec?.ingestion_status === 'needs_ocr' && (
                  <span style={{ color: '#fb923c', fontSize: '0.8em', marginLeft: '4px' }} title="Scanned document requires OCR">
                    (🔍 Needs OCR)
                  </span>
                )}
                {fRec?.ingestion_status === 'failed' && (
                  <span style={{ color: '#f87171', fontSize: '0.8em', marginLeft: '4px' }} title="Ingestion failed">
                    (✕ Failed)
                  </span>
                )}
                <button
                  type="button"
                  class="context-chip-remove"
                  onClick={() => {
                    composerContextFileIdsSignal.value = composerContextFileIdsSignal.value.filter((id) => id !== fid);
                  }}
                  title="Remove context attachment"
                >
                  ✕
                </button>
              </span>
            );
          })}
        </div>
      )}

      {/* Intent Mode Selector Bar */}
      <div class="intent-mode-bar">
        <span class="shortcuts-label">AI Mode:</span>
        <button
          type="button"
          data-intent="conversation"
          class={`intent-mode-btn ${composerIntentSignal.value === 'conversation' ? 'active' : ''}`}
          onClick={() => (composerIntentSignal.value = 'conversation')}
          title="Direct conversational response without automated breakdown runs"
        >
          💬 Ask AI
        </button>
        <button
          type="button"
          data-intent="document_qa"
          class={`intent-mode-btn ${composerIntentSignal.value === 'document_qa' ? 'active' : ''}`}
          onClick={() => (composerIntentSignal.value = 'document_qa')}
          title="Question & Answer grounded on the attached/referenced script"
        >
          📄 Ask Document
        </button>
        <button
          type="button"
          data-intent="create_breakdown"
          class={`intent-mode-btn ${composerIntentSignal.value === 'create_breakdown' ? 'active' : ''}`}
          onClick={() => (composerIntentSignal.value = 'create_breakdown')}
          title="Trigger full deterministic Scene Breakdown with conflict detection and Approval Gate"
        >
          🎬 Run Breakdown
        </button>
      </div>

      {/* Quick Mention & Tag Shortcuts Bar */}
      <div class="composer-shortcuts-bar">
        <span class="shortcuts-label">Quick:</span>
        <button
          type="button"
          class="pill-btn shortcut-agent-btn"
          onClick={() => handleInsertShortcut('@agent')}
          title="Mention AI Agent — it will read or analyze your message"
        >
          🤖 @agent
        </button>
        <button
          type="button"
          class="pill-btn shortcut-tag-btn"
          onClick={() => handleInsertShortcut('#stunt')}
          title="Tag this message under the Stunt track"
        >
          🎭 #stunt
        </button>
        <button
          type="button"
          class="pill-btn shortcut-tag-btn"
          onClick={() => handleInsertShortcut('#vfx')}
          title="Tag this message under the VFX track"
        >
          ✨ #vfx
        </button>
        <button
          type="button"
          class="pill-btn shortcut-tag-btn"
          onClick={() => handleInsertShortcut('#schedule')}
          title="Tag this message under the Schedule track"
        >
          📅 #schedule
        </button>
      </div>

      {/* Message Composer */}
      <form class="chat-composer" onSubmit={handleSendMessage}>
        <input
          type="file"
          ref={fileInputRef}
          style={{ display: 'none' }}
          accept=".pdf,.fdx,.fountain,.doc,.docx,.xlsx,.csv,.txt,.pptx"
          onChange={(e) => {
            const file = (e.target as HTMLInputElement).files?.[0];
            if (file) {
              setSelectedFile(file);
              composerIntentSignal.value = 'document_qa';
            }
          }}
        />

        <button
          type="button"
          class={`btn-attach ${selectedFile ? 'has-file' : ''}`}
          onClick={() => fileInputRef.current?.click()}
          title={selectedFile ? `Attached: ${selectedFile.name}` : 'Attach Screenplay (PDF, FDX, Fountain) / Document'}
          aria-label="Attach File"
        >
          📎 {selectedFile ? `(${selectedFile.name.substring(0, 12)}...)` : ''}
        </button>

        {filesStateSignal.value === 'error' && (
          <span class="files-error-badge" title="Failed to sync space file library">
            ⚠️ File Sync Error
          </span>
        )}

        <input
          id="chat-input-field"
          class="chat-input"
          placeholder={
            context?.space.kind === 'agent_dm'
              ? 'Message StudioTower Assistant (AI responds automatically)...'
              : `Message #${activeTag}... (Use @agent to invoke AI)`
          }
          value={inputText}
          onInput={(e) => setInputText((e.target as HTMLInputElement).value)}
          disabled={isSending}
          autoFocus
        />

        <button
          type="submit"
          id="btn-send-message"
          class="btn-primary btn-send"
          disabled={isSending || (!inputText.trim() && !selectedFile)}
        >
          {isSending ? '⏳' : 'Send'}
        </button>
      </form>

      {/* Document Source Text Preview Drawer */}
      <DocumentPreviewDrawer
        citation={activeCitation}
        spaceId={context?.space.space_id || ''}
        onClose={() => setActiveCitation(null)}
      />
    </div>
  );
}

function renderMessageContentWithCitations(
  msg: Message,
  onSelectCitation: (cit: Citation) => void
): JSX.Element {
  return (
    <MarkdownRenderer
      content={msg.content}
      citations={msg.citations}
      onSelectCitation={onSelectCitation}
    />
  );
}

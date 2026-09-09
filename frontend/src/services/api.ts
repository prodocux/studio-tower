import {
  Space,
  SpaceContext,
  MemberInfo,
  Invite,
  Message,
  FileRecord,
  FileIngestionStatusResponse,
  DocumentChunkSummary,
  DocumentChunksResponse,
  Run,
  LineageGraph,
  MembershipRole,
  User,
  ActivityListResponse,
  ActionConfirmationResponse,
  MessagePageResponse,
  RunTraceResponse,
  DiagnosisRecord,
  TelemetryVerificationResult,
  TelemetryMetricsSummary,
  InvitePreviewResponse,
  GrafanaVisualBoard,
} from '../types';
import { authManager } from './auth';

export class ApiClient {
  private baseUrl: string;

  constructor(baseUrl: string = '') {
    this.baseUrl = baseUrl;
  }

  private async request<T>(path: string, options: RequestInit = {}): Promise<T> {
    const token = await authManager.getToken(false);
    const headers: Record<string, string> = {
      ...(options.headers as Record<string, string>),
    };

    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    if (!(options.body instanceof FormData) && !headers['Content-Type']) {
      headers['Content-Type'] = 'application/json';
    }

    let response = await fetch(`${this.baseUrl}${path}`, {
      ...options,
      headers,
    });

    // Single-Flight 401 Token Refresh and Single Retry
    if (response.status === 401) {
      const refreshedToken = await authManager.handle401Refresh();
      if (refreshedToken) {
        headers['Authorization'] = `Bearer ${refreshedToken}`;
        response = await fetch(`${this.baseUrl}${path}`, {
          ...options,
          headers,
        });
      } else {
        await authManager.signOut();
        window.dispatchEvent(new CustomEvent('auth-expired'));
        throw new Error('Unauthorized');
      }
    }

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      const err = new Error(errorData.detail || `Request failed with status ${response.status}`);
      (err as any).status = response.status;
      if (response.status === 429) {
        const retryAfterHeader = response.headers?.get('Retry-After');
        if (retryAfterHeader) {
          const parsed = parseInt(retryAfterHeader, 10);
          if (Number.isFinite(parsed) && parsed > 0) {
            (err as any).retryAfter = parsed;
          }
        }
      }
      throw err;
    }

    return response.json();
  }

  // Auth / Current User
  async getMe(): Promise<any> {
    return this.request('/v1/me');
  }

  // Space Context & Governance
  async listSpaces(): Promise<Space[]> {
    return this.request<Space[]>('/v1/spaces');
  }

  async createSpace(name: string): Promise<Space> {
    return this.request<Space>('/v1/spaces', {
      method: 'POST',
      body: JSON.stringify({ name }),
    });
  }

  async getSpaceContext(spaceId: string, signal?: AbortSignal): Promise<SpaceContext> {
    return this.request<SpaceContext>(`/v1/spaces/${spaceId}/context`, { signal });
  }

  async listMembers(spaceId: string): Promise<MemberInfo[]> {
    return this.request<MemberInfo[]>(`/v1/spaces/${spaceId}/members`);
  }

  async updateMemberRole(spaceId: string, uid: string, role: MembershipRole): Promise<any> {
    return this.request(`/v1/spaces/${spaceId}/members/${uid}/role`, {
      method: 'PATCH',
      body: JSON.stringify({ role }),
    });
  }

  async removeMember(spaceId: string, uid: string): Promise<any> {
    return this.request(`/v1/spaces/${spaceId}/members/${uid}`, {
      method: 'DELETE',
    });
  }

  async searchUsers(query: string): Promise<User[]> {
    return this.request<User[]>(`/v1/users/search?q=${encodeURIComponent(query)}`);
  }

  async directAddMember(spaceId: string, emailOrUid: string, role: string): Promise<MemberInfo> {
    return this.request<MemberInfo>(`/v1/spaces/${spaceId}/members/direct-add`, {
      method: 'POST',
      body: JSON.stringify({ email_or_uid: emailOrUid, role }),
    });
  }

  async transferOwnership(spaceId: string, newOwnerUid: string): Promise<any> {
    return this.request(`/v1/spaces/${spaceId}/transfer-ownership`, {
      method: 'POST',
      body: JSON.stringify({ new_owner_uid: newOwnerUid }),
    });
  }

  async listInvites(spaceId: string): Promise<Invite[]> {
    return this.request<Invite[]>(`/v1/spaces/${spaceId}/invites`);
  }

  async createInvite(spaceId: string, payload: { role: MembershipRole; target_email?: string | null; max_uses?: number }): Promise<Invite> {
    return this.request<Invite>(`/v1/spaces/${spaceId}/invites`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  async getInvitePreview(token: string, signal?: AbortSignal): Promise<InvitePreviewResponse> {
    return this.request<InvitePreviewResponse>(`/v1/invites/${token}/preview`, {
      method: 'GET',
      signal,
    });
  }

  async acceptInvite(token: string): Promise<Space> {
    return this.request<Space>(`/v1/invites/${token}/accept`, {
      method: 'POST',
    });
  }

  async revokeInvite(token: string): Promise<Invite> {
    return this.request<Invite>(`/v1/invites/${token}/revoke`, {
      method: 'POST',
    });
  }

  async leaveSpace(spaceId: string): Promise<any> {
    return this.request(`/v1/spaces/${spaceId}/leave`, {
      method: 'POST',
    });
  }

  async addTag(spaceId: string, tag: { name: string; slug: string; color: string; description?: string }): Promise<Space> {
    return this.request<Space>(`/v1/spaces/${spaceId}/tags`, {
      method: 'POST',
      body: JSON.stringify(tag),
    });
  }

  async updateTag(
    spaceId: string,
    tagId: string,
    payload: { name?: string; color?: string; description?: string }
  ): Promise<Space> {
    return this.request<Space>(`/v1/spaces/${spaceId}/tags/${tagId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
  }

  async archiveTag(spaceId: string, tagId: string, archived = true): Promise<Space> {
    return this.request<Space>(`/v1/spaces/${spaceId}/tags/${tagId}/archive`, {
      method: 'POST',
      body: JSON.stringify({ archived }),
    });
  }

  async listActivity(
    spaceId: string,
    tag?: string,
    cursor?: string,
    limit = 50,
    signal?: AbortSignal
  ): Promise<ActivityListResponse> {
    const params = new URLSearchParams();
    if (tag && tag !== 'all') params.append('tag', tag);
    if (cursor) params.append('cursor', cursor);
    if (limit) params.append('limit', limit.toString());
    const query = params.toString() ? `?${params.toString()}` : '';
    return this.request<ActivityListResponse>(`/v1/spaces/${spaceId}/activity${query}`, { signal });
  }

  // Messages
  async listMessages(spaceId: string, tag?: string, signal?: AbortSignal): Promise<Message[]> {
    const query = tag && tag !== 'all' ? `?tag=${encodeURIComponent(tag)}` : '';
    return this.request<Message[]>(`/v1/spaces/${spaceId}/messages${query}`, { signal });
  }

  async listMessagesPage(
    spaceId: string,
    tag?: string,
    limit = 30,
    cursor?: string,
    signal?: AbortSignal
  ): Promise<MessagePageResponse> {
    const params = new URLSearchParams();
    if (tag && tag !== 'all') params.append('tag', tag);
    if (limit) params.append('limit', limit.toString());
    if (cursor) params.append('cursor', cursor);
    const query = params.toString() ? `?${params.toString()}` : '';
    return this.request<MessagePageResponse>(`/v1/spaces/${spaceId}/messages/page${query}`, { signal });
  }

  async confirmAction(
    spaceId: string,
    actionId: string,
    signal?: AbortSignal
  ): Promise<ActionConfirmationResponse> {
    return this.request<ActionConfirmationResponse>(`/v1/spaces/${spaceId}/actions/confirm`, {
      method: 'POST',
      signal,
      body: JSON.stringify({ action_id: actionId }),
    });
  }

  async postMessage(spaceId: string, content: string, tag?: string, attachmentFileIds?: string[]): Promise<Message> {
    return this.request<Message>(`/v1/spaces/${spaceId}/messages`, {
      method: 'POST',
      body: JSON.stringify({
        content,
        project_tag: tag || 'general',
        attachment_file_ids: attachmentFileIds || [],
      }),
    });
  }

  async chatWithAgent(
    spaceId: string,
    content: string,
    tag?: string,
    attachmentFileIds?: string[],
    clientMessageId?: string,
    intent?: 'conversation' | 'document_qa' | 'create_breakdown',
    contextFileIds?: string[],
    contextRunId?: string | null,
    signal?: AbortSignal
  ): Promise<{ user_message: Message; agent_message?: Message; run?: Run }> {
    return this.request('/v1/chat', {
      method: 'POST',
      signal,
      body: JSON.stringify({
        space_id: spaceId,
        content,
        project_tag: tag || 'general',
        attachment_file_ids: attachmentFileIds || [],
        context_file_ids: contextFileIds || [],
        client_message_id: clientMessageId,
        intent: intent ?? 'conversation',
        context_run_id: contextRunId || undefined,
      }),
    });
  }

  // Files & Upload with Progress and Cancellation
  async listFiles(spaceId: string, tag?: string, signal?: AbortSignal): Promise<FileRecord[]> {
    const query = tag && tag !== 'all' ? `?tag=${encodeURIComponent(tag)}` : '';
    return this.request<FileRecord[]>(`/v1/spaces/${spaceId}/files${query}`, { signal });
  }

  async deleteFile(spaceId: string, fileId: string): Promise<any> {
    return this.request(`/v1/spaces/${spaceId}/files/${fileId}`, {
      method: 'DELETE',
    });
  }

  async downloadFile(spaceId: string, fileId: string, filename: string): Promise<void> {
    const token = await authManager.getToken(false);
    const headers: Record<string, string> = {};
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    let res = await fetch(`${this.baseUrl}/v1/spaces/${spaceId}/files/${fileId}/download`, {
      headers,
    });

    // Single-Flight 401 Token Refresh and Single Retry
    if (res.status === 401) {
      const refreshedToken = await authManager.handle401Refresh();
      if (refreshedToken) {
        headers['Authorization'] = `Bearer ${refreshedToken}`;
        res = await fetch(`${this.baseUrl}/v1/spaces/${spaceId}/files/${fileId}/download`, {
          headers,
        });
      } else {
        await authManager.signOut();
        window.dispatchEvent(new CustomEvent('auth-expired'));
        throw new Error('Unauthorized');
      }
    }

    if (!res.ok) {
      throw new Error(`Download failed with status ${res.status}`);
    }

    const blob = await res.blob();
    const blobUrl = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = blobUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(() => window.URL.revokeObjectURL(blobUrl), 1000);
  }

  // Document Ingestion & Chunks
  async getFileIngestionStatus(spaceId: string, fileId: string, signal?: AbortSignal): Promise<FileIngestionStatusResponse> {
    return this.request<FileIngestionStatusResponse>(`/v1/spaces/${spaceId}/files/${fileId}/status`, { signal });
  }

  async getFileDocumentChunks(
    spaceId: string,
    fileId: string,
    params?: { cursor?: number; limit?: number; page?: number; section?: string },
    signal?: AbortSignal
  ): Promise<DocumentChunksResponse> {
    const q = new URLSearchParams();
    if (params?.cursor !== undefined) q.set('cursor', String(params.cursor));
    if (params?.limit !== undefined) q.set('limit', String(params.limit));
    if (params?.page !== undefined) q.set('page', String(params.page));
    if (params?.section) q.set('section', params.section);
    const queryStr = q.toString() ? `?${q.toString()}` : '';
    return this.request<DocumentChunksResponse>(`/v1/spaces/${spaceId}/files/${fileId}/chunks${queryStr}`, { signal });
  }

  async getCitationChunk(
    spaceId: string,
    fileId: string,
    chunkId: string,
    generation: number,
    signal?: AbortSignal
  ): Promise<DocumentChunkSummary> {
    return this.request<DocumentChunkSummary>(
      `/v1/spaces/${spaceId}/files/${fileId}/citations/${chunkId}?generation=${generation}`,
      { signal }
    );
  }

  async reindexFile(spaceId: string, fileId: string): Promise<{ status: string; job_id: string; file_id: string }> {
    return this.request<{ status: string; job_id: string; file_id: string }>(`/v1/spaces/${spaceId}/files/${fileId}/reindex`, {
      method: 'POST',
    });
  }

  uploadFileWithProgress(
    file: File,
    spaceId: string,
    tag?: string,
    onProgress?: (percent: number) => void,
    abortSignal?: AbortSignal
  ): Promise<FileRecord> {
    return authManager.getToken(false).then((initialToken) => {
      return new Promise<FileRecord>((resolve, reject) => {
        const formData = new FormData();
        formData.append('file', file);
        if (tag && tag !== 'all') {
          formData.append('project_tag', tag);
        }

        let currentOnAbort: (() => void) | null = null;

        const removeAbortListener = () => {
          if (abortSignal && currentOnAbort) {
            abortSignal.removeEventListener('abort', currentOnAbort);
            currentOnAbort = null;
          }
        };

        const safeResolve = (data: FileRecord) => {
          removeAbortListener();
          resolve(data);
        };

        const safeReject = (err: Error) => {
          removeAbortListener();
          reject(err);
        };

        const sendXhr = (authToken: string | null, isRetry: boolean) => {
          if (abortSignal?.aborted) {
            return safeReject(new Error('Upload canceled'));
          }

          const xhr = new XMLHttpRequest();

          if (abortSignal) {
            removeAbortListener();
            currentOnAbort = () => {
              xhr.abort();
              safeReject(new Error('Upload canceled'));
            };
            abortSignal.addEventListener('abort', currentOnAbort, { once: true });
          }

          xhr.upload.onprogress = (event) => {
            if (event.lengthComputable && onProgress) {
              const percent = Math.round((event.loaded / event.total) * 100);
              onProgress(percent);
            }
          };

          xhr.onload = () => {
            if (xhr.status === 401 && !isRetry) {
              authManager
                .handle401Refresh()
                .then(async (refreshed) => {
                  if (refreshed) {
                    sendXhr(refreshed, true);
                  } else {
                    await authManager.signOut();
                    window.dispatchEvent(new CustomEvent('auth-expired'));
                    safeReject(new Error('Unauthorized'));
                  }
                })
                .catch((refreshErr) => {
                  safeReject(refreshErr instanceof Error ? refreshErr : new Error('Token refresh failed'));
                });
              return;
            }

            if (xhr.status >= 200 && xhr.status < 300) {
              try {
                const data = JSON.parse(xhr.responseText);
                safeResolve(data);
              } catch {
                safeReject(new Error('Invalid upload response'));
              }
            } else {
              try {
                const err = JSON.parse(xhr.responseText);
                safeReject(new Error(err.detail || `Upload failed with status ${xhr.status}`));
              } catch {
                safeReject(new Error(`Upload failed with status ${xhr.status}`));
              }
            }
          };

          xhr.onerror = () => safeReject(new Error('Network error during file upload'));
          xhr.onabort = () => safeReject(new Error('Upload aborted'));

          xhr.open('POST', `${this.baseUrl}/v1/spaces/${spaceId}/files`);
          if (authToken) {
            xhr.setRequestHeader('Authorization', `Bearer ${authToken}`);
          }
          xhr.send(formData);
        };

        sendXhr(initialToken, false);
      });
    });
  }

  async getChunks(
    spaceId: string,
    fileId: string,
    generation: number,
    cursor = 0,
    limit = 50,
    signal?: AbortSignal
  ): Promise<{ items: any[]; total: number; has_more: boolean; next_cursor?: number }> {
    return this.request(
      `/v1/spaces/${spaceId}/files/${fileId}/chunks?generation=${generation}&cursor=${cursor}&limit=${limit}`,
      { signal }
    );
  }

  // Runs & Telemetry
  async listRuns(spaceId: string, tag?: string, signal?: AbortSignal): Promise<Run[]> {
    const query = tag && tag !== 'all' ? `?tag=${encodeURIComponent(tag)}` : '';
    return this.request<Run[]>(`/v1/spaces/${spaceId}/runs${query}`, { signal });
  }

  async approveRun(spaceId: string, runId: string, approved: boolean, rejectionReason?: string): Promise<Run> {
    return this.request<Run>(`/v1/spaces/${spaceId}/runs/${runId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ approved, rejection_reason: rejectionReason }),
    });
  }

  async retryRun(spaceId: string, runId: string, signal?: AbortSignal): Promise<Run> {
    return this.request<Run>(`/v1/spaces/${spaceId}/runs/${runId}/retry`, {
      method: 'POST',
      signal,
    });
  }

  async diagnoseRun(spaceId: string, runId: string, signal?: AbortSignal): Promise<DiagnosisRecord> {
    return this.request<DiagnosisRecord>(`/v1/spaces/${spaceId}/runs/${runId}/diagnose`, {
      method: 'POST',
      signal,
    });
  }

  async getRunTrace(spaceId: string, runId: string, signal?: AbortSignal): Promise<RunTraceResponse> {
    return this.request<RunTraceResponse>(`/v1/spaces/${spaceId}/runs/${runId}/trace`, {
      signal,
    });
  }

  async verifyRunTelemetry(
    spaceId: string,
    runId: string,
    force: boolean = false,
    signal?: AbortSignal
  ): Promise<TelemetryVerificationResult> {
    return this.request<TelemetryVerificationResult>(
      `/v1/spaces/${spaceId}/runs/${runId}/verify-telemetry`,
      {
        method: 'POST',
        body: JSON.stringify({ force: Boolean(force) }),
        signal,
      }
    );
  }

  async getSpaceMetrics(
    spaceId: string,
    timeWindowHours: number = 24,
    projectTag?: string,
    signal?: AbortSignal
  ): Promise<TelemetryMetricsSummary> {
    const params = new URLSearchParams();
    params.set('time_window_hours', String(timeWindowHours));
    if (projectTag && projectTag !== 'all') {
      params.set('project_tag', projectTag);
    }
    return this.request<TelemetryMetricsSummary>(
      `/v1/spaces/${spaceId}/telemetry/metrics?${params.toString()}`,
      { signal }
    );
  }

  async getGrafanaVisual(spaceId: string, signal?: AbortSignal): Promise<GrafanaVisualBoard> {
    return this.request<GrafanaVisualBoard>(`/v1/spaces/${spaceId}/grafana/visual`, { signal });
  }

  // Lineage DAG
  async getLineage(spaceId: string, tag?: string, signal?: AbortSignal): Promise<LineageGraph> {
    const query = tag && tag !== 'all' ? `?tag=${encodeURIComponent(tag)}` : '';
    return this.request<LineageGraph>(`/v1/spaces/${spaceId}/lineage${query}`, { signal });
  }
}

export const api = new ApiClient();

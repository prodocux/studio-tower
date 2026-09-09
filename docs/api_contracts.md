# StudioTower API Contracts Specification (Phase 0 Deliverable)

> **Document Version**: 1.0.0  
> **Target Release**: StudioTower 0.2.0  
> **Security Standard**: Strict Multi-Tenant Isolation & Capabilities-Driven Governance

---

## 1. 認證與全域錯誤契約 (Auth & Error Schema)

### 1.1 Authentication Header
所有受保護 API 請求必須包含標準 Bearer Token：
```http
Authorization: Bearer <Firebase_ID_Token_or_Dev_Token>
```

### 1.2 Unified Error Response Schema
```json
{
  "detail": "Human-readable sanitized error description",
  "error_code": "RESOURCE_NOT_FOUND | PERMISSION_DENIED | CONFLICT | VALIDATION_ERROR | STORAGE_UNAVAILABLE",
  "retryable": false
}
```

---

## 2. 空間上下文與治理 API 契約 (Space Context & Governance)

### 2.1 GET `/v1/spaces/{space_id}/context`
- **目的**：提供 Header 與 Workspace 渲染所需之即時上下文與動態 Capabilities。
- **權限**：該 Space 之有效成員（Owner, Admin, Coordinator, Member）。
- **Response 200 OK**：
```json
{
  "space": {
    "space_id": "space_abc123",
    "name": "Project Bersama - Unit 1",
    "kind": "shared_space",
    "created_by": "steven_01",
    "created_at": "2026-08-10T08:00:00Z",
    "tags": [
      {
        "name": "General",
        "slug": "general",
        "color": "#64748B",
        "description": "General space updates"
      },
      {
        "name": "Block A",
        "slug": "block-a",
        "color": "#3B82F6",
        "description": "Main unit shooting"
      }
    ]
  },
  "current_user_role": "coordinator",
  "member_count": 4,
  "capabilities": {
    "can_invite": true,
    "can_manage_members": false,
    "can_change_role": false,
    "can_remove_member": false,
    "can_transfer_ownership": false,
    "can_approve_runs": true,
    "can_manage_tags": true,
    "can_leave_space": true
  }
}
```

---

### 2.2 GET `/v1/spaces/{space_id}/members`
- **目的**：取得 Space 所有成員之名單與角色。
- **權限**：該 Space 之有效成員。
- **Response 200 OK**：
```json
[
  {
    "uid": "steven_01",
    "display_name": "Steven Wu",
    "email": "steven@example.com",
    "role": "owner",
    "joined_at": "2026-08-10T08:00:00Z"
  },
  {
    "uid": "alice_02",
    "display_name": "Alice Lin",
    "email": "alice@example.com",
    "role": "coordinator",
    "joined_at": "2026-08-10T08:15:00Z"
  },
  {
    "uid": "bob_03",
    "display_name": "Bob Chen",
    "email": "bob@example.com",
    "role": "member",
    "joined_at": "2026-08-10T08:30:00Z"
  }
]
```

---

### 2.3 PATCH `/v1/spaces/{space_id}/members/{uid}/role`
- **目的**：變更成員角色。
- **權限**：
  - `OWNER`：可修改任何非 Owner 成員之角色（可提升為 Admin, Coordinator, Member）。
  - `ADMIN`：可修改 Coordinator 與 Member 之角色。
  - `COORDINATOR` / `MEMBER`：無權操作（回傳 403 Forbidden）。
- **Request Body**：
```json
{
  "role": "coordinator"
}
```
- **Response 200 OK**：
```json
{
  "status": "updated",
  "space_id": "space_abc123",
  "uid": "bob_03",
  "new_role": "coordinator"
}
```

---

### 2.4 DELETE `/v1/spaces/{space_id}/members/{uid}`
- **目的**：將成員移出 Space。
- **權限**：
  - `OWNER`：可移除除自己以外之所有成員。
  - `ADMIN`：可移除 Coordinator 與 Member。
  - 嚴禁移除 Owner（回傳 400 Bad Request: "Cannot remove Space Owner without ownership transfer"）。
- **Response 200 OK**：
```json
{
  "status": "removed",
  "space_id": "space_abc123",
  "uid": "bob_03"
}
```

---

### 2.5 POST `/v1/spaces/{space_id}/transfer-ownership`
- **目的**：移交 Space 擁有權。
- **權限**：僅限目前 `OWNER`。
- **Request Body**：
```json
{
  "new_owner_uid": "alice_02"
}
```
- **Response 200 OK**：
```json
{
  "status": "transferred",
  "space_id": "space_abc123",
  "previous_owner_uid": "steven_01",
  "new_owner_uid": "alice_02",
  "previous_owner_new_role": "admin"
}
```

---

### 2.6 GET `/v1/spaces/{space_id}/invites`
- **目的**：列出該 Space 目前有效之邀請連結清單。
- **權限**：具備 `can_invite` 權限之角色（Owner, Admin, Coordinator）。
- **Response 200 OK**：
```json
[
  {
    "token": "inv_7a9f8b2c",
    "space_id": "space_abc123",
    "created_by": "steven_01",
    "role": "coordinator",
    "target_email": "bob@example.com",
    "max_uses": 1,
    "used_count": 0,
    "expires_at": "2026-08-17T08:00:00Z",
    "created_at": "2026-08-10T08:00:00Z",
    "revoked_at": null
  }
]
```

---

## 3. 真實 Lineage DAG API 契約 (Artifact Lineage)

### 3.1 GET `/v1/spaces/{space_id}/lineage?tag={tag_slug}`
- **目的**：從資料庫中真實存在的檔案、Run 與 Gate 關聯動態構建 DAG 圖結構。
- **權限**：該 Space 之有效成員。
- **Response 200 OK (無假節點，空時 nodes/edges 為空陣列)**：
```json
{
  "space_id": "space_abc123",
  "tag": "general",
  "nodes": [
    {
      "id": "file_src_01",
      "type": "source",
      "label": "Treatment_Scene4_v2.pdf",
      "status": "committed",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "size_bytes": 1048576,
      "uploaded_by": "steven_01",
      "created_at": "2026-08-10T08:00:00Z"
    },
    {
      "id": "run_01",
      "type": "run",
      "label": "Gemini Scene Breakdown #4",
      "status": "awaiting_approval",
      "failure_code": null,
      "trace_id": "trace_9f8a7b",
      "created_at": "2026-08-10T08:01:00Z"
    },
    {
      "id": "gate_01",
      "type": "gate",
      "label": "High-Risk Helicopter Stunt Approval",
      "status": "pending",
      "required_role": "coordinator",
      "created_at": "2026-08-10T08:01:00Z"
    },
    {
      "id": "file_art_01",
      "type": "artifact",
      "label": "pdx_schedule_manifest.json",
      "status": "committed",
      "sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
      "size_bytes": 4096,
      "uploaded_by": "system",
      "created_at": "2026-08-10T08:02:00Z"
    }
  ],
  "edges": [
    {
      "from": "file_src_01",
      "to": "run_01",
      "relation": "analyzed_by"
    },
    {
      "from": "run_01",
      "to": "gate_01",
      "relation": "gated_by"
    },
    {
      "from": "gate_01",
      "to": "file_art_01",
      "relation": "generated_by"
    }
  ]
}
```

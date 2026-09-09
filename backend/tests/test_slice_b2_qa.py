import json
import os

import pytest
from app.core.auth import get_current_user
from app.main import app
from app.models.file_record import DocumentChunk, FileRecord, IngestionStatus
from app.models.space import MembershipRole, Space
from app.models.user import User
from app.services.retrieval_service import RetrievalService
from app.services.storage import store
from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_store():
    store.clear()
    yield


# -----------------------------------------------------------------------------
# Gate B2-1: Scoping & Ingestion Readiness Fencing for Document QA
# -----------------------------------------------------------------------------


def test_gate_b2_1_scoping_and_readiness_fencing():
    user = User(uid="u_qa_1", email="qa1@test.com", display_name="QA Tester")
    space = Space(space_id="sp_qa_1", name="QA Space", created_by="u_qa_1")
    store.save_user(user)
    store.create_space(space, creator_uid="u_qa_1")
    store.add_member("sp_qa_1", "u_qa_1", MembershipRole.OWNER)

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        # 1. document_qa without attachment_file_ids -> 422
        res1 = client.post(
            "/v1/chat",
            json={
                "space_id": "sp_qa_1",
                "content": "What is in the script?",
                "intent": "document_qa",
                "attachment_file_ids": [],
            },
        )
        assert res1.status_code == 422, f"Expected 422 for empty attachments, got {res1.status_code}: {res1.text}"
        assert "Document QA mode requires at least one attached or referenced file" in res1.json()["detail"]

        # 2. document_qa with extracting / indexing file -> 422
        f_extracting = FileRecord(
            file_id="f_ext_1",
            space_id="sp_qa_1",
            filename="draft.pdf",
            uploaded_by="u_qa_1",
            upload_status="committed",
            ingestion_status=IngestionStatus.EXTRACTING,
        )
        store.save_file(f_extracting)

        res2 = client.post(
            "/v1/chat",
            json={
                "space_id": "sp_qa_1",
                "content": "What is in the draft?",
                "intent": "document_qa",
                "attachment_file_ids": ["f_ext_1"],
            },
        )
        assert res2.status_code == 422
        assert "still processing" in res2.json()["detail"]

        # 3. document_qa with needs_ocr file -> 422
        f_ocr = FileRecord(
            file_id="f_ocr_1",
            space_id="sp_qa_1",
            filename="scanned.pdf",
            uploaded_by="u_qa_1",
            upload_status="committed",
            ingestion_status=IngestionStatus.NEEDS_OCR,
        )
        store.save_file(f_ocr)

        res3 = client.post(
            "/v1/chat",
            json={
                "space_id": "sp_qa_1",
                "content": "Read scanned doc",
                "intent": "document_qa",
                "attachment_file_ids": ["f_ocr_1"],
            },
        )
        assert res3.status_code == 422
        assert "requires OCR processing" in res3.json()["detail"]

        # 4. Cross-space file attachment -> 404
        space2 = Space(space_id="sp_qa_other", name="Other Space", created_by="u_qa_1")
        store.create_space(space2, creator_uid="u_qa_1")
        f_other = FileRecord(
            file_id="f_other_1",
            space_id="sp_qa_other",
            filename="secret.pdf",
            uploaded_by="u_qa_1",
            upload_status="committed",
            ingestion_status=IngestionStatus.READY,
            active_generation=1,
        )
        store.save_file(f_other)

        res4 = client.post(
            "/v1/chat",
            json={
                "space_id": "sp_qa_1",
                "content": "Can I read other space file?",
                "intent": "document_qa",
                "attachment_file_ids": ["f_other_1"],
            },
        )
        assert res4.status_code in (403, 404)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate B2-2: Balanced Multi-Document Retrieval & CJK Tokenization
# -----------------------------------------------------------------------------


def test_gate_b2_2_balanced_multi_doc_retrieval_and_cjk():
    # 1. CJK Tokenizer verification
    tokens_zh = RetrievalService.tokenize("拍攝日程：台北場景第一幕")
    assert "拍攝" in tokens_zh or "拍" in tokens_zh
    assert "台北" in tokens_zh or "台" in tokens_zh
    assert "場景" in tokens_zh or "場" in tokens_zh

    # 2. Balanced Multi-Document retrieval
    space_id = "sp_b2_retrieval"
    user_id = "u_ret"
    user = User(uid=user_id, email="ret@test.com", display_name="Ret Tester")
    space = Space(space_id=space_id, name="Retrieval Space", created_by=user_id)
    store.save_user(user)
    store.create_space(space, creator_uid=user_id)
    store.add_member(space_id, user_id, MembershipRole.OWNER)

    # File A (Screenplay)
    f_a = FileRecord(
        file_id="f_doc_a",
        space_id=space_id,
        filename="screenplay_a.pdf",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=1,
        max_allocated_generation=1,
    )
    store.save_file(f_a)

    chunks_a = [
        DocumentChunk(
            chunk_id=f"chk_a_{i}",
            file_id="f_doc_a",
            space_id=space_id,
            ingestion_version=1,
            ordinal=i,
            source_locator=f"page:{i+1}",
            raw_text=f"場景 {i+1}：台北市區夜間飛車追逐，男主角張偉與反派對峙。" if i == 0 else f"場景 {i+1}：室內辦公室對話，氣氛嚴肅。",
            normalized_text=f"場景 {i+1}：台北市區夜間飛車追逐，男主角張偉與反派對峙。" if i == 0 else f"場景 {i+1}：室內辦公室對話，氣氛嚴肅。",
            char_start=0,
            char_end=50,
            token_count=10,
            content_hash=f"hash_a_{i}",
            extraction_method="plain_text",
            extractor_version="v1.0",
        )
        for i in range(5)
    ]
    store.save_document_chunks(space_id, "f_doc_a", 1, chunks_a)

    # File B (Schedule / Contract)
    f_b = FileRecord(
        file_id="f_doc_b",
        space_id=space_id,
        filename="contract_schedule_b.pdf",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=1,
        max_allocated_generation=1,
    )
    store.save_file(f_b)

    chunks_b = [
        DocumentChunk(
            chunk_id=f"chk_b_{i}",
            file_id="f_doc_b",
            space_id=space_id,
            ingestion_version=1,
            ordinal=i,
            source_locator=f"page:{i+1}",
            raw_text=f"拍攝日程第 {i+1} 日：台北夜戲封街許可證已獲核准，預算新台幣五十萬元。" if i == 0 else f"一般條款第 {i+1} 條：保密協議與保險規範。",
            normalized_text=f"拍攝日程第 {i+1} 日：台北夜戲封街許可證已獲核准，預算新台幣五十萬元。" if i == 0 else f"一般條款第 {i+1} 條：保密協議與保險規範。",
            char_start=0,
            char_end=50,
            token_count=10,
            content_hash=f"hash_b_{i}",
            extraction_method="plain_text",
            extractor_version="v1.0",
        )
        for i in range(5)
    ]
    store.save_document_chunks(space_id, "f_doc_b", 1, chunks_b)

    # Multi-doc query comparing both files
    query = "請比較台北夜戲的場景安排與封街許可證拍攝日程"
    candidates, ocr_warnings = RetrievalService.retrieve_for_qa(
        space_id=space_id,
        query_text=query,
        file_ids=["f_doc_a", "f_doc_b"],
        top_k_per_doc=2,
        max_total_chunks=4,
    )

    # Assert balanced presence: candidates must contain chunks from BOTH File A and File B
    files_in_candidates = {c.file_id for c in candidates}
    assert "f_doc_a" in files_in_candidates, "Multi-document retrieval must include candidates from File A"
    assert "f_doc_b" in files_in_candidates, "Multi-document retrieval must include candidates from File B"
    assert len(candidates) >= 2, "Must retrieve top-k across both documents"

    # Verify sequential 1-based indexing
    assert [c.index for c in candidates] == list(range(1, len(candidates) + 1))


# -----------------------------------------------------------------------------
# Gate B2-3: End-to-End Multi-Document QA with Authoritative Citations
# -----------------------------------------------------------------------------


def test_gate_b2_3_e2e_document_qa_with_verified_citations():
    space_id = "sp_b2_qa_e2e"
    user_id = "u_qa_e2e"
    user = User(uid=user_id, email="qae2e@test.com", display_name="QA E2E")
    space = Space(space_id=space_id, name="QA E2E Space", created_by=user_id)
    store.save_user(user)
    store.create_space(space, creator_uid=user_id)
    store.add_member(space_id, user_id, MembershipRole.OWNER)

    f1 = FileRecord(
        file_id="f_qa_101",
        space_id=space_id,
        filename="film_treatment.pdf",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=1,
    )
    store.save_file(f1)

    chunk_101 = DocumentChunk(
        chunk_id="chk_101_01",
        file_id="f_qa_101",
        space_id=space_id,
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="主要角色李維是一位資深特務，在第三幕中發現組織內部背叛。",
        normalized_text="主要角色李維是一位資深特務，在第三幕中發現組織內部背叛。",
        char_start=0,
        char_end=28,
        token_count=8,
        content_hash="sha256_chk_101",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    store.save_document_chunks(space_id, "f_qa_101", 1, [chunk_101])

    from unittest.mock import MagicMock, patch

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "主角是李維，在第三幕中發現組織內部背叛",
                    "citation_index": 1,
                    "exact_quote": "主要角色李維是一位資深特務，在第三幕中發現組織內部背叛。"
                }
            ]
        })
    )

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        # Perform Document QA request
        with patch("google.genai.Client", return_value=mock_client), \
             patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaFakeKey123"}):
            res = client.post(
                "/v1/chat",
                json={
                    "space_id": space_id,
                    "content": "請問主要角色是誰？在第幾幕發現背叛？",
                    "intent": "document_qa",
                    "attachment_file_ids": ["f_qa_101"],
                },
            )
            assert res.status_code == 200, f"QA request failed: {res.text}"
            data = res.json()
            assert "agent_message" in data
            agent_msg = data["agent_message"]
            assert agent_msg["role"] == "agent"

            # Verify authoritative citations attached to agent message
            citations = agent_msg.get("citations", [])
            assert len(citations) > 0, "Agent message must contain verified citations"
            cit = citations[0]
            assert cit["file_id"] == "f_qa_101"
            assert cit["filename"] == "film_treatment.pdf"
            assert cit["generation"] == 1
            assert cit["chunk_id"] == "chk_101_01"
            assert "組織內部背叛" in cit["snippet"] or "李維" in cit["snippet"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate B2-4: Immutable Citation Resolution Endpoint & Generation Fencing
# -----------------------------------------------------------------------------


def test_gate_b2_4_immutable_citation_endpoint_and_generation_fencing():
    space_id = "sp_b2_cit_endpoint"
    user_id = "u_cit_test"
    user = User(uid=user_id, email="cittest@test.com", display_name="Cit Tester")
    space = Space(space_id=space_id, name="Citation Test Space", created_by=user_id)
    store.save_user(user)
    store.create_space(space, creator_uid=user_id)
    store.add_member(space_id, user_id, MembershipRole.OWNER)

    f_rec = FileRecord(
        file_id="f_cit_doc",
        space_id=space_id,
        filename="contract_v1.pdf",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=1,
        max_allocated_generation=1,
        deleted_generations=[],
    )
    store.save_file(f_rec)

    chunk_gen1 = DocumentChunk(
        chunk_id="chk_cit_gen1_01",
        file_id="f_cit_doc",
        space_id=space_id,
        ingestion_version=1,
        ordinal=0,
        source_locator="page:2",
        raw_text="本合約有效期限自簽署日起算二年。",
        normalized_text="本合約有效期限自簽署日起算二年。",
        char_start=0,
        char_end=17,
        token_count=5,
        content_hash="sha256_gen1_hash",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    store.save_document_chunks(space_id, "f_cit_doc", 1, [chunk_gen1])

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        # 1. Fetch exact chunk for Generation 1 -> Success
        res1 = client.get(
            f"/v1/spaces/{space_id}/files/f_cit_doc/citations/chk_cit_gen1_01?generation=1"
        )
        assert res1.status_code == 200, f"Expected 200, got: {res1.text}"
        chunk_res = res1.json()
        assert chunk_res["chunk_id"] == "chk_cit_gen1_01"
        assert chunk_res["ingestion_version"] == 1
        assert "有效期限" in chunk_res["raw_text"]

        # 2. File is reindexed to Generation 2 (Generation 1 is tombstoned)
        f_rec.active_generation = 2
        f_rec.deleted_generations = [1]
        store.save_file(f_rec)

        # 3. Requesting tombstoned generation 1 -> 404 SOURCE_GENERATION_UNAVAILABLE
        res2 = client.get(
            f"/v1/spaces/{space_id}/files/f_cit_doc/citations/chk_cit_gen1_01?generation=1"
        )
        assert res2.status_code == 404
        assert res2.json()["detail"] == "SOURCE_GENERATION_UNAVAILABLE"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate B2-5: OCR Gap Notice in Document QA
# -----------------------------------------------------------------------------


def test_gate_b2_5_ocr_gap_notice_in_qa():
    space_id = "sp_b2_ocr_notice"
    user_id = "u_ocr_user"
    user = User(uid=user_id, email="ocruser@test.com", display_name="OCR User")
    space = Space(space_id=space_id, name="OCR Notice Space", created_by=user_id)
    store.save_user(user)
    store.create_space(space, creator_uid=user_id)
    store.add_member(space_id, user_id, MembershipRole.OWNER)

    f_partial = FileRecord(
        file_id="f_ocr_partial_1",
        space_id=space_id,
        filename="partial_scanned.pdf",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=IngestionStatus.READY_PARTIAL,
        ocr_gap_pages=[3, 4],
        active_generation=1,
        max_allocated_generation=1,
    )
    store.save_file(f_partial)

    chunk_p1 = DocumentChunk(
        chunk_id="chk_p1_01",
        file_id="f_ocr_partial_1",
        space_id=space_id,
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="第一頁文字內容：專案啟動會議紀錄。",
        normalized_text="第一頁文字內容：專案啟動會議紀錄。",
        char_start=0,
        char_end=18,
        token_count=5,
        content_hash="sha256_p1_hash",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    store.save_document_chunks(space_id, "f_ocr_partial_1", 1, [chunk_p1])

    candidates, ocr_warnings = RetrievalService.retrieve_for_qa(
        space_id=space_id,
        query_text="專案啟動會議",
        file_ids=["f_ocr_partial_1"],
    )

    assert len(ocr_warnings) > 0, "Must generate OCR gap warning for READY_PARTIAL file"
    assert "第 [3, 4] 頁" in ocr_warnings[0] or "3, 4" in ocr_warnings[0] or "[3, 4]" in ocr_warnings[0]


# -----------------------------------------------------------------------------
# Gate B2-6: No Answer / Out-of-Scope and Hallucinated Citation Pruning
# -----------------------------------------------------------------------------


def test_gate_b2_6_no_answer_and_hallucinated_citation_pruning():
    import os
    from unittest.mock import MagicMock, patch

    from app.agent.brain import AgentBrain
    from app.services.retrieval_service import RetrievedCandidate

    # 1. Zero candidate test -> returns "No related evidence was found", citations is empty
    reply_empty, cit_empty = AgentBrain.answer_document_qa(
        user_text="請問無關問題？",
        space_name="Test Space",
        candidates=[],
    )
    assert "No related evidence was found" in reply_empty
    assert len(cit_empty) == 0

    # 2. Hallucinated citation index pruning test
    chunk_valid = DocumentChunk(
        chunk_id="chk_real_01",
        file_id="f_real_01",
        space_id="sp_real",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="真實文件資訊：主角名字為林晨。",
        normalized_text="真實文件資訊：主角名字為林晨。",
        char_start=0,
        char_end=15,
        token_count=5,
        content_hash="sha256_real_hash",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    cand1 = RetrievedCandidate(
        index=1,
        file_id="f_real_01",
        filename="real_doc.pdf",
        generation=1,
        content_hash="sha256_real_hash",
        chunk=chunk_valid,
        score=1.5,
    )

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "主角是林晨",
                    "citation_index": 1,
                    "exact_quote": "真實文件資訊：主角名字為林晨。"
                },
                {
                    "claim": "另外導演也在此處",
                    "citation_index": 99,
                    "exact_quote": "真實文件資訊：主角名字為林晨。"
                }
            ]
        })
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply, verified_cits = AgentBrain.answer_document_qa(
            user_text="主角是誰？",
            candidates=[cand1],
        )
        # Any invalid citation marker in response triggers strict rejection
        assert "Not enough evidence was found" in reply or "No related evidence was found" in reply
        assert len(verified_cits) == 0

    # When model outputs strictly valid citation [1]
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "主角是林晨",
                    "citation_index": 1,
                    "exact_quote": "真實文件資訊：主角名字為林晨。"
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply2, verified_cits2 = AgentBrain.answer_document_qa(
            user_text="主角是誰？",
            candidates=[cand1],
        )
        assert len(verified_cits2) == 1
        assert verified_cits2[0].index == 1
        assert verified_cits2[0].chunk_id == "chk_real_01"
        assert "[1]" in reply2


# -----------------------------------------------------------------------------
# Gate B2-7: Same-name Different-file Retrieval Isolation
# -----------------------------------------------------------------------------


def test_gate_b2_7_same_name_different_file_isolation():
    space_id = "sp_b2_same_name"
    user_id = "u_sn"
    user = User(uid=user_id, email="sn@test.com", display_name="SN User")
    space = Space(space_id=space_id, name="SN Space", created_by=user_id)
    store.save_user(user)
    store.create_space(space, creator_uid=user_id)
    store.add_member(space_id, user_id, MembershipRole.OWNER)

    # File 1: script.pdf (Treatment version)
    f1 = FileRecord(
        file_id="f_sn_01",
        space_id=space_id,
        filename="script.pdf",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=1,
    )
    store.save_file(f1)
    chunk1 = DocumentChunk(
        chunk_id="chk_sn_01",
        file_id="f_sn_01",
        space_id=space_id,
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="故事發生在西元 2050 年的未來都市。",
        normalized_text="故事發生在西元 2050 年的未來都市。",
        char_start=0,
        char_end=20,
        token_count=5,
        content_hash="hash_sn_01",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    store.save_document_chunks(space_id, "f_sn_01", 1, [chunk1])

    # File 2: script.pdf (Production shooting draft - distinct file_id)
    f2 = FileRecord(
        file_id="f_sn_02",
        space_id=space_id,
        filename="script.pdf",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=1,
    )
    store.save_file(f2)
    chunk2 = DocumentChunk(
        chunk_id="chk_sn_02",
        file_id="f_sn_02",
        space_id=space_id,
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="製作組注意：西元 2050 年場景使用 3 號虛擬攝影棚拍攝。",
        normalized_text="製作組注意：西元 2050 年場景使用 3 號虛擬攝影棚拍攝。",
        char_start=0,
        char_end=30,
        token_count=8,
        content_hash="hash_sn_02",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    store.save_document_chunks(space_id, "f_sn_02", 1, [chunk2])

    cands, _ = RetrievalService.retrieve_for_qa(
        space_id=space_id,
        query_text="2050 年",
        file_ids=["f_sn_01", "f_sn_02"],
    )

    # Verify both distinct files are resolved with distinct file_id and chunk_id
    cand_file_ids = {c.file_id for c in cands}
    assert "f_sn_01" in cand_file_ids
    assert "f_sn_02" in cand_file_ids
    assert len(cands) == 2


# -----------------------------------------------------------------------------
# Gate B2-8: Fail-Closed Behavior when AI_FALLBACK_ALLOWED=False
# -----------------------------------------------------------------------------


def test_gate_b2_8_fail_closed_when_fallback_disallowed():
    import os
    from unittest.mock import MagicMock, patch

    from app.agent.brain import AgentBrain
    from app.services.retrieval_service import RetrievedCandidate

    chunk = DocumentChunk(
        chunk_id="chk_fc_1",
        file_id="f_fc_1",
        space_id="sp_fc",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Sample document text.",
        normalized_text="Sample document text.",
        char_start=0,
        char_end=21,
        token_count=5,
        content_hash="hash_fc_1",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    cand = RetrievedCandidate(
        index=1,
        file_id="f_fc_1",
        filename="doc.pdf",
        generation=1,
        content_hash="hash_fc_1",
        chunk=chunk,
        score=1.0,
    )

    # 1. Missing API Key with AI_FALLBACK_ALLOWED=False -> 503
    with patch("app.core.config.settings.AI_FALLBACK_ALLOWED", False), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
        with pytest.raises(Exception) as exc_info:
            AgentBrain.answer_document_qa(
                user_text="What is this?",
                candidates=[cand],
            )
        assert "503" in str(exc_info.value) or "AI reasoning engine" in str(exc_info.value)

    # 2. Empty LLM output with AI_FALLBACK_ALLOWED=False -> 503
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(text="")
    with patch("app.core.config.settings.AI_FALLBACK_ALLOWED", False), \
         patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        with pytest.raises(Exception) as exc_info2:
            AgentBrain.answer_document_qa(
                user_text="What is this?",
                candidates=[cand],
            )
        assert "503" in str(exc_info2.value) or "AI reasoning engine" in str(exc_info2.value)


# -----------------------------------------------------------------------------
# Gate B2-9: Uncommitted / In-Progress Generation Citation Fencing
# -----------------------------------------------------------------------------


def test_gate_b2_9_uncommitted_generation_citation_fencing():
    space_id = "sp_b2_uncommitted"
    user_id = "u_uncommitted"
    user = User(uid=user_id, email="uncom@test.com", display_name="Uncommitted Tester")
    space = Space(space_id=space_id, name="Uncommitted Space", created_by=user_id)
    store.save_user(user)
    store.create_space(space, creator_uid=user_id)
    store.add_member(space_id, user_id, MembershipRole.OWNER)

    # Active generation is 1, but generation 2 is currently being ingested
    f_rec = FileRecord(
        file_id="f_doc_reindexing",
        space_id=space_id,
        filename="contract.pdf",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=IngestionStatus.EXTRACTING,
        active_generation=1,
        max_allocated_generation=2,
        deleted_generations=[],
    )
    store.save_file(f_rec)

    chunk_gen2 = DocumentChunk(
        chunk_id="chk_gen2_uncommitted",
        file_id="f_doc_reindexing",
        space_id=space_id,
        ingestion_version=2,
        ordinal=0,
        source_locator="page:1",
        raw_text="Uncommitted draft text from gen 2.",
        normalized_text="Uncommitted draft text from gen 2.",
        char_start=0,
        char_end=34,
        token_count=8,
        content_hash="hash_gen2",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    store.save_document_chunks(space_id, "f_doc_reindexing", 2, [chunk_gen2])

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        # Requesting uncommitted generation 2 must return 404 SOURCE_GENERATION_UNAVAILABLE
        res = client.get(
            f"/v1/spaces/{space_id}/files/f_doc_reindexing/citations/chk_gen2_uncommitted?generation=2"
        )
        assert res.status_code == 404
        assert res.json()["detail"] == "SOURCE_GENERATION_UNAVAILABLE"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate B2-10: Ungrounded / Hallucinated Claims Rejection
# -----------------------------------------------------------------------------


def test_gate_b2_10_ungrounded_hallucinated_claims_rejection():
    import os
    from unittest.mock import MagicMock, patch

    from app.agent.brain import AgentBrain
    from app.services.retrieval_service import RetrievedCandidate

    chunk = DocumentChunk(
        chunk_id="chk_gr_1",
        file_id="f_gr_1",
        space_id="sp_gr",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="導演宣佈開鏡日期為十月一日。",
        normalized_text="導演宣佈開鏡日期為十月一日。",
        char_start=0,
        char_end=15,
        token_count=5,
        content_hash="hash_gr_1",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    cand = RetrievedCandidate(
        index=1,
        file_id="f_gr_1",
        filename="memo.pdf",
        generation=1,
        content_hash="hash_gr_1",
        chunk=chunk,
        score=1.0,
    )

    # 1. Model produces totally ungrounded response citing hallucinated [99]
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text="本片預算高達十億美元 [99]。"
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply, citations = AgentBrain.answer_document_qa(
            user_text="預算是多少？",
            candidates=[cand],
        )
        # Must NOT leak hallucinated claim; must reject
        assert "Not enough evidence was found" in reply or "No related evidence was found" in reply
        assert len(citations) == 0

    # 2. Mixed valid + invalid citations -> MUST reject entirely, cannot leak ungrounded claim
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text="導演宣佈開鏡日期為十月一日 [1]。本片預算高達十億美元 [99]。"
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_mixed, citations_mixed = AgentBrain.answer_document_qa(
            user_text="開鏡與預算？",
            candidates=[cand],
        )
        assert "Not enough evidence was found" in reply_mixed or "No related evidence was found" in reply_mixed
        assert len(citations_mixed) == 0

    # 3. Completely citationless factual claim -> MUST reject entirely
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text="導演宣佈開鏡日期為十月一日。"
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_nocit, citations_nocit = AgentBrain.answer_document_qa(
            user_text="何時開鏡？",
            candidates=[cand],
        )
        assert "Not enough evidence was found" in reply_nocit or "No related evidence was found" in reply_nocit
        assert len(citations_nocit) == 0

    # 4. Model produces grounded response citing [1]
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "開鏡日期為十月一日",
                    "citation_index": 1,
                    "exact_quote": "導演宣佈開鏡日期為十月一日。"
                }
            ]
        })
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply2, citations2 = AgentBrain.answer_document_qa(
            user_text="何時開鏡？",
            candidates=[cand],
        )
        assert len(citations2) == 1
        assert citations2[0].index == 1
        assert "十月一日" in citations2[0].snippet
        assert citations2[0].char_start >= 0


# -----------------------------------------------------------------------------
# Gate B2-11: Failed / Uncommitted Historical Generation Fencing
# -----------------------------------------------------------------------------


def test_gate_b2_11_failed_historical_generation_fencing():
    space_id = "sp_b2_failed_hist"
    user_id = "u_hist_tester"
    user = User(uid=user_id, email="hist@test.com", display_name="Hist Tester")
    space = Space(space_id=space_id, name="Hist Space", created_by=user_id)
    store.save_user(user)
    store.create_space(space, creator_uid=user_id)
    store.add_member(space_id, user_id, MembershipRole.OWNER)

    # Active generation is 3, committed generations are [1, 3].
    # Generation 2 was a failed intermediate run that was never published.
    f_rec = FileRecord(
        file_id="f_doc_skipped_gen",
        space_id=space_id,
        filename="production_bible.pdf",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=3,
        max_allocated_generation=3,
        committed_generations=[1, 3],
        deleted_generations=[],
    )
    store.save_file(f_rec)

    # Chunk remnant from failed generation 2
    chunk_gen2_remnant = DocumentChunk(
        chunk_id="chk_gen2_failed_remnant",
        file_id="f_doc_skipped_gen",
        space_id=space_id,
        ingestion_version=2,
        ordinal=0,
        source_locator="page:1",
        raw_text="Remnant chunk from failed generation 2.",
        normalized_text="Remnant chunk from failed generation 2.",
        char_start=0,
        char_end=39,
        token_count=8,
        content_hash="hash_remnant_2",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    store.save_document_chunks(space_id, "f_doc_skipped_gen", 2, [chunk_gen2_remnant])

    # Legitimately committed chunk from generation 1
    chunk_gen1 = DocumentChunk(
        chunk_id="chk_gen1_legit",
        file_id="f_doc_skipped_gen",
        space_id=space_id,
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Legitimate historical text from generation 1.",
        normalized_text="Legitimate historical text from generation 1.",
        char_start=0,
        char_end=45,
        token_count=8,
        content_hash="hash_legit_1",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    store.save_document_chunks(space_id, "f_doc_skipped_gen", 1, [chunk_gen1])

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        # 1. Querying uncommitted/failed generation 2 (even though 2 < active_gen 3) MUST return 404
        res_gen2 = client.get(
            f"/v1/spaces/{space_id}/files/f_doc_skipped_gen/citations/chk_gen2_failed_remnant?generation=2"
        )
        assert res_gen2.status_code == 404
        assert res_gen2.json()["detail"] == "SOURCE_GENERATION_UNAVAILABLE"

        # 2. Querying committed generation 1 succeeds
        res_gen1 = client.get(
            f"/v1/spaces/{space_id}/files/f_doc_skipped_gen/citations/chk_gen1_legit?generation=1"
        )
        assert res_gen1.status_code == 200
        assert res_gen1.json()["chunk_id"] == "chk_gen1_legit"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate B2-12: Contradictory Factual Claim Rejection Under Valid Citation Index
# -----------------------------------------------------------------------------


def test_gate_b2_12_contradictory_factual_claim_rejection():
    import os
    from unittest.mock import MagicMock, patch

    from app.agent.brain import AgentBrain
    from app.services.retrieval_service import RetrievedCandidate

    chunk = DocumentChunk(
        chunk_id="chk_budget_1",
        file_id="f_budget_1",
        space_id="sp_budget",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Approved production budget is 100 dollars.",
        normalized_text="Approved production budget is 100 dollars.",
        char_start=0,
        char_end=42,
        token_count=6,
        content_hash="hash_budget_1",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    cand = RetrievedCandidate(
        index=1,
        file_id="f_budget_1",
        filename="budget_summary.pdf",
        generation=1,
        content_hash="hash_budget_1",
        chunk=chunk,
        score=1.0,
    )

    # 1. Model outputs contradictory figure "999 dollars" while citing valid index [1]
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "Approved budget is 999 dollars",
                    "citation_index": 1,
                    "exact_quote": "Approved production budget is 100 dollars."
                }
            ]
        })
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply, citations = AgentBrain.answer_document_qa(
            user_text="What is the budget?",
            candidates=[cand],
        )
        assert "Not enough evidence was found" in reply or "No related evidence was found" in reply
        assert len(citations) == 0

    # 2. Case: Doc has 1000 dollars, model claims 100 dollars (100 as substring of 1000)
    chunk_1000 = DocumentChunk(
        chunk_id="chk_b_1000",
        file_id="f_b_1000",
        space_id="sp_budget",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="預算 1000 dollars",
        normalized_text="預算 1000 dollars",
        char_start=0,
        char_end=17,
        token_count=3,
        content_hash="hash_1000",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    cand_1000 = RetrievedCandidate(
        index=1,
        file_id="f_b_1000",
        filename="b1000.pdf",
        generation=1,
        content_hash="hash_1000",
        chunk=chunk_1000,
        score=1.0,
    )
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "預算 100 dollars",
                    "citation_index": 1,
                    "exact_quote": "預算 1000 dollars"
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_sub, cits_sub = AgentBrain.answer_document_qa(
            user_text="預算多少？",
            candidates=[cand_1000],
        )
        assert "Not enough evidence was found" in reply_sub or "No related evidence was found" in reply_sub
        assert len(cits_sub) == 0

    # 3. Case: Entity mismatch (Director is Alice vs Director is Bob)
    chunk_dir = DocumentChunk(
        chunk_id="chk_dir_alice",
        file_id="f_dir_alice",
        space_id="sp_budget",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Director is Alice.",
        normalized_text="Director is Alice.",
        char_start=0,
        char_end=18,
        token_count=3,
        content_hash="hash_dir",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    cand_dir = RetrievedCandidate(
        index=1,
        file_id="f_dir_alice",
        filename="director.pdf",
        generation=1,
        content_hash="hash_dir",
        chunk=chunk_dir,
        score=1.0,
    )
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "Director is Bob",
                    "citation_index": 1,
                    "exact_quote": "Director is Alice."
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_dir, cits_dir = AgentBrain.answer_document_qa(
            user_text="Who is director?",
            candidates=[cand_dir],
        )
        assert "Not enough evidence was found" in reply_dir or "No related evidence was found" in reply_dir
        assert len(cits_dir) == 0

    # 4. Case: Polarity contradiction (Refunds are not allowed vs Refunds are allowed)
    chunk_refund = DocumentChunk(
        chunk_id="chk_refund",
        file_id="f_refund",
        space_id="sp_budget",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Refunds are not allowed under any circumstances.",
        normalized_text="Refunds are not allowed under any circumstances.",
        char_start=0,
        char_end=48,
        token_count=7,
        content_hash="hash_refund",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    cand_refund = RetrievedCandidate(
        index=1,
        file_id="f_refund",
        filename="refund_policy.pdf",
        generation=1,
        content_hash="hash_refund",
        chunk=chunk_refund,
        score=1.0,
    )
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "Refunds are allowed",
                    "citation_index": 1,
                    "exact_quote": "Refunds are not allowed under any circumstances."
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_pol, cits_pol = AgentBrain.answer_document_qa(
            user_text="Can I get refund?",
            candidates=[cand_refund],
        )
        assert "Not enough evidence was found" in reply_pol or "No related evidence was found" in reply_pol
        assert len(cits_pol) == 0

    # 5. Case: Subject-Object Role Reversal (Alice pays Bob vs Bob pays Alice)
    chunk_pay = DocumentChunk(
        chunk_id="chk_pay",
        file_id="f_pay",
        space_id="sp_budget",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Alice pays Bob.",
        normalized_text="Alice pays Bob.",
        char_start=0,
        char_end=15,
        token_count=3,
        content_hash="hash_pay",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    cand_pay = RetrievedCandidate(
        index=1,
        file_id="f_pay",
        filename="contract.pdf",
        generation=1,
        content_hash="hash_pay",
        chunk=chunk_pay,
        score=1.0,
    )
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "Bob pays Alice",
                    "citation_index": 1,
                    "exact_quote": "Alice pays Bob."
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_rev, cits_rev = AgentBrain.answer_document_qa(
            user_text="Who pays whom?",
            candidates=[cand_pay],
        )
        assert "Not enough evidence was found" in reply_rev or "No related evidence was found" in reply_rev
        assert len(cits_rev) == 0

    # 6. Case: Multi-condition document accurately verifying specific valid claim
    chunk_multi = DocumentChunk(
        chunk_id="chk_multi_cond",
        file_id="f_multi_cond",
        space_id="sp_budget",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Refunds are not allowed for tickets. Refunds are allowed for deposits.",
        normalized_text="Refunds are not allowed for tickets. Refunds are allowed for deposits.",
        char_start=0,
        char_end=72,
        token_count=12,
        content_hash="hash_multi",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    cand_multi = RetrievedCandidate(
        index=1,
        file_id="f_multi_cond",
        filename="policy.pdf",
        generation=1,
        content_hash="hash_multi",
        chunk=chunk_multi,
        score=1.0,
    )
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "Refunds are allowed for deposits",
                    "citation_index": 1,
                    "exact_quote": "Refunds are allowed for deposits."
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_multi, cits_multi = AgentBrain.answer_document_qa(
            user_text="Are deposit refunds allowed?",
            candidates=[cand_multi],
        )
        assert len(cits_multi) == 1
        assert cits_multi[0].index == 1
        assert "Refunds are allowed for deposits" in cits_multi[0].snippet

    # 7. Case: Explicit JSON with invalid exact_quote is strictly rejected without substitution
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "Approved budget is 100 dollars",
                    "citation_index": 1,
                    "exact_quote": "THIS QUOTE DOES NOT EXIST"
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_no_sub, cits_no_sub = AgentBrain.answer_document_qa(
            user_text="What is the budget?",
            candidates=[cand],
        )
        assert "Not enough evidence was found" in reply_no_sub or "No related evidence was found" in reply_no_sub
        assert len(cits_no_sub) == 0

    # 8. Case: Claim number equal to citation_index is NOT discarded
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "Approved budget is 1 dollars",
                    "citation_index": 1,
                    "exact_quote": "Approved production budget is 100 dollars."
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_num1, cits_num1 = AgentBrain.answer_document_qa(
            user_text="What is the budget?",
            candidates=[cand],
        )
        assert "Not enough evidence was found" in reply_num1 or "No related evidence was found" in reply_num1
        assert len(cits_num1) == 0

    # 9. Case: Multiple claims from same chunk receive distinct sequential answer citation indices
    chunk_dual = DocumentChunk(
        chunk_id="chk_dual_claim",
        file_id="f_dual",
        space_id="sp_budget",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Budget is 100 dollars. Deposit is 20 dollars.",
        normalized_text="Budget is 100 dollars. Deposit is 20 dollars.",
        char_start=0,
        char_end=45,
        token_count=8,
        content_hash="hash_dual",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    cand_dual = RetrievedCandidate(
        index=1,
        file_id="f_dual",
        filename="terms.pdf",
        generation=1,
        content_hash="hash_dual",
        chunk=chunk_dual,
        score=1.0,
    )
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "Budget is 100 dollars",
                    "citation_index": 1,
                    "exact_quote": "Budget is 100 dollars."
                },
                {
                    "claim": "Deposit is 20 dollars",
                    "citation_index": 1,
                    "exact_quote": "Deposit is 20 dollars."
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_dual, cits_dual = AgentBrain.answer_document_qa(
            user_text="What are the terms?",
            candidates=[cand_dual],
        )
        assert len(cits_dual) == 2
        assert cits_dual[0].index == 1
        assert cits_dual[0].snippet == "Budget is 100 dollars."
        assert cits_dual[1].index == 2
        assert cits_dual[1].snippet == "Deposit is 20 dollars."
        assert "[1]" in reply_dual
        assert "[2]" in reply_dual

    # 10. Anti-Pattern 1: Non-JSON plain text bracket notation must be rejected
    mock_client.models.generate_content.return_value = MagicMock(
        text="Approved budget is 100 dollars [1]."
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_non_json, cits_non_json = AgentBrain.answer_document_qa(
            user_text="What is the budget?",
            candidates=[cand],
        )
        assert "Not enough evidence was found" in reply_non_json or "No related evidence was found" in reply_non_json
        assert len(cits_non_json) == 0

    # 11. Anti-Pattern 2: Float citation index (e.g. 1.9) must be rejected
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "Approved budget is 100 dollars",
                    "citation_index": 1.9,
                    "exact_quote": "Approved production budget is 100 dollars."
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_float, cits_float = AgentBrain.answer_document_qa(
            user_text="What is the budget?",
            candidates=[cand],
        )
        assert "Not enough evidence was found" in reply_float or "No related evidence was found" in reply_float
        assert len(cits_float) == 0

    # 12. Anti-Pattern 3: Claims with 1 valid item and 1 missing/malformed item must reject entire response
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "Approved budget is 100 dollars",
                    "citation_index": 1,
                    "exact_quote": "Approved production budget is 100 dollars."
                },
                {
                    "claim": "Missing exact quote",
                    "citation_index": 1
                    # missing exact_quote!
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_malformed, cits_malformed = AgentBrain.answer_document_qa(
            user_text="What is the budget?",
            candidates=[cand],
        )
        assert "Not enough evidence was found" in reply_malformed or "No related evidence was found" in reply_malformed
        assert len(cits_malformed) == 0

    # 13. Positive Case: Valid structured JSON response
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [
                {
                    "claim": "Production has an approved budget of 100 dollars",
                    "citation_index": 1,
                    "exact_quote": "Approved production budget is 100 dollars."
                }
            ]
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_pos, cits_pos = AgentBrain.answer_document_qa(
            user_text="What is the budget?",
            candidates=[cand],
        )
        assert len(cits_pos) == 1
        assert cits_pos[0].index == 1
        assert "100 dollars" in cits_pos[0].snippet
        assert "[1]" in reply_pos

    # 14. Anti-Pattern 4: Smuggled factual disclaimer with empty claims must NEVER be returned to user
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": [],
            "disclaimer": "依據文件，預算為999元，已經核准。"
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_disc, cits_disc = AgentBrain.answer_document_qa(
            user_text="What is the budget?",
            candidates=[cand],
        )
        assert reply_disc == "Not enough evidence was found in the specified document to answer this question."
        assert "999" not in reply_disc
        assert len(cits_disc) == 0

    # 15. Positive Case: Normal empty claims response
    mock_client.models.generate_content.return_value = MagicMock(
        text=json.dumps({
            "claims": []
        })
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply_empty, cits_empty = AgentBrain.answer_document_qa(
            user_text="What is the budget?",
            candidates=[cand],
        )
        assert reply_empty == "Not enough evidence was found in the specified document to answer this question."
        assert len(cits_empty) == 0


def test_overview_whats_query_uses_discussion_not_strict_qa():
    from unittest.mock import patch

    from app.api.chat_routes import is_macro_analytical_query

    assert is_macro_analytical_query("What's this treatment talking about?")
    assert is_macro_analytical_query("whats this about?")
    assert is_macro_analytical_query("Tell me about this document")
    assert not is_macro_analytical_query("What is the budget?")

    space_id = "sp_overview_qa"
    user_id = "u_overview_qa"
    user = User(uid=user_id, email="overview@test.com", display_name="Overview")
    space = Space(space_id=space_id, name="Overview Space", created_by=user_id)
    store.save_user(user)
    store.create_space(space, creator_uid=user_id)
    store.add_member(space_id, user_id, MembershipRole.OWNER)
    store.save_file(
        FileRecord(
            file_id="f_treat_01",
            space_id=space_id,
            filename="project_nebula_runner_treatment_10pages.pdf",
            uploaded_by=user_id,
            upload_status="committed",
            ingestion_status=IngestionStatus.READY,
            active_generation=1,
        )
    )
    store.save_document_chunks(
        space_id,
        "f_treat_01",
        1,
        [
            DocumentChunk(
                chunk_id="chk_treat_01",
                file_id="f_treat_01",
                space_id=space_id,
                ingestion_version=1,
                ordinal=0,
                source_locator="page:1",
                raw_text="Nebula Runner follows a courier racing through Neon Harbor.",
                normalized_text="Nebula Runner follows a courier racing through Neon Harbor.",
                char_start=0,
                char_end=58,
                token_count=10,
                content_hash="sha256_treat",
                extraction_method="plain_text",
                extractor_version="v1.0",
            )
        ],
    )

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with patch(
            "app.api.chat_routes.AgentBrain.discuss_with_user",
            return_value="The treatment follows a courier racing through Neon Harbor.",
        ) as discuss, patch("app.api.chat_routes.AgentBrain.answer_document_qa") as qa:
            res = client.post(
                "/v1/chat",
                json={
                    "space_id": space_id,
                    "content": "What's this treatment talking about?",
                    "intent": "document_qa",
                    "context_file_ids": ["f_treat_01"],
                },
            )
        assert res.status_code == 200, res.text
        qa.assert_not_called()
        discuss.assert_called_once()
        assert "Neon Harbor" in res.json()["agent_message"]["content"]
        assert "Not enough evidence" not in res.json()["agent_message"]["content"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate B2-13: TXT Extraction, Multi-Doc Balancing & Backend QA Integration Test
# -----------------------------------------------------------------------------


def test_gate_b2_13_ingestion_extraction_and_backend_qa_integration():
    """
    Integration test verifying:
    1. Raw byte ingestion via ProDocuXFacade into normalized chunks & locators.
    2. Multi-document balanced candidate retrieval for cross-document comparison.
    3. Multi-claim grounding and per-claim answer index generation for follow-up queries.
    4. Immutable citation endpoint resolution.
    """
    space_id = "sp_b2_uact"
    user_id = "u_uact_tester"
    user = User(uid=user_id, email="uact@test.com", display_name="UACT User")
    space = Space(space_id=space_id, name="UACT Space", created_by=user_id)
    store.save_user(user)
    store.create_space(space, creator_uid=user_id)
    store.add_member(space_id, user_id, MembershipRole.OWNER)

    from app.integrations.prodocux_facade import ProDocuXFacade

    # 1. Ingest real document 1 bytes: treatment_v1.txt
    v1_bytes = "企劃版本一：總預算為 100 萬美元，拍攝期為 30 天。".encode("utf-8")
    status1, chunks1, _, _, method1, _ = ProDocuXFacade.ingest_document(
        v1_bytes, "treatment_v1.txt", "f_uact_v1", space_id, generation=1
    )
    assert status1 == IngestionStatus.READY
    f1 = FileRecord(
        file_id="f_uact_v1",
        space_id=space_id,
        filename="treatment_v1.txt",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=status1,
        active_generation=1,
        max_allocated_generation=1,
    )
    store.save_file(f1)
    store.save_document_chunks(space_id, "f_uact_v1", 1, chunks1)

    # 2. Ingest real document 2 bytes: treatment_v2.txt
    v2_bytes = "企劃版本二：總預算調整為 150 萬美元。主要角色為林晨。押金定為 20 萬美元。".encode("utf-8")
    status2, chunks2, _, _, method2, _ = ProDocuXFacade.ingest_document(
        v2_bytes, "treatment_v2.txt", "f_uact_v2", space_id, generation=1
    )
    assert status2 == IngestionStatus.READY
    f2 = FileRecord(
        file_id="f_uact_v2",
        space_id=space_id,
        filename="treatment_v2.txt",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=status2,
        active_generation=1,
        max_allocated_generation=1,
    )
    store.save_file(f2)
    store.save_document_chunks(space_id, "f_uact_v2", 1, chunks2)

    from unittest.mock import MagicMock, patch

    mock_client = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: user

    try:
        # Step A: Two-document comparison query
        mock_client.models.generate_content.return_value = MagicMock(
            text=json.dumps({
                "claims": [
                    {
                        "claim": "版本一的總預算為 100 萬美元",
                        "citation_index": 1,
                        "exact_quote": "企劃版本一：總預算為 100 萬美元，拍攝期為 30 天。"
                    },
                    {
                        "claim": "版本二的總預算調整為 150 萬美元",
                        "citation_index": 2,
                        "exact_quote": "企劃版本二：總預算調整為 150 萬美元。"
                    }
                ]
            })
        )

        with patch("google.genai.Client", return_value=mock_client), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
            res1 = client.post(
                "/v1/chat",
                json={
                    "space_id": space_id,
                    "content": "請問 v1 與 v2 的預算分別是多少？",
                    "intent": "document_qa",
                    "attachment_file_ids": ["f_uact_v1", "f_uact_v2"],
                },
            )
            assert res1.status_code == 200
            data1 = res1.json()
            agent_msg1 = data1["agent_message"]
            cits1 = agent_msg1.get("citations", [])
            assert len(cits1) == 2
            assert cits1[0]["filename"] == "treatment_v1.txt"
            assert cits1[0]["index"] == 1
            assert cits1[1]["filename"] == "treatment_v2.txt"
            assert cits1[1]["index"] == 2
            assert "[1]" in agent_msg1["content"]
            assert "[2]" in agent_msg1["content"]

        # Step B: Follow-up question with dual claims on single document
        mock_client.models.generate_content.return_value = MagicMock(
            text=json.dumps({
                "claims": [
                    {
                        "claim": "主要角色為林晨",
                        "citation_index": 1,
                        "exact_quote": "主要角色為林晨。"
                    },
                    {
                        "claim": "押金定為 20 萬美元",
                        "citation_index": 1,
                        "exact_quote": "押金定為 20 萬美元。"
                    }
                ]
            })
        )

        with patch("google.genai.Client", return_value=mock_client), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
            res2 = client.post(
                "/v1/chat",
                json={
                    "space_id": space_id,
                    "content": "那麼 v2 的主角是誰？押金多少？",
                    "intent": "document_qa",
                    "attachment_file_ids": ["f_uact_v2"],
                },
            )
            assert res2.status_code == 200
            data2 = res2.json()
            agent_msg2 = data2["agent_message"]
            cits2 = agent_msg2.get("citations", [])
            assert len(cits2) == 2
            assert cits2[0]["index"] == 1
            assert "林晨" in cits2[0]["snippet"]
            assert cits2[1]["index"] == 2
            assert "20 萬美元" in cits2[1]["snippet"]
            # Verify distinct character ranges for highlighting
            assert cits2[0]["char_start"] != cits2[1]["char_start"]

        # Step C: Resolution endpoint verification for both citations
        for cit_item in cits2:
            chunk_id = cit_item["chunk_id"]
            file_id = cit_item["file_id"]
            gen = cit_item["generation"]
            res_cit = client.get(f"/v1/spaces/{space_id}/files/{file_id}/citations/{chunk_id}?generation={gen}")
            assert res_cit.status_code == 200
            cit_resolved = res_cit.json()
            assert cit_resolved["chunk_id"] == chunk_id
            assert cit_resolved["file_id"] == "f_uact_v2"
            assert "raw_text" in cit_resolved
            assert len(cit_resolved["raw_text"]) > 0

    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate B2-14: Opt-In Live Gemini Reasoning & Citation Smoke Acceptance
# -----------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GEMINI_TESTS") != "1" or not os.environ.get("GEMINI_API_KEY"),
    reason="Live Gemini acceptance test requires RUN_LIVE_GEMINI_TESTS=1 and valid GEMINI_API_KEY",
)
def test_gate_b2_14_live_gemini_acceptance():
    from app.agent.brain import AgentBrain
    from app.core.config import settings
    from app.integrations.prodocux_facade import ProDocuXFacade
    from app.services.retrieval_service import RetrievalService

    space_id = "sp_live_test"
    user_id = "u_live_tester"

    user = User(uid=user_id, email="live@test.com", display_name="Live Tester")
    space = Space(space_id=space_id, name="Live Space", created_by=user_id)
    store.save_user(user)
    store.create_space(space, creator_uid=user_id)
    store.add_member(space_id, user_id, MembershipRole.OWNER)

    # 1. Save committed FileRecord first before saving chunks
    f1 = FileRecord(
        file_id="f_live_v1",
        space_id=space_id,
        filename="treatment_v1.txt",
        uploaded_by=user_id,
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=1,
        max_allocated_generation=1,
    )
    store.save_file(f1)

    # 2. Ingest document and save chunks
    v1_bytes = "企劃版本一：總預算為 100 萬美元，拍攝期為 30 天。".encode("utf-8")
    status1, chunks1, _, _, _, _ = ProDocuXFacade.ingest_document(
        v1_bytes, "treatment_v1.txt", "f_live_v1", space_id, generation=1
    )
    assert status1 == IngestionStatus.READY
    saved_ok = store.save_document_chunks(space_id, "f_live_v1", 1, chunks1)
    assert saved_ok is True
    stored_chunks = store.get_document_chunks(space_id, "f_live_v1", generation=1)
    assert len(stored_chunks) == len(chunks1)
    assert [c.content_hash for c in stored_chunks] == [c.content_hash for c in chunks1]

    # 3. Perform real retrieval & live un-mocked inference (strictly no AI fallback)
    orig_fallback = settings.AI_FALLBACK_ALLOWED
    settings.AI_FALLBACK_ALLOWED = False
    try:
        candidates, warnings = RetrievalService.retrieve_for_qa(
            space_id=space_id,
            query_text="版本一的預算與拍攝期？",
            file_ids=["f_live_v1"],
        )
        assert len(candidates) > 0, "Retrieval must return matching candidates for live inference"

        reply, citations = AgentBrain.answer_document_qa(
            user_text="版本一的預算與拍攝期？",
            candidates=candidates,
        )

        assert len(citations) >= 1
        assert "100" in reply
        assert "[1]" in reply
    finally:
        settings.AI_FALLBACK_ALLOWED = orig_fallback


def test_document_qa_accepts_normalized_quote_when_raw_text_differs():
    import json
    from unittest.mock import MagicMock, patch

    from app.agent.brain import AgentBrain
    from app.services.retrieval_service import RetrievedCandidate

    chunk = DocumentChunk(
        chunk_id="chk_norm_01",
        file_id="f_norm_01",
        space_id="sp_norm",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Act II\n\nScene Breakdown for Project Nebula Runner.",
        normalized_text="Act II Scene Breakdown for Project Nebula Runner.",
        char_start=0,
        char_end=48,
        token_count=8,
        content_hash="hash_norm",
        extraction_method="prodocux_pdf",
        extractor_version="v1.0",
    )
    cand = RetrievedCandidate(
        index=1,
        file_id="f_norm_01",
        filename="act_ii_scene_breakdown.pdf",
        generation=1,
        content_hash="hash_norm",
        chunk=chunk,
        score=1.0,
    )
    payload = json.dumps({
        "claims": [{
            "claim": "The document is an Act II scene breakdown.",
            "citation_index": 1,
            "exact_quote": "Act II Scene Breakdown for Project Nebula Runner.",
        }]
    })
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(text=payload)
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        reply, citations = AgentBrain.answer_document_qa(
            user_text="What is this about?",
            candidates=[cand],
        )
    assert citations
    assert "Act II" in reply





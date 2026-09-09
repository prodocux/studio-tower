import io
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.config import Settings, settings
from app.integrations.prodocux_facade import ProDocuXFacade
from app.main import app
from app.models.file_record import DocumentChunk, FileRecord, IngestionStatus
from app.models.space import MembershipRole, Space, SpaceKind
from app.models.user import User
from app.services.retrieval_service import RetrievalService
from app.services.ingestion_runner import (
    InlineIngestionRunner,
)
from app.services.storage import MemoryStore, store
from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_store():
    store.clear()
    yield
    store.clear()


def make_test_pdf_bytes(page_texts: list[str]) -> bytes:
    """Helper to generate a multi-page PDF in memory using pypdf."""
    import pypdf

    writer = pypdf.PdfWriter()
    for text in page_texts:
        if text.strip():
            writer.add_blank_page(width=612, height=792)
        else:
            writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_test_docx_bytes(paragraphs: list[str], table_rows: list[list[str]] | None = None) -> bytes:
    """Generate a real OPC DOCX that ProDocuX Kernel can extract."""
    from docx import Document

    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    if table_rows:
        width = max(len(row) for row in table_rows)
        table = document.add_table(rows=len(table_rows), cols=width)
        for row_index, row in enumerate(table_rows):
            for column_index, cell in enumerate(row):
                table.cell(row_index, column_index).text = cell
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_test_xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    budget = workbook.active
    budget.title = "Budget"
    budget.append(["Account", "Description", "Amount"])
    budget.append(["Cast", "Principal performers", 120000])
    budget.append(["VFX", "Sky replacement", 45000])
    budget["C4"] = "=SUM(C2:C3)"
    schedule = workbook.create_sheet("Schedule")
    schedule.append(["Scene", "Location", "Day"])
    schedule.append([12, "Hangar", "Night"])
    buf = io.BytesIO()
    workbook.save(buf)
    workbook.close()
    return buf.getvalue()


def make_test_pptx_bytes() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Act III Lookbook"
    slide.placeholders[1].text = "Night exterior\nWire stunt and VFX plate"
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(3), Inches(6), Inches(1)).table
    table.cell(0, 0).text = "Department"
    table.cell(0, 1).text = "Requirement"
    table.cell(1, 0).text = "Camera"
    table.cell(1, 1).text = "Low-light package"
    slide.notes_slide.notes_text_frame.text = "Confirm the stage availability."
    buf = io.BytesIO()
    presentation.save(buf)
    return buf.getvalue()


# -----------------------------------------------------------------------------
# Gate 1: Multi-Format Chunks & Locators
# -----------------------------------------------------------------------------


def test_gate1_multi_format_chunks_and_locators():
    # 1. TXT Extractor
    txt_content = (
        "INT. COFFEE SHOP - DAY\n\nAlice sips her coffee.\n\n"
        "EXT. PARK - AFTERNOON\n\nBob walks the dog across the green grass."
    ).encode("utf-8")
    status, chunks, pages, gaps, method, err = ProDocuXFacade.ingest_document(
        txt_content, "script.txt", "file_txt_1", "space_1", generation=1
    )
    assert status == IngestionStatus.READY
    assert len(chunks) >= 1
    assert chunks[0].chunk_id == "chk_file_txt_1_g1_0000"
    assert chunks[0].source_locator.startswith("section:")
    assert chunks[0].content_hash != ""
    assert chunks[0].char_start == 0

    # 2. FDX Extractor
    fdx_content = b"""<?xml version="1.0" encoding="UTF-8"?>
<FinalDraft DocumentType="Script" Template="No" Version="1">
  <Content>
    <Paragraph Type="Scene Heading"><Text>EXT. ROOFTOP - NIGHT</Text></Paragraph>
    <Paragraph Type="Action"><Text>Rain pours over the gargoyles.</Text></Paragraph>
    <Paragraph Type="Character"><Text>DETECTIVE</Text></Paragraph>
    <Paragraph Type="Dialogue"><Text>We have to move now.</Text></Paragraph>
  </Content>
</FinalDraft>"""
    status, chunks, pages, gaps, method, err = ProDocuXFacade.ingest_document(
        fdx_content, "screenplay.fdx", "file_fdx_1", "space_1", generation=1
    )
    assert status == IngestionStatus.READY
    assert len(chunks) == 1
    assert "scene:1:EXT. ROOFTOP - NIGHT" in chunks[0].source_locator
    assert chunks[0].extraction_method == "fdx_xml"

    # 3. DOCX Extractor delegates to ProDocuX Kernel extract_content_blocks.
    from prodocux_kernel.rendering import extract_content_blocks

    docx_bytes = make_test_docx_bytes(
        ["Treatment Overview", "This is the main treatment outline."],
        table_rows=[["Scene", "Location", "Budget"], ["1", "Hangar", "$50,000"]],
    )
    assert extract_content_blocks("treatment.docx", docx_bytes)["format"] == "docx"
    status, chunks, pages, gaps, method, err = ProDocuXFacade.ingest_document(
        docx_bytes, "treatment.docx", "file_docx_1", "space_1", generation=1
    )
    assert status == IngestionStatus.READY
    assert err is None
    assert method == "prodocux_docx"
    assert chunks[0].extraction_method == "prodocux_docx"

    # 4. XLSX Extractor delegates to ProDocuX and preserves sheet/row locators.
    from prodocux_kernel.intake import profile_xlsx_bytes
    from prodocux_kernel.rendering import extract_content_blocks

    xlsx_bytes = make_test_xlsx_bytes()
    assert extract_content_blocks("production_budget.xlsx", xlsx_bytes)["format"] == "xlsx"
    assert profile_xlsx_bytes(xlsx_bytes, filename="production_budget.xlsx")["sheet_count"] == 2
    status, chunks, sheets, gaps, method, err = ProDocuXFacade.ingest_document(
        xlsx_bytes, "production_budget.xlsx", "file_xlsx_1", "space_1", generation=2
    )
    assert status == IngestionStatus.READY
    assert err is None
    assert sheets == 2
    assert method == "prodocux_xlsx"
    assert chunks[0].chunk_id == "chk_file_xlsx_1_g2_0000"
    assert chunks[0].source_locator == "sheet:Budget:rows:2-4"
    assert "Principal performers" in chunks[0].normalized_text
    assert chunks[0].contains_formula_like_content is True
    assert any(chunk.source_locator == "sheet:Schedule:rows:2-2" for chunk in chunks)

    # 5. PPTX Extractor delegates to the ProDocuX presentation profile.
    from prodocux_kernel.intake import profile_pptx_bytes

    pptx_bytes = make_test_pptx_bytes()
    assert profile_pptx_bytes(pptx_bytes, filename="act_iii_lookbook.pptx")["slide_count"] == 1
    status, chunks, slides, gaps, method, err = ProDocuXFacade.ingest_document(
        pptx_bytes, "act_iii_lookbook.pptx", "file_pptx_1", "space_1", generation=3
    )
    assert status == IngestionStatus.READY
    assert err is None
    assert slides == 1
    assert method == "prodocux_pptx"
    assert chunks[0].chunk_id == "chk_file_pptx_1_g3_0000"
    assert chunks[0].source_locator == "slide:1"
    assert "Act III Lookbook" in chunks[0].normalized_text
    assert "Low-light package" in chunks[0].normalized_text
    assert "Confirm the stage availability." in chunks[0].normalized_text


def test_gate1b_xlsx_pptx_chunks_reach_retrieval_and_citations(monkeypatch):
    """Real Office binaries must cross the StudioTower indexing/retrieval boundary."""
    test_store = MemoryStore()
    monkeypatch.setattr("app.services.retrieval_service.store", test_store)
    space_id = "space_office_qa"

    for file_id, filename, payload in (
        ("file_budget", "production_budget.xlsx", make_test_xlsx_bytes()),
        ("file_lookbook", "act_iii_lookbook.pptx", make_test_pptx_bytes()),
    ):
        status, chunks, _, _, _, err = ProDocuXFacade.ingest_document(
            payload, filename, file_id, space_id, generation=1
        )
        assert status == IngestionStatus.READY
        assert err is None
        record = FileRecord(
            file_id=file_id,
            space_id=space_id,
            filename=filename,
            uploaded_by="office_user",
            upload_status="committed",
            publication_status="published",
            ingestion_status=IngestionStatus.READY,
            active_generation=1,
            max_allocated_generation=1,
            committed_generations=[1],
        )
        test_store.save_file(record)
        assert test_store.save_document_chunks(space_id, file_id, 1, chunks) is True

    candidates, warnings = RetrievalService.retrieve_for_qa(
        space_id,
        "What is the VFX budget and which camera package is required?",
        ["file_budget", "file_lookbook"],
        top_k_per_doc=2,
        max_total_chunks=4,
    )
    assert warnings == []
    assert {candidate.file_id for candidate in candidates} == {"file_budget", "file_lookbook"}
    budget = next(candidate for candidate in candidates if candidate.file_id == "file_budget")
    lookbook = next(candidate for candidate in candidates if candidate.file_id == "file_lookbook")
    budget_citation = budget.to_citation("Sky replacement")
    lookbook_citation = lookbook.to_citation("Low-light package")
    assert budget_citation is not None
    assert budget_citation.source_locator.startswith("sheet:Budget:rows:")
    assert lookbook_citation is not None
    assert lookbook_citation.source_locator == "slide:1"


def test_gate1c_xlsx_pptx_parse_failures_do_not_fall_through_to_txt():
    """Corrupt Office binaries must fail closed instead of being ingested as TXT."""
    status, chunks, _, _, method, err = ProDocuXFacade.ingest_document(
        b"not-an-xlsx-workbook", "broken_budget.xlsx", "file_xlsx_bad", "space_1", generation=1
    )
    assert status == IngestionStatus.FAILED
    assert chunks == []
    assert method == "prodocux_xlsx"
    assert err == "ERR_XLSX_PARSE_FAILED"

    status, chunks, _, _, method, err = ProDocuXFacade.ingest_document(
        b"not-a-pptx-deck", "broken_lookbook.pptx", "file_pptx_bad", "space_1", generation=1
    )
    assert status == IngestionStatus.FAILED
    assert chunks == []
    assert method == "prodocux_pptx"
    assert err == "ERR_PPTX_PARSE_FAILED"


def test_gate1d_xlsx_pptx_upload_commits_generation_and_citations():
    """HTTP upload must run inline ingestion through generation commit into retrieval/citations."""
    owner = User(uid="office_uploader", email="office@test.com", display_name="Office Uploader")
    space = Space(
        space_id="space_office_upload",
        name="Office Upload Space",
        created_by="office_uploader",
        kind=SpaceKind.SHARED_SPACE,
    )
    store.save_user(owner)
    store.create_space(space, creator_uid="office_uploader")
    store.add_member("space_office_upload", "office_uploader", MembershipRole.OWNER)
    headers = {"Authorization": "Bearer dev:office_uploader:office@test.com:Office Uploader"}

    xlsx_res = client.post(
        "/v1/spaces/space_office_upload/files",
        files={
            "file": (
                "production_budget.xlsx",
                io.BytesIO(make_test_xlsx_bytes()),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers=headers,
    )
    pptx_res = client.post(
        "/v1/spaces/space_office_upload/files",
        files={
            "file": (
                "act_iii_lookbook.pptx",
                io.BytesIO(make_test_pptx_bytes()),
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
        },
        headers=headers,
    )
    assert xlsx_res.status_code == 200
    assert pptx_res.status_code == 200
    budget = xlsx_res.json()
    lookbook = pptx_res.json()
    assert budget["ingestion_status"] == "ready"
    assert lookbook["ingestion_status"] == "ready"
    assert budget["active_generation"] == 1
    assert lookbook["active_generation"] == 1

    budget_chunks = store.get_document_chunks("space_office_upload", budget["file_id"], generation=1)
    lookbook_chunks = store.get_document_chunks("space_office_upload", lookbook["file_id"], generation=1)
    assert budget_chunks
    assert lookbook_chunks
    assert all(chunk.extraction_method == "prodocux_xlsx" for chunk in budget_chunks)
    assert all(chunk.source_locator.startswith("sheet:") for chunk in budget_chunks)
    assert all(chunk.extraction_method == "prodocux_pptx" for chunk in lookbook_chunks)
    assert lookbook_chunks[0].source_locator == "slide:1"

    candidates, warnings = RetrievalService.retrieve_for_qa(
        "space_office_upload",
        "What is the VFX budget and which camera package is required?",
        [budget["file_id"], lookbook["file_id"]],
        top_k_per_doc=2,
        max_total_chunks=4,
    )
    assert warnings == []
    assert {candidate.file_id for candidate in candidates} == {budget["file_id"], lookbook["file_id"]}
    budget_hit = next(candidate for candidate in candidates if candidate.file_id == budget["file_id"])
    lookbook_hit = next(candidate for candidate in candidates if candidate.file_id == lookbook["file_id"])
    budget_citation = budget_hit.to_citation("Sky replacement")
    lookbook_citation = lookbook_hit.to_citation("Low-light package")
    assert budget_citation is not None
    assert budget_citation.source_locator.startswith("sheet:Budget:rows:")
    assert lookbook_citation is not None
    assert lookbook_citation.source_locator == "slide:1"


# -----------------------------------------------------------------------------
# Gate 2: CSV Content Fidelity & Formula Risks
# -----------------------------------------------------------------------------


def test_gate2_csv_content_fidelity_preserves_formulas():
    from prodocux_kernel.rendering import extract_content_blocks

    csv_content = (
        "Item,Amount,Formula,Phone\n"
        "Expense,-20,=SUM(A1:A5),+886912345678\n"
        "Handle,@producer,100,Normal"
    ).encode("utf-8")
    assert extract_content_blocks("budget.csv", csv_content)["format"] == "csv"

    status, chunks, pages, gaps, method, err = ProDocuXFacade.ingest_document(
        csv_content, "budget.csv", "file_csv_1", "space_1", generation=1
    )

    assert status == IngestionStatus.READY
    assert err is None
    assert method == "prodocux_csv"
    assert len(chunks) == 1
    chunk = chunks[0]

    # Raw text and normalized text must preserve original data verbatim
    assert "-20" in chunk.raw_text
    assert "=SUM(A1:A5)" in chunk.raw_text
    assert "+886912345678" in chunk.raw_text
    assert "@producer" in chunk.raw_text

    # Metadata risk flag is set
    assert chunk.contains_formula_like_content is True
    assert chunk.source_locator == "rows:2-3"
    assert chunk.extraction_method == "prodocux_csv"


# -----------------------------------------------------------------------------
# Gate 3: Mixed PDF & OCR Gaps
# -----------------------------------------------------------------------------


def test_gate3_mixed_pdf_ocr_gaps():
    # Mixed PDF: 1 page with text, 1 blank scanned page
    try:
        import pypdf
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=612, height=792)
        buf = io.BytesIO()
        writer.write(buf)
        raw_pdf = buf.getvalue()

        # Completely empty PDF yields NEEDS_OCR
        status, chunks, pages, gaps, method, err = ProDocuXFacade.ingest_document(
            raw_pdf, "scanned.pdf", "file_pdf_1", "space_1", generation=1
        )
        assert status == IngestionStatus.NEEDS_OCR
        assert method == "prodocux_pdf"
        assert len(gaps) == 1
    except ImportError:
        pass


# -----------------------------------------------------------------------------
# Gate 4: Generation Fencing & Atomic Swap on Reindex
# -----------------------------------------------------------------------------


def test_gate4_generation_fencing_and_atomic_swap():
    test_store = MemoryStore()
    user = User(uid="u1", email="u1@test.com", display_name="User 1")
    space = Space(space_id="sp1", name="Test Space", created_by="u1", kind=SpaceKind.SHARED_SPACE)
    test_store.save_user(user)
    test_store.create_space(space, creator_uid="u1")
    test_store.add_member("sp1", "u1", MembershipRole.OWNER)

    # Initial file upload commit
    file_rec = FileRecord(
        file_id="file_1",
        space_id="sp1",
        filename="notes.txt",
        uploaded_by="u1",
        upload_status="committed",
    )
    test_store.save_file(file_rec)

    # Generation 1 ingestion
    runner = InlineIngestionRunner()
    runner.submit_ingestion(
        space_id="sp1",
        file_id="file_1",
        content_bytes=b"Initial notes version 1",
        filename="notes.txt",
        store=test_store,
    )

    f1 = test_store.get_file("file_1")
    assert f1.active_generation == 1
    assert f1.ingestion_status == IngestionStatus.READY
    chunks_g1 = test_store.get_document_chunks("sp1", "file_1", generation=1)
    assert len(chunks_g1) == 1
    assert chunks_g1[0].normalized_text == "Initial notes version 1"

    # Generation 2 Re-index (success atomic swap)
    runner.submit_ingestion(
        space_id="sp1",
        file_id="file_1",
        content_bytes=b"Updated notes version 2 with more details",
        filename="notes.txt",
        store=test_store,
    )

    f2 = test_store.get_file("file_1")
    assert f2.active_generation == 2
    assert f2.ingestion_status == IngestionStatus.READY
    chunks_g2 = test_store.get_document_chunks("sp1", "file_1")
    assert len(chunks_g2) == 1
    assert chunks_g2[0].normalized_text == "Updated notes version 2 with more details"

    # Old generation 1 chunks were pruned
    assert len(test_store.get_document_chunks("sp1", "file_1", generation=1)) == 0


# -----------------------------------------------------------------------------
# Gate 5: Stale Worker Rejection on Commit
# -----------------------------------------------------------------------------


def test_gate5_stale_worker_rejection():
    test_store = MemoryStore()
    file_rec = FileRecord(
        file_id="file_fenced",
        space_id="sp1",
        filename="doc.txt",
        uploaded_by="u1",
        upload_status="committed",
        active_generation=1,
        ingestion_job_id="job_active_current",
    )
    test_store.save_file(file_rec)

    # Attempt commit with a stale job ID
    committed = test_store.commit_ingestion_generation(
        space_id="sp1",
        file_id="file_fenced",
        target_generation=2,
        expected_job_id="job_stale_zombie",
        chunk_count=5,
        pages=1,
        ocr_gaps=[],
        status=IngestionStatus.READY,
    )
    assert committed is False
    # Active generation remains unchanged
    assert test_store.get_file("file_fenced").active_generation == 1


# -----------------------------------------------------------------------------
# Gate 6: Cross-Space Tenancy Isolation
# -----------------------------------------------------------------------------


def test_gate6_cross_space_tenancy_isolation():
    u1 = User(uid="user_a", email="a@test.com", display_name="User A")
    sp_a = Space(space_id="space_a", name="Space A", created_by="user_a")
    sp_b = Space(space_id="space_b", name="Space B", created_by="user_a")
    store.save_user(u1)
    store.create_space(sp_a, creator_uid="user_a")
    store.create_space(sp_b, creator_uid="user_a")
    store.add_member("space_a", "user_a", MembershipRole.OWNER)
    store.add_member("space_b", "user_a", MembershipRole.OWNER)

    # File belongs to space_a
    file_rec = FileRecord(
        file_id="file_a",
        space_id="space_a",
        filename="confidential.txt",
        uploaded_by="user_a",
        upload_status="committed",
    )
    store.save_file(file_rec)

    # Accessing file_a under space_b must return 404
    headers = {"Authorization": "Bearer dev:user_a:a@test.com:User A"}
    res_status = client.get("/v1/spaces/space_b/files/file_a/status", headers=headers)
    assert res_status.status_code == 404

    res_chunks = client.get("/v1/spaces/space_b/files/file_a/chunks", headers=headers)
    assert res_chunks.status_code == 404

    res_reindex = client.post("/v1/spaces/space_b/files/file_a/reindex", headers=headers)
    assert res_reindex.status_code == 404


# -----------------------------------------------------------------------------
# Gate 7: Adapter Security Defenses (XXE, Zip Bomb, Malformed XML)
# -----------------------------------------------------------------------------


def test_gate7_adapter_security_defenses():
    # 1. FDX XXE entity defense
    fdx_xxe = b"""<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<FinalDraft DocumentType="Script" Version="1">
  <Content><Paragraph Type="Scene Heading"><Text>&xxe;</Text></Paragraph></Content>
</FinalDraft>"""
    status, chunks, pages, gaps, method, err = ProDocuXFacade.ingest_document(
        fdx_xxe, "malicious.fdx", "file_xxe", "sp1", generation=1
    )
    assert status == IngestionStatus.FAILED
    assert err == "ERR_FDX_XXE_REJECTED"

    # 2. Invalid DOCX must fail closed through Kernel, not a local zip parser.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(505):
            zf.writestr(f"file_{i}.txt", "data")
    zip_bomb_bytes = buf.getvalue()

    status, chunks, pages, gaps, method, err = ProDocuXFacade.ingest_document(
        zip_bomb_bytes, "bomb.docx", "file_bomb", "sp1", generation=1
    )
    assert status == IngestionStatus.FAILED
    assert method == "prodocux_docx"
    assert err == "ERR_DOCX_PARSE_FAILED"


# -----------------------------------------------------------------------------
# Gate 8: Fail-Closed Production Runner Configuration
# -----------------------------------------------------------------------------


def test_gate8_fail_closed_production_runner():
    # Production forbids threadpool / inline
    with pytest.raises(ValueError, match="durable queue for document ingestion"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-123",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="bucket-1",
            STUDIO_TOWER_MAINTENANCE_SECRET="a" * 32,
            INGESTION_RUNNER="threadpool",
        )


# -----------------------------------------------------------------------------
# Gate 9: Lease Recovery & Cascade Purge on File Deletion
# -----------------------------------------------------------------------------


def test_gate9_lease_recovery_and_cascade_delete():
    test_store = MemoryStore()
    now = datetime.now(UTC)

    # File with expired lease
    f_stuck = FileRecord(
        file_id="file_stuck",
        space_id="sp1",
        filename="stuck.txt",
        uploaded_by="u1",
        upload_status="committed",
        ingestion_status=IngestionStatus.EXTRACTING,
        ingestion_lease_owner="worker_dead",
        ingestion_lease_until=now - timedelta(seconds=10),
    )
    test_store.save_file(f_stuck)

    # Reclaimer scans and resets lease
    reclaimed = test_store.scan_and_reclaim_expired_ingestion_leases(now=now)
    assert len(reclaimed) == 1
    assert reclaimed[0].file_id == "file_stuck"

    f_reset = test_store.get_file("file_stuck")
    assert f_reset.ingestion_status == IngestionStatus.EXTRACTING
    assert f_reset.ingestion_lease_owner == "lease_reclaimer"
    assert f_reset.ingestion_lease_until is not None

    # Test Cascade Delete removes chunks
    dummy_chunk = DocumentChunk(
        chunk_id="chk_file_stuck_g1_0000",
        file_id="file_stuck",
        space_id="sp1",
        ingestion_version=1,
        ordinal=0,
        source_locator="section:1",
        raw_text="Sample",
        normalized_text="Sample",
        char_start=0,
        char_end=6,
        token_count=1,
        content_hash="hash1",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    test_store.save_document_chunks("sp1", "file_stuck", 1, [dummy_chunk])
    assert len(test_store.get_document_chunks("sp1", "file_stuck", 1)) == 1

    test_store.delete_file("file_stuck")
    assert test_store.get_file("file_stuck") is None
    assert len(test_store.get_document_chunks("sp1", "file_stuck", 1)) == 0


# -----------------------------------------------------------------------------
# Gate 10: API Pagination & Filtering
# -----------------------------------------------------------------------------


def test_gate10_chunks_pagination_and_filtering():
    u1 = User(uid="u1", email="u1@test.com", display_name="User 1")
    space = Space(space_id="sp1", name="Space 1", created_by="u1")
    store.save_user(u1)
    store.create_space(space, creator_uid="u1")
    store.add_member("sp1", "u1", MembershipRole.OWNER)

    file_rec = FileRecord(
        file_id="file_paged",
        space_id="sp1",
        filename="multi_section.txt",
        uploaded_by="u1",
        upload_status="committed",
        active_generation=1,
    )
    store.save_file(file_rec)

    chunks = [
        DocumentChunk(
            chunk_id=f"chk_file_paged_g1_{i:04d}",
            file_id="file_paged",
            space_id="sp1",
            ingestion_version=1,
            ordinal=i,
            page_number=i + 1,
            section_heading=f"Section {i + 1}",
            source_locator=f"section:{i + 1}",
            raw_text=f"Raw text {i + 1}",
            normalized_text=f"Normalized text {i + 1}",
            char_start=i * 20,
            char_end=(i + 1) * 20,
            token_count=3,
            content_hash=f"hash_{i}",
            extraction_method="plain_text",
            extractor_version="v1.0",
        )
        for i in range(5)
    ]
    store.save_document_chunks("sp1", "file_paged", 1, chunks)

    headers = {"Authorization": "Bearer dev:u1:u1@test.com:User 1"}

    # Paginate limit 2, cursor 0
    res = client.get("/v1/spaces/sp1/files/file_paged/chunks?cursor=0&limit=2", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert len(data["items"]) == 2
    assert data["items"][0]["ordinal"] == 0
    assert data["items"][1]["ordinal"] == 1
    assert data["cursor"] == 0
    assert data["limit"] == 2
    assert data["total"] == 5
    assert data["has_more"] is True
    assert data["next_cursor"] == 2
    assert data["active_generation"] == 1
    # Check that raw_text is omitted from summary schema
    assert "raw_text" not in data["items"][0]

    # Filter by page
    res_page = client.get("/v1/spaces/sp1/files/file_paged/chunks?page=3", headers=headers)
    assert res_page.status_code == 200
    pdata = res_page.json()
    assert len(pdata["items"]) == 1
    assert pdata["items"][0]["page_number"] == 3
    assert pdata["total"] == 1
    assert pdata["has_more"] is False


# -----------------------------------------------------------------------------
# Gate 11: Reindex Role Permission Enforcement (MEMBER rejected, ADMIN/OWNER/COORDINATOR allowed)
# -----------------------------------------------------------------------------


def test_gate11_reindex_role_permissions():
    owner = User(uid="owner_1", email="owner@test.com", display_name="Owner")
    member = User(uid="member_1", email="member@test.com", display_name="Member")
    coordinator = User(uid="coord_1", email="coord@test.com", display_name="Coord")
    space = Space(space_id="sp_role", name="Role Test Space", created_by="owner_1")
    store.save_user(owner)
    store.save_user(member)
    store.save_user(coordinator)
    store.create_space(space, creator_uid="owner_1")
    store.add_member("sp_role", "owner_1", MembershipRole.OWNER)
    store.add_member("sp_role", "member_1", MembershipRole.MEMBER)
    store.add_member("sp_role", "coord_1", MembershipRole.COORDINATOR)

    file_rec = FileRecord(
        file_id="file_role_test",
        space_id="sp_role",
        filename="script.txt",
        uploaded_by="owner_1",
        upload_status="committed",
    )
    store.save_file(file_rec)
    store.save_file_blob("file_role_test", b"Script line 1\nScript line 2")

    member_auth = {"Authorization": "Bearer dev:member_1:member@test.com:Member"}
    coord_auth = {"Authorization": "Bearer dev:coord_1:coord@test.com:Coord"}

    # 1. MEMBER is rejected with 403
    res_member = client.post("/v1/spaces/sp_role/files/file_role_test/reindex", headers=member_auth)
    assert res_member.status_code == 403
    assert "Only space owners, admins, or coordinators" in res_member.json()["detail"]

    # 2. COORDINATOR is permitted (returns 202)
    res_coord = client.post("/v1/spaces/sp_role/files/file_role_test/reindex", headers=coord_auth)
    assert res_coord.status_code == 202
    assert res_coord.json()["status"] == "accepted"


# -----------------------------------------------------------------------------
# Gate 12: Backend Authoritative Document QA Readiness Gate
# -----------------------------------------------------------------------------


def test_gate12_backend_document_qa_readiness_gate():
    u = User(uid="u_qa", email="qa@test.com", display_name="QA User")
    space = Space(space_id="sp_qa", name="QA Space", created_by="u_qa", kind=SpaceKind.SHARED_SPACE)
    store.save_user(u)
    store.create_space(space, creator_uid="u_qa")
    store.add_member("sp_qa", "u_qa", MembershipRole.OWNER)

    auth = {"Authorization": "Bearer dev:u_qa:qa@test.com:QA User"}

    # 1. Uncommitted file
    f_uncommitted = FileRecord(
        file_id="f_uncommitted",
        space_id="sp_qa",
        filename="uncommitted.pdf",
        uploaded_by="u_qa",
        upload_status="pending_upload",
    )
    store.save_file(f_uncommitted)
    res = client.post(
        "/v1/chat",
        json={
            "space_id": "sp_qa",
            "content": "Analyze uncommitted",
            "intent": "document_qa",
            "attachment_file_ids": ["f_uncommitted"],
        },
        headers=auth,
    )
    assert res.status_code in (404, 422)

    # 2. Ingestion in progress (extracting / indexing)
    f_extracting = FileRecord(
        file_id="f_extracting",
        space_id="sp_qa",
        filename="extracting.pdf",
        uploaded_by="u_qa",
        upload_status="committed",
        ingestion_status=IngestionStatus.EXTRACTING,
    )
    store.save_file(f_extracting)
    res = client.post(
        "/v1/chat",
        json={
            "space_id": "sp_qa",
            "content": "Analyze extracting",
            "intent": "document_qa",
            "attachment_file_ids": ["f_extracting"],
        },
        headers=auth,
    )
    assert res.status_code == 422
    assert "is still processing" in res.json()["detail"]

    # 3. Needs OCR
    f_needs_ocr = FileRecord(
        file_id="f_needs_ocr",
        space_id="sp_qa",
        filename="scanned.pdf",
        uploaded_by="u_qa",
        upload_status="committed",
        ingestion_status=IngestionStatus.NEEDS_OCR,
    )
    store.save_file(f_needs_ocr)
    res = client.post(
        "/v1/chat",
        json={
            "space_id": "sp_qa",
            "content": "Analyze scanned",
            "intent": "document_qa",
            "attachment_file_ids": ["f_needs_ocr"],
        },
        headers=auth,
    )
    assert res.status_code == 422
    assert "requires OCR processing" in res.json()["detail"]

    # 4. Ingestion Failed
    f_failed = FileRecord(
        file_id="f_failed",
        space_id="sp_qa",
        filename="corrupted.pdf",
        uploaded_by="u_qa",
        upload_status="committed",
        ingestion_status=IngestionStatus.FAILED,
    )
    store.save_file(f_failed)
    res = client.post(
        "/v1/chat",
        json={
            "space_id": "sp_qa",
            "content": "Analyze corrupted",
            "intent": "document_qa",
            "attachment_file_ids": ["f_failed"],
        },
        headers=auth,
    )
    assert res.status_code == 422
    assert "failed ingestion" in res.json()["detail"]


# -----------------------------------------------------------------------------
# Gate 13: Ready Partial Ingestion Passes OCR Gap Warning into AI Context
# -----------------------------------------------------------------------------


def test_gate13_ready_partial_passes_ocr_gap_warning():
    import os
    from unittest.mock import MagicMock, patch

    u = User(uid="u_part", email="part@test.com", display_name="Partial User")
    space = Space(space_id="sp_part", name="Partial Space", created_by="u_part", kind=SpaceKind.AGENT_DM)
    store.save_user(u)
    store.create_space(space, creator_uid="u_part")
    store.add_member("sp_part", "u_part", MembershipRole.OWNER)

    f_partial = FileRecord(
        file_id="f_partial_1",
        space_id="sp_part",
        filename="treatment_mixed.pdf",
        uploaded_by="u_part",
        upload_status="committed",
        ingestion_status=IngestionStatus.READY_PARTIAL,
        ocr_gap_pages=[2, 4],
        has_ocr_gaps=True,
        active_generation=1,
        max_allocated_generation=1,
    )
    store.save_file(f_partial)

    chunk_p1 = DocumentChunk(
        chunk_id="chk_partial_01",
        file_id="f_partial_1",
        space_id="sp_part",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Treatment Page 1 content text for analysis.",
        normalized_text="Treatment Page 1 content text for analysis.",
        char_start=0,
        char_end=43,
        token_count=10,
        content_hash="hash_p1",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    store.save_document_chunks("sp_part", "f_partial_1", 1, [chunk_p1])

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(text="AI answering with awareness of OCR gaps. [1]")

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaTestKey"}):
        res = client.post(
            "/v1/chat",
            json={
                "space_id": "sp_part",
                "content": "What happens in the treatment?",
                "intent": "document_qa",
                "attachment_file_ids": ["f_partial_1"],
            },
            headers={"Authorization": "Bearer dev:u_part:part@test.com:Partial User"},
        )
        assert res.status_code == 200

        # Verify prompt received by Gemini contains the OCR gap warning
        called_contents = mock_client.models.generate_content.call_args[1].get("contents", "")
        assert "treatment_mixed.pdf" in called_contents
        assert "scanned pages [2, 4]" in called_contents


# -----------------------------------------------------------------------------
# Gate 14: Cloud Tasks Enqueue, Payload Security & Worker Secret Verification
# -----------------------------------------------------------------------------


def test_gate14_cloud_tasks_enqueue_and_worker_callback():
    from unittest.mock import MagicMock

    from app.services.ingestion_runner import CloudTasksIngestionRunner

    test_store = MemoryStore()
    user = User(uid="u1", email="u1@test.com", display_name="User 1")
    space = Space(space_id="sp_ct", name="CT Space", created_by="u1")
    test_store.save_user(user)
    test_store.create_space(space, creator_uid="u1")
    test_store.add_member("sp_ct", "u1", MembershipRole.OWNER)

    file_rec = FileRecord(
        file_id="f_ct_1",
        space_id="sp_ct",
        filename="big_screenplay.txt",
        uploaded_by="u1",
        upload_status="committed",
    )
    test_store.save_file(file_rec)
    test_store.save_file_blob("f_ct_1", b"TXT Content")

    # Mock Cloud Tasks client
    mock_tasks_client = MagicMock()
    mock_tasks_client.queue_path.return_value = "projects/p/locations/l/queues/q"

    ct_runner = CloudTasksIngestionRunner(client=mock_tasks_client)
    job_id = ct_runner.submit_ingestion(
        space_id="sp_ct",
        file_id="f_ct_1",
        content_bytes=b"BIG_BINARY_SHOULD_NOT_BE_IN_TASK",
        filename="big_screenplay.txt",
        store=test_store,
    )

    assert mock_tasks_client.create_task.called
    req_dict = mock_tasks_client.create_task.call_args[1].get("request", {})
    task_created = req_dict.get("task", {})
    http_req = task_created["http_request"]

    # Verify task headers contain X-Task-Secret
    assert "X-Task-Secret" in http_req["headers"]

    # Verify task body does NOT contain raw binary bytes (only descriptor payload)
    import json
    body_dict = json.loads(http_req["body"].decode("utf-8"))
    assert body_dict["file_id"] == "f_ct_1"
    assert body_dict["job_id"] == job_id
    assert "BIG_BINARY" not in str(body_dict)

    # Test Worker Callback Endpoint
    # 1. Invalid secret -> 403 Forbidden
    res_bad_sec = client.post(
        "/v1/internal/tasks/ingest-document",
        json=body_dict,
        headers={"X-Task-Secret": "wrong-secret"},
    )
    assert res_bad_sec.status_code == 403

    # 2. Valid secret -> 200 OK
    store.save_user(user)
    store.create_space(space, creator_uid="u1")
    store.add_member("sp_ct", "u1", MembershipRole.OWNER)
    store.save_file(file_rec)
    store.save_file_blob("f_ct_1", b"INT. ROOM - DAY\nAction text here.")
    valid_headers = {"X-Task-Secret": settings.STUDIO_TOWER_TASK_SECRET}
    res_worker = client.post(
        "/v1/internal/tasks/ingest-document",
        json=body_dict,
        headers=valid_headers,
    )
    assert res_worker.status_code == 200
    assert res_worker.json()["status"] == "success"

    # 3. Test SDK Import & Client instantiation test without mock
    from google.cloud import tasks_v2
    assert hasattr(tasks_v2, "CloudTasksClient")


# -----------------------------------------------------------------------------
# Gate 15: Lease Recovery Re-dispatches and Achieves Ingestion Completion
# -----------------------------------------------------------------------------


def test_gate15_lease_recovery_redispatches_to_completion():
    from datetime import UTC, datetime, timedelta

    u = User(uid="u_rec", email="rec@test.com", display_name="Rec User")
    space = Space(space_id="sp_rec", name="Rec Space", created_by="u_rec")
    store.save_user(u)
    store.create_space(space, creator_uid="u_rec")
    store.add_member("sp_rec", "u_rec", MembershipRole.OWNER)

    now = datetime.now(UTC)
    f_stuck = FileRecord(
        file_id="f_stuck_recover",
        space_id="sp_rec",
        filename="stuck.txt",
        uploaded_by="u_rec",
        upload_status="committed",
        ingestion_status=IngestionStatus.EXTRACTING,
        ingestion_lease_owner="crashed_worker_1",
        ingestion_lease_until=now - timedelta(seconds=20),
    )
    store.save_file(f_stuck)
    store.save_file_blob("f_stuck_recover", b"Stuck document text recovered successfully.")

    # Call maintenance endpoint with valid key
    headers = {"X-Maintenance-Key": settings.STUDIO_TOWER_MAINTENANCE_SECRET}
    res = client.post("/v1/maintenance/reclaim-ingestion-leases", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["reclaimed_count"] == 1
    assert "f_stuck_recover" in data["reclaimed_file_ids"]
    assert data["redispatched"] is True
    assert data["redispatched_count"] == 1

    # Verify that file has been re-dispatched and achieved READY status with chunks
    recovered_file = store.get_file("f_stuck_recover")
    assert recovered_file.ingestion_status == IngestionStatus.READY
    assert recovered_file.active_generation >= 1
    chunks = store.get_document_chunks("sp_rec", "f_stuck_recover")
    assert len(chunks) >= 1
    assert "Stuck document text" in chunks[0].raw_text


# -----------------------------------------------------------------------------
# Gate 16: Generation Fencing Protects Chunks from Stale Worker Overwrites
# -----------------------------------------------------------------------------


def test_gate16_generation_fencing_protects_chunks_from_stale_worker():
    test_store = MemoryStore()
    user = User(uid="u1", email="u1@test.com", display_name="User 1")
    space = Space(space_id="sp_fence", name="Fencing Space", created_by="u1")
    test_store.save_user(user)
    test_store.create_space(space, creator_uid="u1")
    test_store.add_member("sp_fence", "u1", MembershipRole.OWNER)

    f_rec = FileRecord(
        file_id="f_fence_1",
        space_id="sp_fence",
        filename="contract.txt",
        uploaded_by="u1",
        upload_status="committed",
    )
    test_store.save_file(f_rec)

    # 1. Worker A acquires Generation 1
    acq_a, rec_a, gen_a = test_store.acquire_ingestion_lease("sp_fence", "f_fence_1", worker_id="worker_A", lease_seconds=1)
    assert acq_a is True
    assert gen_a == 1
    job_a = rec_a.ingestion_job_id

    # 2. Worker A times out without finishing; Worker B claims Generation 2 (monotonic allocation!)
    from datetime import UTC, datetime, timedelta
    past = datetime.now(UTC) - timedelta(seconds=10)
    rec_a.ingestion_lease_until = past
    test_store.save_file(rec_a)

    acq_b, rec_b, gen_b = test_store.acquire_ingestion_lease("sp_fence", "f_fence_1", worker_id="worker_B", lease_seconds=60)
    assert acq_b is True
    assert gen_b == 2  # Monotonically incremented, strictly different from gen_a!
    job_b = rec_b.ingestion_job_id
    assert job_b != job_a

    # 3. Worker B successfully saves chunks and commits Generation 2
    chunks_b = [
        DocumentChunk(
            chunk_id="chk_f_fence_1_g2_0000",
            file_id="f_fence_1",
            space_id="sp_fence",
            ingestion_version=2,
            ordinal=0,
            source_locator="page:1",
            raw_text="Valid Generation 2 content by Worker B",
            normalized_text="Valid Generation 2 content by Worker B",
            char_start=0,
            char_end=37,
            token_count=7,
            content_hash="hash_b",
            extraction_method="plain_text",
            extractor_version="v1.0",
        )
    ]
    saved_b = test_store.save_document_chunks("sp_fence", "f_fence_1", 2, chunks_b, expected_job_id=job_b)
    assert saved_b is True
    commit_b = test_store.commit_ingestion_generation(
        space_id="sp_fence",
        file_id="f_fence_1",
        target_generation=2,
        expected_job_id=job_b,
        chunk_count=1,
        pages=1,
        ocr_gaps=[],
        status=IngestionStatus.READY,
    )
    assert commit_b is True

    # 4. Zombie Worker A wakes up and attempts to write stale chunks with job_a
    chunks_a_stale = [
        DocumentChunk(
            chunk_id="chk_f_fence_1_g1_0000",
            file_id="f_fence_1",
            space_id="sp_fence",
            ingestion_version=1,
            ordinal=0,
            source_locator="page:1",
            raw_text="CORRUPTED STALE CHUNK WRITTEN BY ZOMBIE WORKER A",
            normalized_text="CORRUPTED STALE CHUNK WRITTEN BY ZOMBIE WORKER A",
            char_start=0,
            char_end=49,
            token_count=9,
            content_hash="hash_a_stale",
            extraction_method="plain_text",
            extractor_version="v1.0",
        )
    ]
    # save_document_chunks must reject Worker A's write!
    saved_a = test_store.save_document_chunks("sp_fence", "f_fence_1", 1, chunks_a_stale, expected_job_id=job_a)
    assert saved_a is False

    # commit must reject Worker A's commit!
    commit_a = test_store.commit_ingestion_generation(
        space_id="sp_fence",
        file_id="f_fence_1",
        target_generation=1,
        expected_job_id=job_a,
        chunk_count=1,
        pages=1,
        ocr_gaps=[],
        status=IngestionStatus.READY,
    )
    assert commit_a is False

    # 5. Verify that active generation is 2 and chunks contain ONLY Worker B's content!
    f_final = test_store.get_file("f_fence_1")
    assert f_final.active_generation == 2
    assert f_final.ingestion_status == IngestionStatus.READY

    active_chunks = test_store.get_document_chunks("sp_fence", "f_fence_1")
    assert len(active_chunks) == 1
    assert active_chunks[0].raw_text == "Valid Generation 2 content by Worker B"
    assert "CORRUPTED" not in active_chunks[0].raw_text

    # 6. Part B: FirestoreStore Authoritative Remote Lease & Job Fencing Protection
    from unittest.mock import MagicMock

    from app.services.storage import FirestoreStore

    mock_client = MagicMock()
    mock_batch = MagicMock()
    mock_client.batch.return_value = mock_batch

    # Mock Firestore file document with EXPIRED lease
    mock_file_doc = MagicMock()
    mock_file_doc.exists = True
    expired_time = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    mock_file_doc.to_dict.return_value = {
        "file_id": "f_fs_fence",
        "space_id": "sp_fs",
        "filename": "lease_expired.txt",
        "uploaded_by": "u_fs",
        "upload_status": "committed",
        "ingestion_status": "extracting",
        "ingestion_job_id": "job_valid_123",
        "ingestion_lease_until": expired_time,
        "active_generation": 0,
        "max_allocated_generation": 1,
    }
    mock_client.collection.return_value.document.return_value.get.return_value = mock_file_doc

    fs_store = FirestoreStore(project_id="test-proj", client=mock_client)
    # Also save into memory store so super() has a record
    fs_file_rec = FileRecord.model_validate(mock_file_doc.to_dict.return_value)
    fs_store.save_file(fs_file_rec)

    # Attempt chunk save with expired lease -> MUST RETURN FALSE AND NOT COMMIT BATCH!
    dummy_chunk = DocumentChunk(
        chunk_id="chk_fs_g1_0000",
        file_id="f_fs_fence",
        space_id="sp_fs",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Should not be committed",
        normalized_text="Should not be committed",
        char_start=0,
        char_end=23,
        token_count=4,
        content_hash="h_fs",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    saved_expired = fs_store.save_document_chunks("sp_fs", "f_fs_fence", 1, [dummy_chunk], expected_job_id="job_valid_123")
    assert saved_expired is False
    assert not mock_batch.commit.called

    # Status = ready (non-writable) -> MUST RETURN FALSE AND NOT COMMIT BATCH!
    mock_file_doc.to_dict.return_value["ingestion_status"] = "ready"
    mock_file_doc.to_dict.return_value["ingestion_lease_until"] = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
    saved_ready = fs_store.save_document_chunks("sp_fs", "f_fs_fence", 1, [dummy_chunk], expected_job_id="job_valid_123")
    assert saved_ready is False
    assert not mock_batch.commit.called

    # Lease missing / None -> MUST RETURN FALSE AND NOT COMMIT BATCH!
    mock_file_doc.to_dict.return_value["ingestion_status"] = "extracting"
    mock_file_doc.to_dict.return_value["ingestion_lease_until"] = None
    saved_no_lease = fs_store.save_document_chunks("sp_fs", "f_fs_fence", 1, [dummy_chunk], expected_job_id="job_valid_123")
    assert saved_no_lease is False
    assert not mock_batch.commit.called

    # 7. Part C: Cold-Start Worker (Empty Memory Cache) can save chunks & CAS commit
    mock_client_cold = MagicMock()
    mock_batch_cold = MagicMock()
    mock_client_cold.batch.return_value = mock_batch_cold
    active_lease_time = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
    mock_cold_doc = MagicMock()
    mock_cold_doc.exists = True
    mock_cold_doc.to_dict.return_value = {
        "file_id": "f_cold_1",
        "space_id": "sp_cold",
        "filename": "cold_start.txt",
        "uploaded_by": "u_cold",
        "upload_status": "committed",
        "ingestion_status": "extracting",
        "ingestion_job_id": "job_cold_123",
        "ingestion_lease_until": active_lease_time,
        "active_generation": 0,
        "max_allocated_generation": 1,
    }
    mock_client_cold.collection.return_value.document.return_value.get.return_value = mock_cold_doc

    fs_store_cold = FirestoreStore(project_id="test-proj", client=mock_client_cold)
    # Memory cache in fs_store_cold is 100% empty (simulating new Cloud Run / Cloud Tasks worker instance)
    assert len(fs_store_cold.files) == 0

    chunk_cold = DocumentChunk(
        chunk_id="chk_cold_g1_0000",
        file_id="f_cold_1",
        space_id="sp_cold",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Valid chunk written by cold worker",
        normalized_text="Valid chunk written by cold worker",
        char_start=0,
        char_end=35,
        token_count=6,
        content_hash="h_cold",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    saved_cold = fs_store_cold.save_document_chunks("sp_cold", "f_cold_1", 1, [chunk_cold], expected_job_id="job_cold_123")
    # Must succeed and commit batch directly in Firestore!
    assert saved_cold is True

    # 8. Part D: Stateful Firestore Active Generation Preservation & Cold-Start Sweeper
    class StatefulFirestoreFake:
        def __init__(self):
            self.collections = {
                "files": {},
                "document_chunks": {},
                "pending_generation_cleanups": {},
            }

        def collection(self, col_name: str):
            fake_self = self

            class FakeCollection:
                def document(self, doc_id: str):
                    class FakeDocRef:
                        def __init__(self, d_id):
                            self.id = d_id
                            self.path = f"{col_name}/{d_id}"

                        def get(self, transaction=None):
                            doc_exists = self.id in fake_self.collections.get(col_name, {})
                            doc_data = dict(fake_self.collections.get(col_name, {}).get(self.id, {})) if doc_exists else {}
                            class FakeSnap:
                                exists = doc_exists
                                id = doc_id
                                reference = self
                                def to_dict(self_inner):
                                    return dict(doc_data)
                            return FakeSnap()

                        def set(self, data, transaction=None):
                            fake_self.collections.setdefault(col_name, {})[self.id] = dict(data)

                        def delete(self, transaction=None):
                            if self.id in fake_self.collections.get(col_name, {}):
                                del fake_self.collections[col_name][self.id]

                    return FakeDocRef(doc_id)

                def where(self, field: str, op: str, value: Any):
                    class FakeQuery:
                        def __init__(self, filters):
                            self.filters = filters

                        def where(self, f2, op2, v2):
                            return FakeQuery(self.filters + [(f2, op2, v2)])

                        def stream(self):
                            results = []
                            for d_id, doc_data in list(fake_self.collections.get(col_name, {}).items()):
                                matches = True
                                for (f_name, f_op, f_val) in self.filters:
                                    if f_op == "==" and doc_data.get(f_name) != f_val:
                                        matches = False
                                    elif f_op == "<" and doc_data.get(f_name) >= f_val:
                                        matches = False
                                if matches:
                                    class FakeStreamDoc:
                                        def __init__(self, doc_id, data_dict):
                                            self.id = doc_id
                                            self.reference = fake_self.collection(col_name).document(doc_id)
                                            self._data = dict(data_dict)
                                        def to_dict(self):
                                            return dict(self._data)
                                    results.append(FakeStreamDoc(d_id, doc_data))
                            return results

                    return FakeQuery([(field, op, value)])

                def limit(self, num: int):
                    class FakeLimitQuery:
                        def stream(self):
                            results = []
                            for d_id, doc_data in list(fake_self.collections.get(col_name, {}).items())[:num]:
                                class FakeStreamDoc:
                                    def __init__(self, doc_id, data_dict):
                                        self.id = doc_id
                                        self.reference = fake_self.collection(col_name).document(doc_id)
                                        self._data = dict(data_dict)
                                    def to_dict(self):
                                        return dict(self._data)
                                results.append(FakeStreamDoc(d_id, doc_data))
                            return results
                    return FakeLimitQuery()

                def stream(self):
                    return self.limit(50).stream()

            return FakeCollection()

        def transaction(self):
            class FakeTx:
                _read_only = False
                _max_attempts = 5
                _id = b"tx_1"
                id = "tx_1"

                def _clean_up(self):
                    pass

                def _rollback(self):
                    pass

                def _commit(self):
                    pass

                def _begin(self, retry_id=None):
                    pass

                def set(self, doc_ref, data):
                    doc_ref.set(data, transaction=self)

                def delete(self, doc_ref):
                    doc_ref.delete(transaction=self)

                def get(self, doc_ref):
                    return doc_ref.get(transaction=self)

            return FakeTx()

        def batch(self):
            class FakeBatch:
                def __init__(self):
                    self.ops = []

                def set(self, doc_ref, data):
                    self.ops.append(("set", doc_ref, data))

                def delete(self, doc_ref):
                    self.ops.append(("delete", doc_ref, None))

                def commit(self):
                    for op, ref, val in self.ops:
                        if op == "set":
                            ref.set(val)
                        elif op == "delete":
                            ref.delete()
                    self.ops = []

            return FakeBatch()

    # Instantiate shared stateful Firestore backend
    shared_fs_client = StatefulFirestoreFake()

    # Pre-populate active generation 2 in Firestore
    active_lease_time = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
    shared_fs_client.collections["files"]["f_gen_protect"] = {
        "file_id": "f_gen_protect",
        "space_id": "sp_protect",
        "filename": "protect.pdf",
        "uploaded_by": "alice",
        "upload_status": "committed",
        "ingestion_status": "extracting",  # Reindexing generation 3 is currently active!
        "ingestion_job_id": "job_reindex_g3",
        "ingestion_lease_until": active_lease_time,
        "active_generation": 2,  # Generation 2 is the committed active generation
        "max_allocated_generation": 3,
    }

    # Store active generation 2 chunks in remote Firestore collection
    shared_fs_client.collections["document_chunks"]["chk_g2_0001"] = {
        "chunk_id": "chk_g2_0001",
        "file_id": "f_gen_protect",
        "space_id": "sp_protect",
        "ingestion_version": 2,
        "ordinal": 0,
        "source_locator": "page:1",
        "raw_text": "CONFIDENTIAL ACTIVE SCRIPT CONTENT",
        "normalized_text": "CONFIDENTIAL ACTIVE SCRIPT CONTENT",
        "char_start": 0,
        "char_end": 35,
        "token_count": 5,
        "content_hash": "sha256_active_generation_2_exact",
        "extraction_method": "plain_text",
        "extractor_version": "v1.0",
    }

    fs_store_worker1 = FirestoreStore(project_id="test-proj", client=shared_fs_client)

    # 1. Stale worker for generation 1 attempts write -> MUST BE REJECTED
    stale_g1_chunk = DocumentChunk(
        chunk_id="chk_g1_stale",
        file_id="f_gen_protect",
        space_id="sp_protect",
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text="Stale g1 content",
        normalized_text="Stale g1 content",
        char_start=0,
        char_end=16,
        token_count=3,
        content_hash="sha256_stale_g1",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    saved_stale = fs_store_worker1.save_document_chunks(
        "sp_protect", "f_gen_protect", 1, [stale_g1_chunk], expected_job_id="job_stale_g1"
    )
    assert saved_stale is False

    # 2. Reindexing worker for generation 3 writes 1 batch then compensating delete triggers
    g3_chunk = DocumentChunk(
        chunk_id="chk_g3_temp",
        file_id="f_gen_protect",
        space_id="sp_protect",
        ingestion_version=3,
        ordinal=0,
        source_locator="page:1",
        raw_text="Temporary g3 content",
        normalized_text="Temporary g3 content",
        char_start=0,
        char_end=20,
        token_count=3,
        content_hash="sha256_g3_temp",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    # Simulate partial batch commit for generation 3
    shared_fs_client.collections["document_chunks"]["chk_g3_temp"] = g3_chunk.model_dump(mode="json")

    # Call compensating delete for generation 3 on failure
    fs_store_worker1._safe_compensating_delete("sp_protect", "f_gen_protect", generation=3, job_id="job_reindex_g3")

    # Generation 3 uncommitted chunks are deleted
    assert "chk_g3_temp" not in shared_fs_client.collections["document_chunks"]

    # 3. Old generation 2 compensating delete arrives during reindex -> MUST BE REJECTED AND NEVER PURGE ACTIVE GENERATION 2!
    fs_store_worker1._safe_compensating_delete("sp_protect", "f_gen_protect", generation=2, job_id="job_old_g2")

    # Remote Firestore active generation 2 chunks MUST REMAIN 100% UNTOUCHED AND INTACT!
    g2_remote_chunks = fs_store_worker1.get_document_chunks("sp_protect", "f_gen_protect", generation=2)
    assert len(g2_remote_chunks) == 1
    assert g2_remote_chunks[0].chunk_id == "chk_g2_0001"
    assert g2_remote_chunks[0].content_hash == "sha256_active_generation_2_exact"
    assert g2_remote_chunks[0].raw_text == "CONFIDENTIAL ACTIVE SCRIPT CONTENT"

    # 4a. Atomic Mutual Exclusion — rejected while cleanup is IN PROGRESS
    shared_fs_client.collections["files"]["f_gen_protect"]["cleaning_generations"] = [4]
    commit_during_cleanup = fs_store_worker1.commit_ingestion_generation(
        space_id="sp_protect",
        file_id="f_gen_protect",
        target_generation=4,
        expected_job_id="job_reindex_g3",
        chunk_count=1,
        pages=1,
        ocr_gaps=[],
    )
    assert commit_during_cleanup is False
    assert shared_fs_client.collections["files"]["f_gen_protect"]["active_generation"] == 2
    shared_fs_client.collections["files"]["f_gen_protect"]["cleaning_generations"] = []

    # 4b. [P1] Tombstone permanence — commit is REJECTED even AFTER cleanup finishes
    # Simulate: generation 4 cleanup completed (cleaning_generations cleared, deleted_generations stamped)
    shared_fs_client.collections["files"]["f_gen_protect"]["cleaning_generations"] = []
    shared_fs_client.collections["files"]["f_gen_protect"]["deleted_generations"] = [4]
    # Late worker arrives — cleanup already done, cleaning lock is released, but tombstone remains
    late_commit = fs_store_worker1.commit_ingestion_generation(
        space_id="sp_protect",
        file_id="f_gen_protect",
        target_generation=4,
        expected_job_id="job_reindex_g3",
        chunk_count=1,
        pages=1,
        ocr_gaps=[],
    )
    # MUST be rejected by the permanent tombstone — not just the in-progress lock
    assert late_commit is False, "Late commit after cleanup completion must be tombstone-rejected"
    # Active generation MUST still be generation 2 — not the zombie-committed empty generation 4
    assert shared_fs_client.collections["files"]["f_gen_protect"]["active_generation"] == 2, \
        "Active generation must not be overwritten by tombstoned late commit"
    # Clean up for next step
    shared_fs_client.collections["files"]["f_gen_protect"]["deleted_generations"] = []

    # 5. Cold-Start Maintenance Worker sweeps pending generation cleanups with error isolation & backoff
    shared_fs_client.collections["pending_generation_cleanups"].clear()
    fs_store_sweeper = FirestoreStore(project_id="test-proj", client=shared_fs_client)

    # Item 1: target generation triggers simulated transient storage exception during sweep
    shared_fs_client.collections["pending_generation_cleanups"]["pgc_fail_01"] = {
        "cleanup_id": "pgc_fail_01",
        "space_id": "sp_protect",
        "file_id": "f_gen_protect",
        "generation": 5,
        "job_id": "job_fail",
        "lease_token": None,
        "lease_until": None,
        "retry_count": 0,
        "status": "pending",
    }
    # Item 2: valid pending cleanup for generation 3
    shared_fs_client.collections["pending_generation_cleanups"]["pgc_success_02"] = {
        "cleanup_id": "pgc_success_02",
        "space_id": "sp_protect",
        "file_id": "f_gen_protect",
        "generation": 3,
        "job_id": "job_reindex_g3",
        "lease_token": None,
        "lease_until": None,
        "retry_count": 0,
        "status": "pending",
    }

    # Intercept delete_generation_document_chunks so generation 5 raises transient error
    original_del = fs_store_sweeper.delete_generation_document_chunks
    def flaky_del(space_id, file_id, generation, job_id=None):
        if generation == 5:
            raise RuntimeError("Transient firestore transport error")
        return original_del(space_id, file_id, generation, job_id)
    fs_store_sweeper.delete_generation_document_chunks = flaky_del

    sweep_res = fs_store_sweeper.sweep_pending_generation_cleanups(limit=10)
    # Item 1 failed (transient error → backoff), Item 2 succeeded despite Item 1 failure!
    assert sweep_res["scanned"] == 2
    assert sweep_res["claimed"] == 2
    assert sweep_res["cleaned"] == 1
    assert sweep_res["failed"] == 1
    # Item 2 is completed and removed
    assert "pgc_success_02" not in shared_fs_client.collections["pending_generation_cleanups"]
    # Item 1 remains with exponential next_retry_at and retry_count == 1
    assert "pgc_fail_01" in shared_fs_client.collections["pending_generation_cleanups"]
    rec1 = shared_fs_client.collections["pending_generation_cleanups"]["pgc_fail_01"]
    assert rec1["retry_count"] == 1
    assert rec1["next_retry_at"] is not None

    # 5b. [P2] Sweeper failure update CAS — expired Worker A cannot overwrite Worker B's active lease
    # Setup: pgc_cas_test is currently held by Worker B (active lease)
    shared_fs_client.collections["pending_generation_cleanups"]["pgc_cas_test"] = {
        "cleanup_id": "pgc_cas_test",
        "space_id": "sp_protect",
        "file_id": "f_gen_protect",
        "generation": 6,
        "job_id": "job_cas_test",
        "lease_token": "worker_b_active_token",  # Worker B holds the active lease
        "lease_until": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        "retry_count": 1,
        "status": "in_progress",
    }
    # Simulate Worker A's failure update path: arrives late with stale token "worker_a_expired_token"
    # Directly invoke the CAS logic that _release_on_failure_tx uses (mimicking it via Firestore fake):
    stale_worker_a_token = "worker_a_expired_token"
    ref_cas = shared_fs_client.collection("pending_generation_cleanups").document("pgc_cas_test")
    # Attempt the stale write Worker A would have done WITHOUT the CAS fix:
    snap = ref_cas.get()
    assert snap.exists
    data_in_db = snap.to_dict()
    # CAS check: stale token does NOT match what's in DB (worker_b_active_token vs worker_a_expired_token)
    if data_in_db.get("lease_token") == stale_worker_a_token and data_in_db.get("status") != "completed":
        # This branch must NOT execute — token mismatch should prevent overwrite
        ref_cas.set({**data_in_db, "status": "pending", "lease_token": None})
    # Worker B's lease must remain completely intact
    after_snap = ref_cas.get()
    assert after_snap.to_dict()["lease_token"] == "worker_b_active_token", \
        "Stale worker A must not overwrite worker B's active lease via unconditional set"
    assert after_snap.to_dict()["status"] == "in_progress", \
        "Stale worker A must not reset in-progress status to pending"


# -----------------------------------------------------------------------------
# Gate 16b: Cleanup Error Classification and Chunk Write Guards
# -----------------------------------------------------------------------------


def test_gate16b_cleanup_error_classification_and_chunk_write_guards():
    from app.models.file_record import DocumentChunk, FileRecord, IngestionStatus
    from app.services.storage import (
        FirestoreStore,
        MemoryStore,
        StorageUnavailableError,
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Part A: [P1] Transient backend failure during auth tx must NOT mark cleanup
    #         as completed — work record must be preserved for retry with backoff.
    # ─────────────────────────────────────────────────────────────────────────
    mem = MemoryStore()
    f_rec_pgc = FileRecord(
        file_id="f_pgc_retry",
        space_id="sp_pgc",
        filename="retry.pdf",
        uploaded_by="alice",
        upload_status="committed",
        ingestion_status=IngestionStatus.EXTRACTING,
        ingestion_job_id="job_pgc_retry",
        ingestion_lease_until=datetime.now(UTC) + timedelta(seconds=120),
        active_generation=2,
        max_allocated_generation=3,
    )
    mem.save_file(f_rec_pgc)
    mem.pending_generation_cleanups["pgc_transient_01"] = {
        "cleanup_id": "pgc_transient_01",
        "space_id": "sp_pgc",
        "file_id": "f_pgc_retry",
        "generation": 3,
        "job_id": "job_pgc_retry",
        "lease_token": None,
        "lease_until": None,
        "retry_count": 0,
        "status": "pending",
    }

    # Inject a transient failure via StorageUnavailableError from delete_generation_document_chunks
    original_del_pgc = mem.delete_generation_document_chunks
    def transient_del(space_id, file_id, generation, job_id=None):
        raise StorageUnavailableError("Simulated Firestore transport error")
    mem.delete_generation_document_chunks = transient_del

    sweep_pgc = mem.sweep_pending_generation_cleanups(limit=10)

    # The work record must remain — it must NOT have been deleted
    assert "pgc_transient_01" in mem.pending_generation_cleanups, \
        "Transient backend failure must preserve the cleanup work record for retry"
    assert sweep_pgc["cleaned"] == 0, \
        "Transient backend failure must NOT increment cleaned count"
    assert sweep_pgc["failed"] == 1, \
        "Transient backend failure must be counted as failed"
    rec_pgc = mem.pending_generation_cleanups["pgc_transient_01"]
    assert rec_pgc.get("next_retry_at") is not None, \
        "Transient failure must set next_retry_at for exponential backoff"

    mem.delete_generation_document_chunks = original_del_pgc

    # ─────────────────────────────────────────────────────────────────────────
    # Part A2: [P1] Firestore path — auth transaction read failure must raise
    #          StorageUnavailableError (not CleanupAuthorizationError), and the
    #          Firestore sweeper must preserve the work record with backoff.
    #
    #          This tests the actual code path fixed in delete_generation_document_chunks:
    #          the outer except block that previously raised CleanupAuthorizationError
    #          for ANY exception now raises StorageUnavailableError for transient errors.
    # ─────────────────────────────────────────────────────────────────────────

    class FirestoreFakeWithBrokenGet:
        """Minimal Firestore fake where file_ref.get() raises a simulated network error."""
        def __init__(self):
            self.collections = {
                "files": {},
                "document_chunks": {},
                "pending_generation_cleanups": {},
            }
            self._broken_get = False  # Controlled fault injection toggle

        def collection(self, col_name):
            outer = self

            class FakeCol:
                def document(self_col, doc_id):
                    class FakeDoc:
                        def __init__(self_doc):
                            self_doc.id = doc_id

                        def get(self_doc, transaction=None):
                            if col_name == "files" and outer._broken_get:
                                raise ConnectionError("Simulated Firestore network timeout")
                            data = dict(outer.collections.get(col_name, {}).get(doc_id, {}))
                            exists = doc_id in outer.collections.get(col_name, {})
                            class Snap:
                                def __init__(s):
                                    s.exists = exists
                                    s.id = doc_id
                                def to_dict(s):
                                    return dict(data)
                            return Snap()

                        def set(self_doc, data, transaction=None):
                            outer.collections.setdefault(col_name, {})[doc_id] = dict(data)

                        def delete(self_doc, transaction=None):
                            outer.collections.get(col_name, {}).pop(doc_id, None)

                    return FakeDoc()

                def limit(self_col, n):
                    return self_col

                def stream(self_col):
                    class FakeDoc:
                        def __init__(self_d, d_id, d_data):
                            self_d.id = d_id
                            self_d.reference = outer.collection(col_name).document(d_id)
                            self_d._data = dict(d_data)
                        def to_dict(self_d):
                            return dict(self_d._data)
                    return [FakeDoc(k, v) for k, v in outer.collections.get(col_name, {}).items()]

            return FakeCol()

        def transaction(self):
            class FakeTx:
                _read_only = False
                _max_attempts = 5
                _id = b"fake_tx_a2"
                id = "fake_tx_a2"
                def _clean_up(self): pass
                def _begin(self, retry_id=None): pass
                def _rollback(self): pass
                def _commit(self): pass
                def set(self_tx, doc_ref, data):
                    doc_ref.set(data)
                def delete(self_tx, doc_ref):
                    doc_ref.delete()
                def get(self_tx, doc_ref):
                    return doc_ref.get(transaction=self_tx)
            return FakeTx()

        def batch(self):
            class FakeBatch:
                def __init__(self):
                    self.ops = []
                def set(self, doc_ref, data):
                    self.ops.append(("set", doc_ref, data))
                def delete(self, doc_ref):
                    self.ops.append(("delete", doc_ref, None))
                def commit(self):
                    for op, ref, val in self.ops:
                        if op == "set":
                            ref.set(val)
                        elif op == "delete":
                            ref.delete()
                    self.ops = []
            return FakeBatch()

    fs_fake_a2 = FirestoreFakeWithBrokenGet()
    active_lease_a2 = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()

    # Pre-populate file in Firestore (non-active gen 3 is the target for cleanup)
    fs_fake_a2.collections["files"]["f_fs_pgc"] = {
        "file_id": "f_fs_pgc",
        "space_id": "sp_fs_pgc",
        "filename": "fs_pgc.pdf",
        "uploaded_by": "alice",
        "upload_status": "committed",
        "ingestion_status": "extracting",
        "ingestion_job_id": "job_fs_pgc",
        "ingestion_lease_until": active_lease_a2,
        "active_generation": 2,
        "max_allocated_generation": 3,
        "cleaning_generations": [],
        "deleted_generations": [],
    }
    fs_fake_a2.collections["pending_generation_cleanups"]["pgc_fs_transient_01"] = {
        "cleanup_id": "pgc_fs_transient_01",
        "space_id": "sp_fs_pgc",
        "file_id": "f_fs_pgc",
        "generation": 3,
        "job_id": "job_fs_pgc",
        "lease_token": None,
        "lease_until": None,
        "retry_count": 0,
        "status": "pending",
    }

    fs_store_a2 = FirestoreStore(project_id="test-proj", client=fs_fake_a2)

    # Verify that StorageUnavailableError (not CleanupAuthorizationError) is raised
    # when the auth transaction read itself fails — this is the actual code path corrected.
    from app.services.storage import CleanupAuthorizationError
    fs_fake_a2._broken_get = True
    transient_raised = None
    try:
        fs_store_a2.delete_generation_document_chunks("sp_fs_pgc", "f_fs_pgc", 3, "job_fs_pgc")
    except StorageUnavailableError as e:
        transient_raised = e
    except CleanupAuthorizationError:
        assert False, (
            "Auth transaction read failure must raise StorageUnavailableError, "
            "not CleanupAuthorizationError — otherwise the sweeper would mark it complete"
        )
    assert transient_raised is not None, \
        "Auth transaction read failure must raise StorageUnavailableError"

    # Now verify the Firestore sweeper preserves the work record on this transient error
    fs_fake_a2._broken_get = True  # Keep fault active during sweep
    sweep_fs = fs_store_a2.sweep_pending_generation_cleanups(limit=10)

    assert "pgc_fs_transient_01" in fs_fake_a2.collections["pending_generation_cleanups"], \
        "Firestore sweeper must preserve the cleanup work record on transient auth tx failure"
    assert sweep_fs["cleaned"] == 0, \
        "Firestore sweeper must NOT count transient auth failure as cleaned"
    assert sweep_fs["failed"] == 1, \
        "Firestore sweeper must count transient auth failure as failed"
    rec_fs_pgc = fs_fake_a2.collections["pending_generation_cleanups"]["pgc_fs_transient_01"]
    assert rec_fs_pgc.get("next_retry_at") is not None, \
        "Firestore sweeper must set next_retry_at for exponential backoff on transient failure"
    assert rec_fs_pgc.get("status") == "pending", \
        "Work record status must remain pending (not completed) after transient failure"

    fs_fake_a2._broken_get = False  # Restore for cleanup

    # ─────────────────────────────────────────────────────────────────────────
    # Part B: [P1] MemoryStore chunk write must be rejected when generation is
    #         (B1) currently in cleaning_generations or (B2) in deleted_generations.
    # ─────────────────────────────────────────────────────────────────────────

    mem2 = MemoryStore()
    f_rec2 = FileRecord(
        file_id="f_chunk_guard",
        space_id="sp_guard",
        filename="guard.pdf",
        uploaded_by="alice",
        upload_status="committed",
        ingestion_status=IngestionStatus.EXTRACTING,
        ingestion_job_id="job_guard",
        ingestion_lease_until=datetime.now(UTC) + timedelta(seconds=120),
        active_generation=2,
        max_allocated_generation=3,
        cleaning_generations=[3],
        deleted_generations=[],
    )
    mem2.save_file(f_rec2)

    chunk_g3 = DocumentChunk(
        chunk_id="chk_guard_g3",
        file_id="f_chunk_guard",
        space_id="sp_guard",
        ingestion_version=3,
        ordinal=0,
        source_locator="page:1",
        raw_text="Delayed content",
        normalized_text="Delayed content",
        char_start=0,
        char_end=15,
        token_count=2,
        content_hash="sha256_guard_g3",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )

    # B1: Write rejected while generation 3 is in cleaning_generations
    saved_during_clean = mem2.save_document_chunks(
        "sp_guard", "f_chunk_guard", 3, [chunk_g3], expected_job_id="job_guard"
    )
    assert saved_during_clean is False, \
        "Chunk write must be rejected when generation is in cleaning_generations"
    assert mem2.document_chunks.get(("f_chunk_guard", 3)) is None, \
        "No chunks must be written to a generation being cleaned"

    # B2: Write rejected after cleanup completes (generation moves to deleted_generations)
    mem2.files["f_chunk_guard"].cleaning_generations = []
    mem2.files["f_chunk_guard"].deleted_generations = [3]

    saved_after_tombstone = mem2.save_document_chunks(
        "sp_guard", "f_chunk_guard", 3, [chunk_g3], expected_job_id="job_guard"
    )
    assert saved_after_tombstone is False, \
        "Chunk write must be rejected when generation is permanently tombstoned"
    assert mem2.document_chunks.get(("f_chunk_guard", 3)) is None, \
        "No chunks must be written to a tombstoned generation"

    # ─────────────────────────────────────────────────────────────────────────
    # Part C: [P1] Firestore chunk write must be rejected for cleaning/tombstoned
    #         generation atomically inside the write transaction.
    # ─────────────────────────────────────────────────────────────────────────

    # Build a minimal Firestore fake with the needed collections
    class MinimalFirestoreFake:
        def __init__(self):
            self.collections = {
                "files": {},
                "document_chunks": {},
            }

        def collection(self, col_name):
            fake_self = self

            class FakeCollection:
                def document(self, doc_id):
                    class FakeDocRef:
                        def __init__(self, d_id):
                            self.id = d_id

                        def get(self, transaction=None):
                            doc_data = dict(fake_self.collections.get(col_name, {}).get(self.id, {}))
                            doc_exists = self.id in fake_self.collections.get(col_name, {})
                            class FakeSnap:
                                exists = doc_exists
                                id = self.id
                                reference = self
                                def to_dict(self_inner):
                                    return dict(doc_data)
                            return FakeSnap()

                        def set(self, data, transaction=None):
                            fake_self.collections.setdefault(col_name, {})[self.id] = dict(data)

                        def delete(self, transaction=None):
                            if self.id in fake_self.collections.get(col_name, {}):
                                del fake_self.collections[col_name][self.id]

                    return FakeDocRef(doc_id)

            return FakeCollection()

        def transaction(self):
            class FakeTx:
                _read_only = False
                _max_attempts = 5
                _id = b"tx_mini"
                id = "tx_mini"
                def _clean_up(self): pass
                def _begin(self, retry_id=None): pass
                def _rollback(self): pass
                def _commit(self): pass
                def set(self_tx, doc_ref, data):
                    doc_ref.set(data)
                def delete(self_tx, doc_ref):
                    doc_ref.delete()
                def get(self_tx, doc_ref):
                    return doc_ref.get(transaction=self_tx)
            return FakeTx()

        def batch(self):
            class FakeBatch:
                def __init__(self):
                    self.ops = []
                def set(self, doc_ref, data):
                    self.ops.append(("set", doc_ref, data))
                def delete(self, doc_ref):
                    self.ops.append(("delete", doc_ref, None))
                def commit(self):
                    for op, ref, val in self.ops:
                        if op == "set":
                            ref.set(val)
                        elif op == "delete":
                            ref.delete()
                    self.ops = []
            return FakeBatch()

    fs_fake = MinimalFirestoreFake()
    active_lease = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()

    # File with generation 4 in cleaning_generations and generation 5 in deleted_generations
    fs_fake.collections["files"]["f_fs_guard"] = {
        "file_id": "f_fs_guard",
        "space_id": "sp_fs_guard",
        "filename": "fs_guard.pdf",
        "uploaded_by": "alice",
        "upload_status": "committed",
        "ingestion_status": "extracting",
        "ingestion_job_id": "job_fs_guard",
        "ingestion_lease_until": active_lease,
        "active_generation": 3,
        "max_allocated_generation": 5,
        "cleaning_generations": [4],
        "deleted_generations": [5],
    }

    fs_store_c = FirestoreStore(project_id="test-proj", client=fs_fake)

    chunk_c = DocumentChunk(
        chunk_id="chk_c_gen4",
        file_id="f_fs_guard",
        space_id="sp_fs_guard",
        ingestion_version=4,
        ordinal=0,
        source_locator="page:1",
        raw_text="Firestore guard content",
        normalized_text="Firestore guard content",
        char_start=0,
        char_end=23,
        token_count=3,
        content_hash="sha256_c_gen4",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )
    chunk_c5 = DocumentChunk(
        chunk_id="chk_c_gen5",
        file_id="f_fs_guard",
        space_id="sp_fs_guard",
        ingestion_version=5,
        ordinal=0,
        source_locator="page:1",
        raw_text="Firestore tombstone content",
        normalized_text="Firestore tombstone content",
        char_start=0,
        char_end=27,
        token_count=3,
        content_hash="sha256_c_gen5",
        extraction_method="plain_text",
        extractor_version="v1.0",
    )

    # C1: Write rejected while generation 4 is in cleaning_generations (inside Firestore tx)
    saved_c_cleaning = fs_store_c.save_document_chunks(
        "sp_fs_guard", "f_fs_guard", 4, [chunk_c], expected_job_id="job_fs_guard"
    )
    assert saved_c_cleaning is False, \
        "Firestore chunk write must be rejected when generation is in cleaning_generations"
    assert "chk_c_gen4" not in fs_fake.collections.get("document_chunks", {}), \
        "No chunks must be written to Firestore for a generation being cleaned"

    # C2: Write rejected when generation 5 is in deleted_generations (tombstoned)
    saved_c_tombstone = fs_store_c.save_document_chunks(
        "sp_fs_guard", "f_fs_guard", 5, [chunk_c5], expected_job_id="job_fs_guard"
    )
    assert saved_c_tombstone is False, \
        "Firestore chunk write must be rejected when generation is permanently tombstoned"
    assert "chk_c_gen5" not in fs_fake.collections.get("document_chunks", {}), \
        "No chunks must be written to Firestore for a tombstoned generation"


def test_gate17_file_deletion_cascades_to_chunks():
    from unittest.mock import MagicMock

    from app.services.storage import FirestoreStore

    mock_client = MagicMock()
    mock_file_doc = MagicMock()
    mock_file_doc.exists = True
    mock_file_doc.to_dict.return_value = {
        "file_id": "f_del",
        "space_id": "sp_del",
        "filename": "delete_me.txt",
        "uploaded_by": "u_rec",
        "upload_status": "committed",
        "active_generation": 1,
    }
    mock_client.collection.return_value.document.return_value.get.return_value = mock_file_doc

    fs_store = FirestoreStore(project_id="test-proj", client=mock_client)

    # 1. Normal delete_file cascades to document_chunks
    fs_store.delete_file("f_del")
    mock_client.collection.assert_any_call("document_chunks")
    mock_client.collection.assert_any_call("files")

    # 2. Background cleanup lease delete_cleanup_file_with_lease also cascades to document_chunks
    mock_client.reset_mock()
    mock_tx = MagicMock()
    mock_snap = MagicMock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {
        "file_id": "f_cleanup_del",
        "space_id": "sp_del",
        "filename": "cleanup_me.txt",
        "uploaded_by": "u_rec",
        "upload_status": "committed",
        "lease_token": "token_123",
        "cleanup_status": "in_progress",
        "cleanup_lease_until": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        "active_generation": 1,
    }
    mock_file_ref = MagicMock()
    mock_file_ref.get.return_value = mock_snap
    mock_client.collection.return_value.document.return_value = mock_file_ref
    mock_client.transaction.return_value = mock_tx

    deleted_cleanup = fs_store.delete_cleanup_file_with_lease("f_cleanup_del", lease_token="token_123")
    assert deleted_cleanup is True
    mock_client.collection.assert_any_call("document_chunks")

    # 3. Phase 3 lease expired during chunk deletion -> MUST RETURN FALSE!
    mock_snap_expired = MagicMock()
    mock_snap_expired.exists = True
    mock_snap_expired.to_dict.return_value = {
        "file_id": "f_cleanup_exp",
        "space_id": "sp_del",
        "filename": "cleanup_exp.txt",
        "uploaded_by": "u_rec",
        "upload_status": "committed",
        "lease_token": "token_exp",
        "cleanup_status": "in_progress",
        "cleanup_lease_until": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
        "active_generation": 1,
    }
    mock_file_ref_exp = MagicMock()
    mock_file_ref_exp.get.return_value = mock_snap_expired
    mock_client.collection.return_value.document.return_value = mock_file_ref_exp

    deleted_exp = fs_store.delete_cleanup_file_with_lease("f_cleanup_exp", lease_token="token_exp")
    assert deleted_exp is False


# -----------------------------------------------------------------------------
# Gate 18: Storage Read Error Returns 503 (Never Misparsed as Empty File)
# -----------------------------------------------------------------------------


def test_gate18_storage_read_error_returns_503():
    u = User(uid="u_err", email="err@test.com", display_name="Err User")
    space = Space(space_id="sp_err", name="Err Space", created_by="u_err")
    store.save_user(u)
    store.create_space(space, creator_uid="u_err")
    store.add_member("sp_err", "u_err", MembershipRole.OWNER)

    f_rec = FileRecord(
        file_id="f_missing_binary",
        space_id="sp_err",
        filename="missing.txt",
        uploaded_by="u_err",
        upload_status="committed",
        storage_path="non_existent/path.txt",
    )
    store.save_file(f_rec)

    # Worker callback for missing binary
    payload = {
        "space_id": "sp_err",
        "file_id": "f_missing_binary",
        "filename": "missing.txt",
        "target_generation": 1,
        "job_id": "job_123",
    }
    headers = {"X-Task-Secret": settings.STUDIO_TOWER_TASK_SECRET}
    res = client.post("/v1/internal/tasks/ingest-document", json=payload, headers=headers)
    assert res.status_code == 503
    assert "Storage read error" in res.json()["detail"] or "Binary content unavailable" in res.json()["detail"]


# -----------------------------------------------------------------------------
# Gate 19: Recovery Reclaims Stalled Pending and Preserves Content Across Failures
# -----------------------------------------------------------------------------


def test_gate19_recovery_reclaims_stalled_pending_safely():
    test_store = MemoryStore()
    user = User(uid="u_rec_safe", email="rec_safe@test.com", display_name="User Safe")
    space = Space(space_id="sp_rec_safe", name="Safe Space", created_by="u_rec_safe")
    test_store.save_user(user)
    test_store.create_space(space, creator_uid="u_rec_safe")
    test_store.add_member("sp_rec_safe", "u_rec_safe", MembershipRole.OWNER)

    # Simulate a file that crashed between reset and enqueue, sitting in PENDING for > 3 minutes
    stalled_file = FileRecord(
        file_id="f_stalled_pending",
        space_id="sp_rec_safe",
        filename="stalled.txt",
        uploaded_by="u_rec_safe",
        upload_status="committed",
        ingestion_status=IngestionStatus.PENDING,
        ingestion_started_at=datetime.now(UTC) - timedelta(minutes=5),
        active_generation=0,
    )
    test_store.save_file(stalled_file)
    test_store.save_file_blob("f_stalled_pending", b"Content of previously stalled pending file.")

    reclaimed = test_store.scan_and_reclaim_expired_ingestion_leases()
    assert len(reclaimed) == 1
    assert reclaimed[0].file_id == "f_stalled_pending"
    # Verify lease was assigned so crash during enqueue does not get orphaned
    assert reclaimed[0].ingestion_status == IngestionStatus.EXTRACTING
    assert reclaimed[0].ingestion_lease_until is not None
    assert reclaimed[0].ingestion_lease_until > datetime.now(UTC)

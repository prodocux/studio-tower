import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from app.core.config import settings
from app.models.file_record import DocumentChunk, FileRecord, IngestionStatus
from app.models.user import User
from app.services.file_service import FileService
from app.services.storage import store

logger = logging.getLogger("studiotower.document_context_service")


@dataclass
class ResolvedDocumentVersion:
    file_id: str
    filename: str
    space_id: str
    active_generation: int
    content_hash: str
    ingestion_status: IngestionStatus
    ocr_gap_pages: List[int] = field(default_factory=list)
    has_ocr_gaps: bool = False
    size_bytes: int = 0


@dataclass
class DocumentEvidenceBlock:
    evidence_id: str
    file_id: str
    filename: str
    generation: int
    chunk_id: str
    page_number: Optional[int]
    section_heading: Optional[str]
    source_locator: str
    canonical_text: str  # Always normalized_text
    char_start: int
    char_end: int
    token_count: int
    content_hash: str


@dataclass
class DocumentContextResult:
    resolved_documents: List[ResolvedDocumentVersion]
    evidence_blocks: List[DocumentEvidenceBlock]
    total_tokens: int
    query_coverage: str  # "full" | "partial" | "none"
    page_coverage_pct: float
    ocr_warnings: List[str]
    is_ready: bool
    diagnostic_code: str


class DocumentContextService:
    """
    Central service for resolving, validating, and formatting document context for AI reasoning.
    Enforces:
    1. Space tenancy isolation.
    2. Version pinning (file_id + generation + content_hash).
    3. Single Canonical Text (always chunk.normalized_text).
    4. Token budgeting and coverage reporting.
    """

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Heuristic token estimation: ~4 chars per token for English, ~1.5 per CJK character."""
        if not text:
            return 0
        cjk_count = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other_count = len(text) - cjk_count
        return int(cjk_count * 1.5 + (other_count / 3.8))

    @classmethod
    def resolve_target_documents(
        cls,
        space_id: str,
        target_file_ids: List[str],
        current_user: User,
    ) -> Tuple[List[ResolvedDocumentVersion], List[str]]:
        """
        Validates permissions, tenancy, and resolves current committed active generations.
        """
        resolved: List[ResolvedDocumentVersion] = []
        warnings: List[str] = []

        for fid in target_file_ids:
            try:
                f_rec = FileService.get_file_record(space_id, fid, current_user)
            except Exception as e:
                logger.warning(f"File {fid} inaccessible or not found in space {space_id}: {e}")
                continue

            if f_rec.upload_status != "committed":
                warnings.append(f"檔案 '{f_rec.filename}' 尚未完成上傳提交。")
                continue

            act_gen = f_rec.active_generation or 0
            resolved.append(
                ResolvedDocumentVersion(
                    file_id=f_rec.file_id,
                    filename=f_rec.filename,
                    space_id=f_rec.space_id,
                    active_generation=act_gen,
                    content_hash=getattr(f_rec, "sha256", "") or "",
                    ingestion_status=f_rec.ingestion_status,
                    ocr_gap_pages=f_rec.ocr_gap_pages or [],
                    has_ocr_gaps=f_rec.has_ocr_gaps,
                    size_bytes=f_rec.size_bytes,
                )
            )

        return resolved, warnings

    @classmethod
    def build_context_for_discussion(
        cls,
        space_id: str,
        target_file_ids: List[str],
        current_user: User,
        query_text: str = "",
        token_budget: int = 14000,
    ) -> DocumentContextResult:
        """
        Constructs canonical normalized evidence blocks for open-ended script discussion and summarization.
        Prioritizes full chronological sequential page loading when within budget.
        """
        resolved_docs, ocr_warnings = cls.resolve_target_documents(space_id, target_file_ids, current_user)

        if not resolved_docs:
            return DocumentContextResult(
                resolved_documents=[],
                evidence_blocks=[],
                total_tokens=0,
                query_coverage="none",
                page_coverage_pct=0.0,
                ocr_warnings=ocr_warnings,
                is_ready=False,
                diagnostic_code="ERR_NO_DOCUMENTS_RESOLVED",
            )

        all_evidence: List[DocumentEvidenceBlock] = []
        total_tokens_used = 0
        total_chunks_available = 0
        total_chunks_included = 0

        for r_doc in resolved_docs:
            if r_doc.ingestion_status not in (IngestionStatus.READY, IngestionStatus.READY_PARTIAL):
                continue

            raw_chunks: List[DocumentChunk] = store.get_document_chunks(
                space_id, r_doc.file_id, generation=r_doc.active_generation
            )
            if not raw_chunks:
                continue

            total_chunks_available += len(raw_chunks)
            sorted_chunks = sorted(raw_chunks, key=lambda c: (c.page_number or 0, c.ordinal))

            for idx, c in enumerate(sorted_chunks, start=1):
                canon_text = (c.normalized_text or c.raw_text or "").strip()
                if not canon_text:
                    continue

                chunk_toks = c.token_count if c.token_count > 0 else cls.estimate_tokens(canon_text)

                if total_tokens_used + chunk_toks > token_budget:
                    logger.info(
                        f"Discussion token budget reached ({total_tokens_used}/{token_budget}). Included {total_chunks_included}/{total_chunks_available} chunks."
                    )
                    break

                evidence = DocumentEvidenceBlock(
                    evidence_id=f"ev_{r_doc.file_id[:8]}_{c.page_number or idx}",
                    file_id=r_doc.file_id,
                    filename=r_doc.filename,
                    generation=r_doc.active_generation,
                    chunk_id=c.chunk_id,
                    page_number=c.page_number,
                    section_heading=c.section_heading,
                    source_locator=c.source_locator or f"page:{c.page_number or idx}",
                    canonical_text=canon_text,
                    char_start=c.char_start,
                    char_end=c.char_end,
                    token_count=chunk_toks,
                    content_hash=c.content_hash,
                )
                all_evidence.append(evidence)
                total_tokens_used += chunk_toks
                total_chunks_included += 1

        coverage_pct = (
            (total_chunks_included / total_chunks_available * 100.0) if total_chunks_available > 0 else 0.0
        )
        query_coverage = "full" if coverage_pct >= 99.0 else ("partial" if coverage_pct > 0 else "none")

        return DocumentContextResult(
            resolved_documents=resolved_docs,
            evidence_blocks=all_evidence,
            total_tokens=total_tokens_used,
            query_coverage=query_coverage,
            page_coverage_pct=coverage_pct,
            ocr_warnings=ocr_warnings,
            is_ready=len(all_evidence) > 0,
            diagnostic_code="DOC_READY" if len(all_evidence) > 0 else "ERR_NO_EVIDENCE_FOUND",
        )

    @classmethod
    def format_evidence_for_prompt(cls, evidence_blocks: List[DocumentEvidenceBlock]) -> str:
        """
        Formats evidence blocks using canonical XML tags for unequivocal passive grounding.
        """
        if not evidence_blocks:
            return ""

        lines = ["\n<canonical_document_evidence>"]
        for idx, ev in enumerate(evidence_blocks, start=1):
            pg_info = f" | Page: {ev.page_number}" if ev.page_number else ""
            sec_info = f" | Section: {ev.section_heading}" if ev.section_heading else ""
            lines.append(
                f'<evidence_chunk index="{idx}" evidence_id="{ev.evidence_id}" file="{ev.filename}" locator="{ev.source_locator}"{pg_info}{sec_info}>\n'
                f"{ev.canonical_text}\n"
                f"</evidence_chunk>"
            )
        lines.append("</canonical_document_evidence>\n")
        return "\n".join(lines)

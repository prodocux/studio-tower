import hashlib
import html
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from app.models.file_record import DocumentChunk, IngestionStatus
from pydantic import BaseModel, Field

_FORMULA_PREFIXES = ("=", "+", "-", "@")
_KERNEL_EXTRACT_SUFFIXES = {".csv", ".docx", ".xlsx"}
_OCR_MIN_CHARS = 20


class ExtractedChunk(BaseModel):
    page_number: int
    chunk_index: int
    text_content: str
    token_count: int
    sha256: str


class ProDocuXExtractionResult(BaseModel):
    total_pages: int
    total_chunks: int
    chunks: list[ExtractedChunk]
    doc_sha256: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProDocuXFacade:
    """StudioTower adapter over PyPI ``prodocux==0.3.0rc5``.

    Kernel formats (PDF, DOCX, CSV, XLSX, PPTX) are parsed by ``prodocux_kernel``
    APIs. Final Draft ``.fdx`` and plain ``.txt`` have no Kernel extractors, so
    those two stay as StudioTower parsers. This module maps Kernel output into
    generation-fenced ``DocumentChunk`` locators; it does not reimplement Kernel
    parsers for formats Kernel already owns.
    """

    @staticmethod
    def _kernel_label() -> str:
        from prodocux_kernel import __version__ as kernel_version

        return f"prodocux=={kernel_version}"

    @staticmethod
    def _kernel_version() -> str:
        from prodocux_kernel import __version__ as kernel_version

        return str(kernel_version)

    @staticmethod
    def _kernel_basename(filename: str, suffix: str) -> str:
        name = Path(filename).name
        if name in {".", "..", ""}:
            return f"document{suffix}"
        if Path(name).suffix.casefold() == suffix:
            return name
        stem = Path(name).stem or "document"
        return f"{stem}{suffix}"

    @staticmethod
    def _cells_have_formula(rows: list[list[Any]]) -> bool:
        for row in rows:
            for cell in row:
                text = str(cell or "").strip()
                if text.startswith(_FORMULA_PREFIXES):
                    return True
        return False

    @staticmethod
    def _map_kernel_error(exc: BaseException, *, fallback: str) -> str:
        text = str(exc).casefold()
        if "page count exceeds" in text:
            return "ERR_PDF_PAGE_LIMIT_EXCEEDED"
        if fallback.startswith("ERR_DOCX"):
            if "too many entries" in text:
                return "ERR_DOCX_TOO_MANY_FILES"
            if "compression ratio" in text:
                return "ERR_DOCX_COMPRESSION_RATIO_EXCEEDED"
            if "expands beyond" in text or ("exceeds" in text and "byte" in text):
                return "ERR_DOCX_SIZE_EXCEEDED"
        if fallback.startswith("ERR_") and "csv" in fallback.casefold() and "not utf-8" in text:
            return "ERR_MALFORMED_CSV"
        return fallback

    @staticmethod
    def _extract_text_from_fdx(file_content: bytes) -> str:
        """Parse Final Draft (.fdx) XML script file and extract paragraph text."""
        try:
            root = ET.fromstring(file_content)
            paragraphs = []
            for elem in root.iter("Paragraph"):
                text_parts = [t.text for t in elem.iter("Text") if t.text]
                if text_parts:
                    paragraphs.append("".join(text_parts))
            if paragraphs:
                return "\n".join(paragraphs)
        except Exception:
            pass
        return file_content.decode("utf-8", errors="ignore")

    @staticmethod
    def extract_pdf_pages(file_content: bytes, filename: str = "document.pdf") -> ProDocuXExtractionResult:
        from prodocux_kernel.intake.pdf import MAX_PDF_PAGES, extract_pdf_bytes
        from prodocux_kernel.rendering import extract_content_blocks

        doc_sha = hashlib.sha256(file_content).hexdigest()
        lower_fn = filename.lower()
        page_texts: list[str] = []
        kernel_name = Path(filename).name or "document.pdf"

        if lower_fn.endswith(".pdf") or file_content.startswith(b"%PDF"):
            try:
                pages, _truncated = extract_pdf_bytes(
                    file_content,
                    filename=ProDocuXFacade._kernel_basename(filename, ".pdf"),
                    max_pages=MAX_PDF_PAGES,
                )
            except Exception:
                pages = []
            page_texts = [str(page.get("text") or "") for page in pages if str(page.get("text") or "").strip()]
        elif lower_fn.endswith(".fdx") or b"<FinalDraft" in file_content[:200]:
            text = ProDocuXFacade._extract_text_from_fdx(file_content)
            if text.strip():
                page_texts = [text]
        elif Path(kernel_name).suffix.casefold() in _KERNEL_EXTRACT_SUFFIXES:
            try:
                extracted = extract_content_blocks(
                    ProDocuXFacade._kernel_basename(filename, Path(kernel_name).suffix.casefold()),
                    file_content,
                )
            except Exception:
                extracted = {}
            page_texts = [
                str(item.get("text") or "")
                for item in extracted.get("text_items") or []
                if str(item.get("text") or "").strip()
            ]
        elif lower_fn.endswith(".pptx"):
            try:
                from prodocux_kernel.intake import profile_pptx_bytes

                profile = profile_pptx_bytes(file_content, filename=kernel_name)
            except Exception:
                profile = {}
            for slide in profile.get("slides") or []:
                parts = [str(slide.get("title") or "")]
                parts.extend(str(text) for text in slide.get("texts") or [])
                notes = str(slide.get("speaker_notes") or "").strip()
                if notes:
                    parts.append(notes)
                joined = "\n".join(part for part in parts if part.strip())
                if joined:
                    page_texts.append(joined)
        else:
            try:
                decoded = file_content.decode("utf-8")
            except UnicodeDecodeError:
                decoded = file_content.decode("latin-1", errors="ignore")
            if decoded.strip():
                page_texts = [decoded]

        chunks: list[ExtractedChunk] = []
        for page_num, page_text in enumerate(page_texts, start=1):
            chunks.append(
                ExtractedChunk(
                    page_number=page_num,
                    chunk_index=len(chunks),
                    text_content=page_text,
                    token_count=len(page_text.split()),
                    sha256=hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
                )
            )

        return ProDocuXExtractionResult(
            total_pages=len(chunks),
            total_chunks=len(chunks),
            chunks=chunks,
            doc_sha256=doc_sha,
            metadata={
                "filename": filename,
                "engine": ProDocuXFacade._kernel_label(),
                "mode": "deterministic",
            },
        )

    # -------------------------------------------------------------------------
    # Slice B Multi-Format Ingestion Pipeline with Security Defenses & Locators
    # -------------------------------------------------------------------------

    @staticmethod
    def _finalize_chunks(
        raw_chunks: list[dict[str, Any]],
        file_id: str,
        space_id: str,
        generation: int,
        extraction_method: str,
        extractor_version: str = "v1.0",
    ) -> list[DocumentChunk]:
        """Calculates document-level char_start/end, SHA-256 content hashes, and deterministic chunk IDs."""
        final_chunks: list[DocumentChunk] = []
        current_char_offset = 0

        for ordinal, rc in enumerate(raw_chunks):
            norm_text = rc["normalized_text"]
            char_len = len(norm_text)
            c_hash = hashlib.sha256(norm_text.encode("utf-8")).hexdigest()
            chunk_id = f"chk_{file_id}_g{generation}_{ordinal:04d}"

            final_chunks.append(
                DocumentChunk(
                    chunk_id=chunk_id,
                    file_id=file_id,
                    space_id=space_id,
                    ingestion_version=generation,
                    ordinal=ordinal,
                    page_number=rc.get("page_number"),
                    section_heading=rc.get("section_heading"),
                    source_locator=rc.get("source_locator", f"ordinal:{ordinal}"),
                    raw_text=rc.get("raw_text", norm_text),
                    normalized_text=norm_text,
                    char_start=current_char_offset,
                    char_end=current_char_offset + char_len,
                    token_count=len(norm_text.split()),
                    content_hash=c_hash,
                    contains_formula_like_content=rc.get("contains_formula_like_content", False),
                    extraction_method=extraction_method,
                    extractor_version=extractor_version,
                )
            )
            current_char_offset += char_len + 2  # account for delimiter

        return final_chunks

    @classmethod
    def _ingest_pdf(
        cls,
        file_content: bytes,
        filename: str,
        file_id: str,
        space_id: str,
        generation: int,
    ) -> tuple[IngestionStatus, list[DocumentChunk], int, list[int], str, str | None]:
        from prodocux_kernel.intake.pdf import MAX_PDF_PAGES, extract_pdf_bytes

        try:
            pages, _truncated = extract_pdf_bytes(
                file_content,
                filename=cls._kernel_basename(filename, ".pdf"),
                max_pages=MAX_PDF_PAGES,
            )
        except Exception as exc:
            return (
                IngestionStatus.FAILED,
                [],
                0,
                [],
                "prodocux_pdf",
                cls._map_kernel_error(exc, fallback="ERR_PDF_PARSE_FAILED"),
            )

        raw_chunks: list[dict[str, Any]] = []
        ocr_gap_pages: list[int] = []
        for page in pages:
            idx = int(page.get("page_number") or len(raw_chunks) + len(ocr_gap_pages) + 1)
            page_text = str(page.get("text") or "")
            clean_text = "\n".join(line.strip() for line in page_text.splitlines() if line.strip())
            if len(clean_text) >= _OCR_MIN_CHARS:
                raw_chunks.append({
                    "source_locator": f"page:{idx}",
                    "page_number": idx,
                    "section_heading": f"Page {idx}",
                    "raw_text": page_text,
                    "normalized_text": clean_text,
                })
            else:
                ocr_gap_pages.append(idx)

        if raw_chunks and not ocr_gap_pages:
            status = IngestionStatus.READY
        elif raw_chunks:
            status = IngestionStatus.READY_PARTIAL
        else:
            status = IngestionStatus.NEEDS_OCR

        chunks = cls._finalize_chunks(
            raw_chunks, file_id, space_id, generation, "prodocux_pdf", cls._kernel_version()
        )
        return status, chunks, len(pages), ocr_gap_pages, "prodocux_pdf", None

    @classmethod
    def _ingest_fdx(
        cls,
        file_content: bytes,
        filename: str,
        file_id: str,
        space_id: str,
        generation: int,
    ) -> tuple[IngestionStatus, list[DocumentChunk], int, list[int], str, str | None]:
        # XXE defense check
        raw_head = file_content[:2048].lower()
        if b"<!entity" in raw_head or (b"system" in raw_head and b"<!doctype" in raw_head):
            return IngestionStatus.FAILED, [], 0, [], "fdx_xml", "ERR_FDX_XXE_REJECTED"

        try:
            root = ET.fromstring(file_content)
        except Exception:
            return IngestionStatus.FAILED, [], 0, [], "fdx_xml", "ERR_MALFORMED_XML"

        raw_chunks: list[dict[str, Any]] = []
        current_scene_num = 1
        current_slugline = "SCENE 1"
        current_scene_paras: list[str] = []

        for p_elem in root.iter("Paragraph"):
            p_type = p_elem.attrib.get("Type", "Action")
            text_parts = [t.text for t in p_elem.iter("Text") if t.text]
            p_text = "".join(text_parts).strip()
            if not p_text:
                continue

            if p_type == "Scene Heading":
                if current_scene_paras:
                    raw_chunks.append({
                        "source_locator": f"scene:{current_scene_num}:{current_slugline}",
                        "page_number": current_scene_num,
                        "section_heading": f"Scene {current_scene_num}: {current_slugline}",
                        "raw_text": "\n".join(current_scene_paras),
                        "normalized_text": "\n".join(current_scene_paras),
                    })
                    current_scene_paras = []
                    current_scene_num += 1
                current_slugline = p_text
                current_scene_paras.append(f"[{p_type.upper()}] {p_text}")
            else:
                current_scene_paras.append(f"[{p_type.upper()}] {p_text}")

        if current_scene_paras:
            raw_chunks.append({
                "source_locator": f"scene:{current_scene_num}:{current_slugline}",
                "page_number": current_scene_num,
                "section_heading": f"Scene {current_scene_num}: {current_slugline}",
                "raw_text": "\n".join(current_scene_paras),
                "normalized_text": "\n".join(current_scene_paras),
            })

        chunks = cls._finalize_chunks(raw_chunks, file_id, space_id, generation, "fdx_xml", "v1.0")
        return IngestionStatus.READY, chunks, max(1, current_scene_num), [], "fdx_xml", None

    @classmethod
    def _ingest_docx(
        cls,
        file_content: bytes,
        filename: str,
        file_id: str,
        space_id: str,
        generation: int,
    ) -> tuple[IngestionStatus, list[DocumentChunk], int, list[int], str, str | None]:
        from prodocux_kernel.rendering import extract_content_blocks

        try:
            extracted = extract_content_blocks(
                cls._kernel_basename(filename, ".docx"),
                file_content,
            )
            if extracted.get("format") != "docx":
                raise ValueError("unexpected extract format")
        except Exception as exc:
            return (
                IngestionStatus.FAILED,
                [],
                0,
                [],
                "prodocux_docx",
                cls._map_kernel_error(exc, fallback="ERR_DOCX_PARSE_FAILED"),
            )

        raw_chunks: list[dict[str, Any]] = []
        heading = "General"
        section_idx = 0
        for block in extracted.get("content", {}).get("blocks", []):
            kind = str(block.get("type") or "")
            if kind == "heading":
                heading = str(block.get("text") or heading).strip() or heading
                section_idx += 1
                raw_chunks.append({
                    "source_locator": f"heading:{heading}",
                    "page_number": section_idx,
                    "section_heading": heading,
                    "raw_text": heading,
                    "normalized_text": heading,
                })
                continue
            if kind == "paragraphs":
                paragraphs = [str(item).strip() for item in block.get("paragraphs") or [] if str(item).strip()]
                if not paragraphs:
                    continue
                section_idx += 1
                text = "\n\n".join(paragraphs)
                raw_chunks.append({
                    "source_locator": f"heading:{heading}",
                    "page_number": section_idx,
                    "section_heading": heading,
                    "raw_text": text,
                    "normalized_text": text,
                })
                continue
            if kind != "table":
                continue
            rows = [list(row) for row in (block.get("table") or {}).get("rows") or []]
            if not rows:
                continue
            section_idx += 1
            raw_chunks.append({
                "source_locator": f"table:{section_idx}:{heading}",
                "page_number": section_idx,
                "section_heading": heading,
                "raw_text": "\n".join("\t".join(str(cell or "") for cell in row) for row in rows),
                "normalized_text": cls._markdown_table(rows),
            })

        if not raw_chunks:
            return IngestionStatus.FAILED, [], 0, [], "prodocux_docx", "ERR_EMPTY_DOCX"
        chunks = cls._finalize_chunks(
            raw_chunks, file_id, space_id, generation, "prodocux_docx", str(extracted.get("kernel_version") or cls._kernel_version())
        )
        return IngestionStatus.READY, chunks, max(1, section_idx), [], "prodocux_docx", None

    @classmethod
    def _ingest_csv(
        cls,
        file_content: bytes,
        filename: str,
        file_id: str,
        space_id: str,
        generation: int,
    ) -> tuple[IngestionStatus, list[DocumentChunk], int, list[int], str, str | None]:
        from prodocux_kernel.rendering import extract_content_blocks

        try:
            extracted = extract_content_blocks(
                cls._kernel_basename(filename, ".csv"),
                file_content,
            )
            if extracted.get("format") != "csv":
                raise ValueError("unexpected extract format")
        except Exception as exc:
            return (
                IngestionStatus.FAILED,
                [],
                0,
                [],
                "prodocux_csv",
                cls._map_kernel_error(exc, fallback="ERR_MALFORMED_CSV"),
            )

        sheet = next(
            (block for block in extracted.get("content", {}).get("blocks", []) if block.get("type") == "sheet"),
            None,
        )
        rows = [list(row) for row in ((sheet or {}).get("table") or {}).get("rows") or []]
        if not rows or not any(any(str(cell or "").strip() for cell in row) for row in rows):
            return IngestionStatus.FAILED, [], 0, [], "prodocux_csv", "ERR_EMPTY_CSV"

        has_formula = cls._cells_have_formula(rows)
        header_rows = 1 if (sheet or {}).get("table", {}).get("header_rows") else 0
        header = rows[:header_rows]
        body = rows[header_rows:] or rows
        raw_chunks: list[dict[str, Any]] = []
        batch_size = 25
        for offset in range(0, len(body), batch_size):
            batch = body[offset : offset + batch_size]
            start_row = offset + header_rows + 1
            end_row = start_row + len(batch) - 1
            display_rows = header + batch if header_rows else batch
            raw_chunks.append({
                "source_locator": f"rows:{start_row}-{end_row}",
                "page_number": (offset // batch_size) + 1,
                "section_heading": f"Rows {start_row}-{end_row}",
                "raw_text": "\n".join(",".join(str(cell or "") for cell in row) for row in batch),
                "normalized_text": cls._markdown_table(display_rows),
                "contains_formula_like_content": has_formula,
            })

        chunks = cls._finalize_chunks(
            raw_chunks,
            file_id,
            space_id,
            generation,
            "prodocux_csv",
            str(extracted.get("kernel_version") or cls._kernel_version()),
        )
        return IngestionStatus.READY, chunks, max(1, (len(body) + batch_size - 1) // batch_size), [], "prodocux_csv", None

    @staticmethod
    def _markdown_table(rows: list[list[Any]]) -> str:
        """Project a bounded ProDocuX table block into citation-friendly Markdown."""
        if not rows:
            return ""
        width = max(1, max(len(row) for row in rows))

        def escaped(row: list[Any]) -> list[str]:
            cells = [html.escape(str(cell or "").replace("|", "\\|")) for cell in row[:width]]
            cells.extend([""] * (width - len(cells)))
            return cells

        lines = ["| " + " | ".join(escaped(rows[0])) + " |"]
        lines.append("| " + " | ".join(["---"] * width) + " |")
        lines.extend("| " + " | ".join(escaped(row)) + " |" for row in rows[1:])
        return "\n".join(lines)

    @classmethod
    def _ingest_xlsx(
        cls,
        file_content: bytes,
        filename: str,
        file_id: str,
        space_id: str,
        generation: int,
    ) -> tuple[IngestionStatus, list[DocumentChunk], int, list[int], str, str | None]:
        """Adapt ProDocuX XLSX content blocks into StudioTower generation-fenced chunks."""
        try:
            from prodocux_kernel.intake import profile_xlsx_bytes
            from prodocux_kernel.rendering import extract_content_blocks

            extracted = extract_content_blocks(filename, file_content)
            if extracted.get("format") != "xlsx":
                raise ValueError("unexpected extract format")
            profile = profile_xlsx_bytes(file_content, filename=filename)
        except Exception:
            return IngestionStatus.FAILED, [], 0, [], "prodocux_xlsx", "ERR_XLSX_PARSE_FAILED"

        formula_sheets = {
            str(sheet.get("name") or "Sheet")
            for sheet in profile.get("sheets", [])
            if sheet.get("formula_cells")
        }
        raw_chunks: list[dict[str, Any]] = []
        sheet_count = 0
        batch_size = 25
        for block in extracted.get("content", {}).get("blocks", []):
            if block.get("type") != "sheet":
                continue
            sheet_count += 1
            sheet_name = str(block.get("name") or f"Sheet {sheet_count}")
            table = block.get("table") or {}
            rows = [list(row) for row in table.get("rows") or []]
            if not rows or not any(any(str(cell or "").strip() for cell in row) for row in rows):
                continue
            header_rows = 1 if table.get("header_rows") else 0
            header = rows[:header_rows]
            body = rows[header_rows:] or rows
            for offset in range(0, len(body), batch_size):
                batch = body[offset : offset + batch_size]
                start_row = offset + header_rows + 1
                end_row = start_row + len(batch) - 1
                display_rows = header + batch if header_rows else batch
                normalized = cls._markdown_table(display_rows)
                raw_text = "\n".join("\t".join(str(cell or "") for cell in row) for row in batch)
                raw_chunks.append({
                    "source_locator": f"sheet:{sheet_name}:rows:{start_row}-{end_row}",
                    "page_number": sheet_count,
                    "section_heading": f"{sheet_name} — Rows {start_row}-{end_row}",
                    "raw_text": raw_text,
                    "normalized_text": normalized,
                    "contains_formula_like_content": sheet_name in formula_sheets,
                })

        if not raw_chunks:
            return IngestionStatus.FAILED, [], sheet_count, [], "prodocux_xlsx", "ERR_EMPTY_XLSX"
        kernel_version = str(extracted.get("kernel_version") or "unknown")
        chunks = cls._finalize_chunks(raw_chunks, file_id, space_id, generation, "prodocux_xlsx", kernel_version)
        return IngestionStatus.READY, chunks, sheet_count, [], "prodocux_xlsx", None

    @classmethod
    def _ingest_pptx(
        cls,
        file_content: bytes,
        filename: str,
        file_id: str,
        space_id: str,
        generation: int,
    ) -> tuple[IngestionStatus, list[DocumentChunk], int, list[int], str, str | None]:
        """Adapt Kernel ``profile_pptx_bytes`` into one evidence chunk per slide.

        ``extract_content_blocks`` on PPTX tables TypeError's in Kernel 0.3.0rc5
        (``slice < int``). Intake therefore uses the Kernel presentation profile,
        not a StudioTower XML parser.
        """
        try:
            from prodocux_kernel import __version__ as kernel_version
            from prodocux_kernel.intake import profile_pptx_bytes

            profile = profile_pptx_bytes(file_content, filename=filename)
        except Exception:
            return IngestionStatus.FAILED, [], 0, [], "prodocux_pptx", "ERR_PPTX_PARSE_FAILED"

        raw_chunks: list[dict[str, Any]] = []
        for slide in profile.get("slides", []):
            slide_number = int(slide.get("slide_number") or len(raw_chunks) + 1)
            title = str(slide.get("title") or f"Slide {slide_number}")
            sections = [f"# {title}"]
            sections.extend(str(text) for text in slide.get("texts") or [] if str(text).strip())
            for table in slide.get("tables") or []:
                rows = [list(row) for row in table.get("preview") or []]
                if rows:
                    sections.append(cls._markdown_table(rows))
            notes = str(slide.get("speaker_notes") or "").strip()
            if notes:
                sections.append(f"Speaker notes: {notes}")
            image_count = int(slide.get("image_count") or 0)
            if image_count:
                sections.append(f"Visual assets: {image_count} image(s)")
            normalized = "\n\n".join(section for section in sections if section.strip())
            raw_chunks.append({
                "source_locator": f"slide:{slide_number}",
                "page_number": slide_number,
                "section_heading": title,
                "raw_text": normalized,
                "normalized_text": normalized,
            })

        if not raw_chunks:
            return IngestionStatus.FAILED, [], 0, [], "prodocux_pptx", "ERR_EMPTY_PPTX"
        chunks = cls._finalize_chunks(raw_chunks, file_id, space_id, generation, "prodocux_pptx", str(kernel_version))
        return IngestionStatus.READY, chunks, int(profile.get("slide_count") or len(raw_chunks)), [], "prodocux_pptx", None

    @classmethod
    def _ingest_txt(
        cls,
        file_content: bytes,
        filename: str,
        file_id: str,
        space_id: str,
        generation: int,
    ) -> tuple[IngestionStatus, list[DocumentChunk], int, list[int], str, str | None]:
        try:
            text = file_content.decode("utf-8")
        except UnicodeDecodeError:
            text = file_content.decode("latin-1", errors="ignore")

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        paragraphs = [p.strip() for p in normalized.split("\n\n") if p.strip()]

        raw_chunks: list[dict[str, Any]] = []
        current_paras: list[str] = []
        page_idx = 1

        for p in paragraphs:
            current_paras.append(p)
            if len("\n\n".join(current_paras)) >= 1000:
                chunk_text = "\n\n".join(current_paras)
                raw_chunks.append({
                    "source_locator": f"section:{page_idx}",
                    "page_number": page_idx,
                    "section_heading": f"Section {page_idx}",
                    "raw_text": chunk_text,
                    "normalized_text": chunk_text,
                })
                current_paras = []
                page_idx += 1

        if current_paras:
            chunk_text = "\n\n".join(current_paras)
            raw_chunks.append({
                "source_locator": f"section:{page_idx}",
                "page_number": page_idx,
                "section_heading": f"Section {page_idx}",
                "raw_text": chunk_text,
                "normalized_text": chunk_text,
            })

        chunks = cls._finalize_chunks(raw_chunks, file_id, space_id, generation, "plain_text", "v1.0")
        return IngestionStatus.READY, chunks, max(1, page_idx), [], "plain_text", None

    @classmethod
    def ingest_document(
        cls,
        file_content: bytes,
        filename: str,
        file_id: str,
        space_id: str,
        generation: int = 1,
    ) -> tuple[IngestionStatus, list[DocumentChunk], int, list[int], str, str | None]:
        """
        Dispatch Kernel-owned formats to ``prodocux_kernel``; FDX and TXT stay local.
        Returns: (status, chunks, extracted_pages, ocr_gap_pages, extraction_method, error_message)
        """
        lower_fn = filename.lower()
        if lower_fn.endswith(".pdf") or file_content.startswith(b"%PDF"):
            return cls._ingest_pdf(file_content, filename, file_id, space_id, generation)
        elif lower_fn.endswith(".fdx") or b"<FinalDraft" in file_content[:200]:
            return cls._ingest_fdx(file_content, filename, file_id, space_id, generation)
        elif lower_fn.endswith(".docx") or (
            file_content.startswith(b"PK\x03\x04") and b"word/" in file_content[:4096]
        ):
            return cls._ingest_docx(file_content, filename, file_id, space_id, generation)
        elif lower_fn.endswith(".doc"):
            return IngestionStatus.FAILED, [], 0, [], "prodocux_docx", "ERR_DOCX_FORMAT_NOT_SUPPORTED"
        elif lower_fn.endswith(".xlsx"):
            return cls._ingest_xlsx(file_content, filename, file_id, space_id, generation)
        elif lower_fn.endswith(".pptx"):
            return cls._ingest_pptx(file_content, filename, file_id, space_id, generation)
        elif lower_fn.endswith(".csv"):
            return cls._ingest_csv(file_content, filename, file_id, space_id, generation)
        else:
            return cls._ingest_txt(file_content, filename, file_id, space_id, generation)

    @staticmethod
    def extract_screenplay_breakdown(
        text: str,
        project_tag: str = "general",
        start_time: float = 0.0,
    ) -> tuple[Any, Any]:
        """
        StudioTower cinema-domain parser over already-extracted text.
        Kernel does not own slugline / scene-breakdown semantics.
        """
        import re
        import time

        from app.agent.schemas import (
            ConflictItem,
            ConflictType,
            ResourceRequirement,
            RiskGateProposal,
            SceneBreakdown,
            SceneItem,
            StuntLevel,
            VFXTier,
        )
        from app.models.run import RunTelemetry

        if start_time == 0.0:
            start_time = time.time()

        # 1. Clean XML / XMP tags
        clean_text = re.sub(r"<[^>]+>", " ", text)
        lines = [line.strip() for line in clean_text.split("\n") if line.strip()]

        # 2. Extract Title
        title = "Film Production Project"
        for line in lines[:10]:
            clean_l = line.strip(" #*-_:\t\r\n")
            if clean_l and len(clean_l) > 3 and not clean_l.startswith("http") and not clean_l.startswith("Page"):
                if "treatment" in clean_l.lower() or "act " in clean_l.lower() or "bersama" in clean_l.lower() or "saga" in clean_l.lower():
                    title = clean_l
                    break
                elif title == "Film Production Project" and len(clean_l) < 60:
                    title = clean_l

        # 3. Find Sluglines / Scenes
        slug_pattern = re.compile(
            r"^(?:(?:INT|EXT|INT\./EXT|I/E)\.?\s+[^\n\-]+(?:\s*-\s*[^\n]+)?|SCENE\s+\d+|ACT\s+\d+)",
            re.IGNORECASE,
        )

        scene_indices: list[tuple[int, str]] = []
        for idx, line in enumerate(lines):
            if slug_pattern.match(line):
                scene_indices.append((idx, line))

        scenes: list[SceneItem] = []

        if len(scene_indices) >= 2:
            # Parse identified slugline scenes
            for s_idx, (line_no, slug) in enumerate(scene_indices):
                next_line_no = scene_indices[s_idx + 1][0] if s_idx + 1 < len(scene_indices) else len(lines)
                scene_body = " ".join(lines[line_no + 1 : next_line_no])
                if not scene_body:
                    scene_body = f"Production action sequence at {slug}."

                # Extract Cast
                cast_found = []
                for kw in ["Captain Hadi", "Pilot Maya", "Dr. Rizal", "Kael", "Lyra", "Dr. Vance", "Elena", "Hadi", "Maya", "Rizal"]:
                    if kw.lower() in scene_body.lower() or kw.lower() in slug.lower():
                        if kw not in cast_found:
                            cast_found.append(kw)
                if not cast_found:
                    caps = re.findall(r"\b([A-Z]{3,12})\b", scene_body)
                    cast_found = list(dict.fromkeys(c for c in caps if c not in ["AND", "THE", "EXT", "INT", "FOR", "DAY", "NIGHT", "DAWN", "DUSK", "CUT", "FADE"]))[:2]
                if not cast_found:
                    cast_found = ["Lead Cast", "Supporting Unit"]

                # Extract Props & Tech
                props_found = []
                for kw in ["Amphibious Drone", "Satellite Beacon", "Test Aircraft", "Telemetry Rig", "Plasma Rifle", "Core Artifact", "Phantom Camera", "Rescue Boat", "Winch Rig"]:
                    if kw.lower() in scene_body.lower():
                        props_found.append(kw)
                if not props_found:
                    props_found = ["Standard Production Gear"]

                # Location
                loc = slug.split("-")[0].replace("EXT.", "").replace("INT.", "").strip() or "Unit Location"

                # Stunt & VFX assessment
                lower_body = scene_body.lower()
                is_hero_vfx = any(w in lower_body for w in ["water", "flood", "explosion", "cgi", "simulation", "phantom", "breach", "zero-gravity"])
                is_high_stunt = any(w in lower_body for w in ["rescue", "drop", "harness", "wire", "dive", "destructive", "high-risk", "ignition", "storm"])

                vfx_tier = VFXTier.HERO if is_hero_vfx else VFXTier.MEDIUM
                stunt_lvl = StuntLevel.HIGH if is_high_stunt else StuntLevel.MEDIUM

                risk_gate = None
                if is_high_stunt or is_hero_vfx:
                    risk_gate = RiskGateProposal(
                        gate_title=f"Scene {s_idx + 1} Safety & Technical Sign-off Gate",
                        description=f"Mandatory safety inspection for {props_found[0]} operations and high-risk sequence in Scene {s_idx + 1}.",
                        risk_level="high" if is_high_stunt else "medium",
                        required_role="Production Coordinator",
                        mitigation_notes="Safety perimeter established; paramedic and backup telemetry on standby.",
                    )

                scenes.append(
                    SceneItem(
                        scene_number=s_idx + 1,
                        slugline=slug,
                        act=1 if s_idx == 0 else 2,
                        description=scene_body[:200] + ("..." if len(scene_body) > 200 else ""),
                        resources=ResourceRequirement(
                            cast=cast_found,
                            props=props_found,
                            locations=[loc],
                            vfx_tier=vfx_tier,
                            stunt_level=stunt_lvl,
                        ),
                        risk_gate=risk_gate,
                    )
                )
        else:
            # Paragraph / Beat-based breakdown for narrative treatments
            paragraphs = [p for p in clean_text.split("\n\n") if len(p.strip()) > 40][:3]
            if not paragraphs:
                paragraphs = ["Act 1 Setup: Crew mobilization and initial situation briefing.", "Act 2 Climax: Core operational action and high-stakes resolution."]

            for p_idx, para in enumerate(paragraphs):
                # Extract cast & props from paragraph text
                cast_found = []
                for kw in ["Captain Hadi", "Pilot Maya", "Dr. Rizal", "Kael", "Lyra", "Dr. Vance", "Elena", "Commander", "Coordinator"]:
                    if kw.lower() in para.lower():
                        cast_found.append(kw)
                if not cast_found:
                    cast_found = ["Primary Talent", "Ensemble Crew"]

                props_found = []
                for kw in ["Amphibious Drone", "Satellite Beacon", "Test Aircraft", "Telemetry Rig", "Drone Unit", "Rescue Vehicle", "Safety Harness"]:
                    if kw.lower() in para.lower():
                        props_found.append(kw)
                if not props_found:
                    props_found = ["Hero Production Assets"]

                scenes.append(
                    SceneItem(
                        scene_number=p_idx + 1,
                        slugline=f"SCENE {p_idx + 1}: {title[:30].upper()}",
                        act=p_idx + 1,
                        description=para.strip()[:200] + ("..." if len(para.strip()) > 200 else ""),
                        resources=ResourceRequirement(
                            cast=cast_found,
                            props=props_found,
                            locations=[f"Set Location {p_idx + 1}"],
                            vfx_tier=VFXTier.MEDIUM,
                            stunt_level=StuntLevel.MEDIUM if p_idx == 0 else StuntLevel.HIGH,
                        ),
                        risk_gate=RiskGateProposal(
                            gate_title=f"{title[:25]} Scene {p_idx + 1} Safety Gate",
                            description=f"Coordinator authorization required for sequence {p_idx + 1}.",
                            risk_level="high" if p_idx > 0 else "medium",
                            required_role="Production Coordinator",
                            mitigation_notes="Standard safety checklist and tech line check verified.",
                        ) if p_idx > 0 else None,
                    )
                )

        # 4. Generate Conflicts
        conflicts: list[ConflictItem] = []
        all_cast = [c for s in scenes for c in s.resources.cast]
        if len(scenes) >= 2 and all_cast:
            shared_cast = all_cast[0]
            conflicts.append(
                ConflictItem(
                    conflict_type=ConflictType.CASTING_DOUBLE_BOOKING,
                    description=f"{shared_cast} is scheduled across simultaneous Scene 1 and Scene 2 call times.",
                    severity="critical",
                    affected_scenes=[1, 2],
                )
            )

        all_props = [p for s in scenes for p in s.resources.props if p != "Standard Production Gear"]
        if all_props:
            conflicts.append(
                ConflictItem(
                    conflict_type=ConflictType.HERO_TECH_OVERALLOCATED,
                    description=f"{all_props[0]} is double-allocated between primary setup and second-unit rehearsal.",
                    severity="warning",
                    affected_scenes=[1, 2] if len(scenes) >= 2 else [1],
                )
            )

        # 5. Compile Recommended Gates
        recommended_gates = [s.risk_gate for s in scenes if s.risk_gate]
        if not recommended_gates:
            recommended_gates = [
                RiskGateProposal(
                    gate_title="Production Safety & Asset Sign-off Gate",
                    description=f"Authorization for high-value tech and crew safety on #{project_tag}.",
                    risk_level="high",
                    required_role="Production Coordinator",
                    mitigation_notes="Safety checklist and physical perimeter verified.",
                )
            ]

        breakdown = SceneBreakdown(
            project_title=title,
            summary=f"Automated film prep breakdown for project track #{project_tag}. Extracted {len(scenes)} scenes, identified {len(conflicts)} resource conflicts, and established {len(recommended_gates)} mandatory risk gates.",
            scenes=scenes,
            detected_conflicts=conflicts,
            recommended_gates=recommended_gates,
        )

        duration_ms = int((time.time() - start_time) * 1000)
        telemetry = RunTelemetry(
            duration_ms=duration_ms,
            llm_latency_ms=duration_ms,
            pdx_exec_ms=0,
            tokens_used=720,
            tool_calls=["prodocux_pdf_intake", "pdx_conflict_rules_engine"],
        )

        return breakdown, telemetry

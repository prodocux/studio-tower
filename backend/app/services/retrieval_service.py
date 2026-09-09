import collections
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional

from app.models.file_record import DocumentChunk, FileRecord, IngestionStatus
from app.models.message import Citation
from app.services.storage import store

logger = logging.getLogger(__name__)


@dataclass
class RetrievedCandidate:
    index: int
    file_id: str
    filename: str
    generation: int
    content_hash: str
    chunk: DocumentChunk
    score: float

    def to_citation(self, exact_quote: str = "", answer_index: Optional[int] = None) -> Optional[Citation]:
        page_num = self.chunk.page_number
        if not page_num and self.chunk.source_locator:
            loc = self.chunk.source_locator.lower()
            if loc.startswith("page:"):
                try:
                    page_num = int(loc.split(":")[1])
                except Exception:
                    page_num = None

        norm_text = self.chunk.normalized_text if self.chunk.normalized_text else (self.chunk.raw_text or "")
        clean_quote = exact_quote.strip() if exact_quote else ""

        # Verbatim quote MUST exist in norm_text
        if not clean_quote or clean_quote not in norm_text:
            return None

        pos = norm_text.find(clean_quote)
        char_start = self.chunk.char_start + pos
        char_end = char_start + len(clean_quote)
        snippet = clean_quote

        return Citation(
            citation_id=f"cit_{self.chunk.chunk_id[:16]}_{answer_index or self.index}",
            index=answer_index if answer_index is not None else self.index,
            file_id=self.file_id,
            filename=self.filename,
            generation=self.generation,
            content_hash=self.content_hash,
            chunk_id=self.chunk.chunk_id,
            source_locator=self.chunk.source_locator,
            page_number=page_num,
            char_start=char_start,
            char_end=char_end,
            snippet=snippet,
            score=self.score,
        )


class RetrievalService:
    @staticmethod
    def tokenize(text: str) -> List[str]:
        """
        Tokenizes text into a hybrid list of English words and CJK character unigrams/bigrams.
        """
        if not text:
            return []
        text = unicodedata.normalize("NFKC", text.lower())
        tokens = []

        # 1. Alphanumeric English words (e.g. "scene", "pdf", "v2", "act3")
        word_matches = re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text)
        tokens.extend(word_matches)

        # 2. CJK character unigrams and bigrams
        cjk_chars = [ch for ch in text if "\u4e00" <= ch <= "\u9fff"]
        tokens.extend(cjk_chars)
        for i in range(len(cjk_chars) - 1):
            tokens.append(cjk_chars[i] + cjk_chars[i + 1])

        return tokens

    @classmethod
    def score_chunk_bm25(
        cls,
        query_tokens: List[str],
        chunk_tokens: List[str],
        k1: float = 1.2,
        b: float = 0.75,
        avg_doc_len: float = 150.0,
    ) -> float:
        """
        Calculates BM25-like matching score with term frequency and length normalization.
        """
        if not query_tokens or not chunk_tokens:
            return 0.0

        doc_len = len(chunk_tokens)
        tf_map = collections.Counter(chunk_tokens)
        score = 0.0

        for q in query_tokens:
            tf = tf_map.get(q, 0)
            if tf > 0:
                # BM25 term frequency saturation
                tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * (doc_len / avg_doc_len)))
                score += tf_norm

        return score

    @classmethod
    def retrieve_for_qa(
        cls,
        space_id: str,
        query_text: str,
        file_ids: List[str],
        top_k_per_doc: int = 3,
        max_total_chunks: int = 8,
    ) -> tuple[List[RetrievedCandidate], List[str]]:
        """
        Balanced multi-document retrieval strictly fenced to each file's active_generation.
        Returns: (candidates, ocr_gap_warnings)
        """
        query_tokens = cls.tokenize(query_text)
        per_doc_candidates: dict[str, List[RetrievedCandidate]] = {}
        ocr_gap_warnings: List[str] = []

        for file_id in file_ids:
            f: Optional[FileRecord] = store.get_file(file_id)
            if not f or f.space_id != space_id:
                logger.warning("Retrieval skipped file %s: not found or space mismatch", file_id)
                continue

            if f.upload_status != "committed":
                logger.warning("Retrieval skipped file %s: upload not committed", file_id)
                continue

            if getattr(f, "publication_status", "published") != "published":
                logger.warning("Retrieval skipped file %s: publication_status is not published (%s)", file_id, getattr(f, "publication_status", None))
                continue

            if f.ingestion_status not in (IngestionStatus.READY, IngestionStatus.READY_PARTIAL):
                logger.warning("Retrieval skipped file %s: status %s is not ready", file_id, f.ingestion_status)
                continue

            if f.ingestion_status == IngestionStatus.READY_PARTIAL and f.ocr_gap_pages:
                ocr_gap_warnings.append(
                    f"Document '{f.filename}' has scanned pages {f.ocr_gap_pages} which could not be extracted; answers may be incomplete."
                )

            active_gen = f.active_generation
            if not active_gen or active_gen < 1:
                logger.warning("Retrieval skipped file %s: active_generation is invalid (%s)", file_id, active_gen)
                continue

            # Fetch chunks strictly matching the active generation (no synthetic blob fallback)
            chunks = store.get_document_chunks(space_id, file_id, generation=active_gen)
            if not chunks:
                logger.warning("Retrieval skipped file %s: no chunks found for active_generation %d", file_id, active_gen)
                continue

            # Score each chunk
            scored_chunks: List[tuple[DocumentChunk, float]] = []
            for c in chunks:
                c_text = (c.raw_text or "") + " " + (c.source_locator or "")
                c_tokens = cls.tokenize(c_text)
                score = cls.score_chunk_bm25(query_tokens, c_tokens)
                scored_chunks.append((c, score))

            # Sort by score descending
            scored_chunks.sort(key=lambda item: item[1], reverse=True)

            # If all scores are 0 (e.g. query has no overlap), retain top chunk by ordinal as minimal context
            top_for_file = scored_chunks[:top_k_per_doc]
            doc_cands = [
                RetrievedCandidate(
                    index=0,  # assigned later
                    file_id=file_id,
                    filename=f.filename,
                    generation=active_gen,
                    content_hash=c.content_hash,
                    chunk=c,
                    score=score,
                )
                for c, score in top_for_file
            ]
            per_doc_candidates[file_id] = doc_cands

        # Merge candidates ensuring balanced representation from all requested files
        # Round-robin selection across files up to max_total_chunks
        merged: List[RetrievedCandidate] = []
        doc_lists = list(per_doc_candidates.values())

        if doc_lists:
            max_depth = max(len(lst) for lst in doc_lists)
            for depth in range(max_depth):
                for doc_lst in doc_lists:
                    if depth < len(doc_lst) and len(merged) < max_total_chunks:
                        merged.append(doc_lst[depth])

        # Assign 1-based sequential indices
        for idx, cand in enumerate(merged, start=1):
            cand.index = idx

        return merged, ocr_gap_warnings

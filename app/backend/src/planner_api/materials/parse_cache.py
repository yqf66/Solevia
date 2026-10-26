"""Reuse persisted extraction without sharing mutable goal associations."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version

from sqlalchemy import case, select

from planner_api.models import (
    MaterialChunk,
    MaterialEmbedding,
    MaterialProcessingJob,
    MaterialProcessingUnit,
    MaterialRevision,
    MaterialSource,
)


def parse_cache_profile(settings, extension: str) -> dict:
    profile = {
        "schema": 1,
        "extension": extension,
        "chunk_chars": settings.material_chunk_chars,
        "overlap_chars": settings.material_chunk_overlap_chars,
    }
    if extension == ".pdf":
        profile["ocr"] = {
            "enabled": settings.material_ocr_enabled,
            "dpi": settings.material_ocr_dpi,
            "confidence": settings.material_ocr_min_confidence,
            "max_pages": settings.material_ocr_max_pages,
        }
        profile["mineru"] = {
            "enabled": settings.material_mineru_enabled,
            "mode": settings.material_mineru_mode,
            "tier": settings.material_mineru_tier,
            "threshold": settings.material_mineru_auto_threshold,
        }
    return profile


class MaterialParseCache:
    def __init__(self, session, settings):
        self.session = session
        self.settings = settings

    def restore(self, target: MaterialRevision) -> bool:
        # Explicit reparse must execute even if identical bytes are already cached.
        if target.metadata_json.get("requested_pdf_parser") or target.status == "READY":
            return False
        extension = target.metadata_json.get("extension")
        if not extension:
            return False
        profile = parse_cache_profile(self.settings, extension)
        candidates = self.session.scalars(
            select(MaterialRevision)
            .join(MaterialSource, MaterialSource.id == MaterialRevision.source_id)
            .where(
                MaterialRevision.id != target.id,
                MaterialRevision.content_hash == target.content_hash,
                MaterialRevision.file_size == target.file_size,
                MaterialRevision.status == "READY",
                MaterialRevision.metadata_json["extension"].as_string() == extension,
                MaterialSource.status == "ACTIVE",
            )
            .order_by(
                case((MaterialRevision.parser_name == "mineru", 0), else_=1),
                MaterialRevision.completed_at.desc(),
            )
            .limit(32)
        )
        for donor in candidates:
            if not self._compatible(donor, profile):
                continue
            chunks = list(
                self.session.scalars(
                    select(MaterialChunk)
                    .where(MaterialChunk.revision_id == donor.id)
                    .order_by(MaterialChunk.sequence)
                )
            )
            metadata = donor.metadata_json
            if not chunks or len(chunks) != metadata.get("chunk_count"):
                continue
            if max(chunk.char_end for chunk in chunks) != metadata.get("character_count"):
                continue
            if [chunk.sequence for chunk in chunks] != list(range(len(chunks))):
                continue
            if any(
                chunk.char_start < 0
                or chunk.char_end - chunk.char_start != len(chunk.text)
                or hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() != chunk.content_hash
                for chunk in chunks
            ):
                continue
            self._copy(donor, target, chunks, profile)
            return True
        return False

    def _compatible(self, donor, profile):
        metadata = donor.metadata_json
        if metadata.get("coverage", "COMPLETE") != "COMPLETE" or metadata.get("unread_pages"):
            return False
        if metadata.get("mineru_attempted") and not metadata.get("mineru_used"):
            return False
        if (
            profile["extension"] == ".pdf"
            and self.settings.material_mineru_enabled
            and self.settings.material_mineru_mode == "FORCE"
            and donor.parser_name != "mineru"
        ):
            return False
        if not 0 < metadata.get("character_count", 0) <= self.settings.material_max_extracted_chars:
            return False
        if (
            metadata.get("chunk_chars") != self.settings.material_chunk_chars
            or metadata.get("overlap_chars") != self.settings.material_chunk_overlap_chars
        ):
            return False
        expected_version = (
            "2"
            if donor.parser_name == "builtin-text"
            else "4.0.10"
            if donor.parser_name == "mineru"
            else None
        )
        if expected_version is None:
            try:
                expected_version = version(donor.parser_name)
            except PackageNotFoundError:
                return False
        if donor.parser_version != expected_version:
            return False
        stored_profile = metadata.get("parse_cache_profile")
        if stored_profile is not None:
            if stored_profile != profile:
                return False
            if (
                metadata.get("requested_pdf_parser") == "BASIC"
                and self.settings.material_mineru_enabled
            ):
                return False
        elif profile["extension"] == ".pdf":
            # Existing parsed PDFs predate cache profiles. Admit conservative,
            # known defaults so users can reuse their already parsed papers.
            if donor.parser_name == "mineru":
                if self.settings.material_mineru_tier != "standard":
                    return False
            elif self.settings.material_mineru_enabled and (
                "complexity_score" not in metadata
                or metadata["complexity_score"] >= self.settings.material_mineru_auto_threshold
            ):
                return False
            if metadata.get("ocr_pages") and profile["ocr"] != {
                "enabled": True,
                "dpi": 144,
                "confidence": 0.6,
                "max_pages": 100,
            }:
                return False
        return True

    def _copy(self, donor, target, chunks, profile):
        from .processing import _fts_search_text

        now = datetime.now(UTC)
        old_embeddings = {
            row.chunk_id: row
            for row in self.session.scalars(
                select(MaterialEmbedding)
                .join(MaterialChunk, MaterialChunk.id == MaterialEmbedding.chunk_id)
                .where(
                    MaterialChunk.revision_id == donor.id,
                    MaterialEmbedding.model == self.settings.material_embedding_identity,
                )
            )
        }
        embeddings_complete = len(old_embeddings) == len(chunks) and all(
            old_embeddings[chunk.id].content_hash == chunk.content_hash for chunk in chunks
        )
        for chunk in chunks:
            copied = MaterialChunk(
                revision_id=target.id,
                sequence=chunk.sequence,
                section_title=chunk.section_title,
                text=chunk.text,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                locator_json=deepcopy(chunk.locator_json),
                content_hash=chunk.content_hash,
            )
            self.session.add(copied)
            self.session.flush()
            search = _fts_search_text(copied.section_title + "\n" + copied.text)
            if search:
                self.session.connection().exec_driver_sql(
                    "INSERT INTO material_chunks_fts(chunk_id, revision_id, search_text) VALUES (?, ?, ?)",
                    (copied.id, target.id, search),
                )
            if embeddings_complete:
                self.session.add(
                    MaterialEmbedding(
                        chunk_id=copied.id,
                        model=self.settings.material_embedding_identity,
                        content_hash=copied.content_hash,
                        vector_json=deepcopy(old_embeddings[chunk.id].vector_json),
                    )
                )
            self.session.add(
                MaterialProcessingUnit(
                    revision_id=target.id,
                    sequence=copied.sequence,
                    unit_type="CHUNK",
                    range_start=copied.char_start,
                    range_end=copied.char_end,
                    status="SUCCEEDED",
                    checkpoint_json={"chunk_count": len(chunks), "cache_reused": True},
                )
            )
        metadata = deepcopy(donor.metadata_json)
        for key in ("requested_pdf_parser", "semantic_warning", "embedding_model"):
            metadata.pop(key, None)
        target.metadata_json = {
            **metadata,
            "parse_cache_profile": profile,
            "cache_reused": True,
            "cache_source_revision_id": donor.id,
            "cache_reused_at": now.isoformat(),
            "semantic_status": "READY"
            if embeddings_complete
            else "UNAVAILABLE"
            if self.settings.material_semantic_enabled
            else "DISABLED",
        }
        if embeddings_complete:
            target.metadata_json["embedding_model"] = self.settings.material_embedding_identity
        elif self.settings.material_semantic_enabled:
            target.metadata_json["semantic_warning"] = (
                "已复用正文；当前向量模型尚未建立索引，关键词检索可用。"
            )
        target.parser_name = donor.parser_name
        target.parser_version = donor.parser_version
        target.status = "READY"
        target.completed_at = now
        target.error_code = target.error_message = None
        job = self.session.scalar(
            select(MaterialProcessingJob).where(MaterialProcessingJob.revision_id == target.id)
        )
        if job is None:
            self.session.add(
                MaterialProcessingJob(
                    revision_id=target.id,
                    status="READY",
                    completed_at=now,
                    processing_duration_ms=0,
                )
            )

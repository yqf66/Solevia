from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from planner_api.config import Settings, get_settings
from planner_api.models import (
    Goal,
    GoalMaterialLink,
    MaterialChunk,
    MaterialEmbedding,
    MaterialProcessingJob,
    MaterialProcessingUnit,
    MaterialRevision,
    MaterialRoot,
    MaterialSource,
)

from .chunking import chunk_material
from .parsers import MaterialParseError
from .sandbox_parser import parse_material
from .schemas import MaterialSearchHitRead, MaterialSearchRequest


class MaterialProcessingCancelled(Exception):
    pass


class MaterialProcessingService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.semantic_status = "DISABLED"
        self.retrieval_status: dict = {"status": "NOT_RUN"}

    def validate_search_access(self, goal_id: str, selected: list[str] | None):
        if self.session.get(Goal, goal_id) is None:
            raise HTTPException(404, "Goal not found")
        # Column reads bypass stale ORM identities. No cross-request document
        # candidates are cached, including after a same-revision index rebuild.
        links = list(self.session.execute(select(GoalMaterialLink.source_id,
            GoalMaterialLink.scope, GoalMaterialLink.usage_policy, MaterialSource.status,
            MaterialSource.title, MaterialSource.relative_path, MaterialRoot.status.label("root_status"))
            .join(MaterialSource, MaterialSource.id == GoalMaterialLink.source_id)
            .outerjoin(MaterialRoot, MaterialRoot.id == MaterialSource.root_id)
            .where(GoalMaterialLink.goal_id == goal_id, GoalMaterialLink.status == "ACTIVE")
            .order_by(GoalMaterialLink.source_id)))
        linked = {row.source_id for row in links}
        if selected is not None and set(selected) - linked:
            raise HTTPException(422, "分析范围包含未关联到当前规划的资料")
        included = [row for row in links if selected is None or row.source_id in selected]
        if any(row.status != "ACTIVE" or row.root_status not in {None, "ACTIVE"} for row in included):
            raise HTTPException(409, "所选资料或授权目录已停用")
        return included

    def _search_scope(self, goal_id: str, selected: list[str] | None) -> dict:
        from .retrieval_cache import fingerprint

        included = self.validate_search_access(goal_id, selected)
        ids = [row.source_id for row in included]
        latest = {}
        for row in self.session.execute(select(MaterialRevision.id, MaterialRevision.source_id,
            MaterialRevision.revision, MaterialRevision.status, MaterialRevision.content_hash,
            MaterialRevision.metadata_json).where(MaterialRevision.source_id.in_(ids))
            .order_by(MaterialRevision.revision.desc())):
            latest.setdefault(row.source_id, SimpleNamespace(**row._mapping))
        signature = fingerprint({"sources": [tuple(row) for row in included],
            "revisions": [vars(latest[key]) for key in sorted(latest)]})
        return {"latest": latest, "revision_ids": [r.id for r in latest.values() if r.status == "READY"],
                "fingerprint": signature}

    def _database_stamp(self):
        if self.session.get_bind().dialect.name == "sqlite":
            return tuple(self.session.execute(text("SELECT total_changes()" )).one()) + (
                self.session.execute(text("PRAGMA data_version")).scalar_one(),)
        return None

    def search_goal_materials_many(self, goal_id: str, requests: list[MaterialSearchRequest]):
        self.last_search_scope = None
        if not requests:
            return []
        if not self.settings.material_retrieval_shared_enabled:
            groups = [self.search_goal_materials(goal_id, request) for request in requests]
            self.retrieval_status = {"status": "OFF", "scope_reads": len(requests),
                "query_count": len(requests)}
            return groups
        if any(request.source_ids != requests[0].source_ids for request in requests):
            raise ValueError("Shared retrieval requires one authorized source scope")
        for attempt in range(2):
            self.semantic_status = "NOT_RUN" if self.settings.material_semantic_enabled else "DISABLED"
            stamp = self._database_stamp()
            scope = self._search_scope(goal_id, requests[0].source_ids)
            results, failed = None, False
            metrics = {"vector_scans": 0, "query_vectors_encoded": 0, "query_vector_hits": 0}
            if self.settings.material_semantic_enabled and scope["revision_ids"]:
                try:
                    from .semantic import semantic_candidates_many
                    results, metrics = semantic_candidates_many(self.session, scope["revision_ids"],
                        [r.query for r in requests], self.settings,
                        [min(r.max_chunks * 20, 300) for r in requests])
                except Exception as error:
                    failed = True
                    logging.getLogger(__name__).warning("Shared semantic fallback: %s", type(error).__name__)
            groups, statuses = [], []
            for index, request in enumerate(requests):
                groups.append(self.search_goal_materials(goal_id, request, _scope=scope,
                    _semantic_results=results[index] if results is not None else None,
                    _semantic_failed=failed))
                statuses.append(self.semantic_status)
            # Detect source/version/authorization changes and external SQLite
            # commits, including index-only mutations; never return mixed scope.
            current = self._search_scope(goal_id, requests[0].source_ids)
            if current["fingerprint"] != scope["fingerprint"] or self._database_stamp() != stamp:
                continue
            self.last_search_scope = scope
            self.retrieval_status = {"status": "SHARED", "query_count": len(requests),
                "scope_reads": 2, "snapshot_retries": attempt, "scope_hash": scope["fingerprint"],
                "semantic_by_query": statuses, "semantic_fallback": failed, **metrics,
                "candidate_cache": "DISABLED_NO_INDEX_GENERATION_CONTRACT"}
            return groups
        raise HTTPException(409, {"code": "MATERIAL_SCOPE_CHANGED",
            "message": "检索期间资料版本或授权发生变化，已丢弃本次结果，请重试。"})

    def process_for_goal(
        self, goal_id: str, source_id: str, pdf_parser: str | None = None
    ) -> MaterialRevision:
        link = self.session.scalar(
            select(GoalMaterialLink).where(
                GoalMaterialLink.goal_id == goal_id,
                GoalMaterialLink.source_id == source_id,
                GoalMaterialLink.status == "ACTIVE",
            )
        )
        if link is None:
            raise HTTPException(404, "Material is not linked to this goal")
        source = self.session.get(MaterialSource, source_id)
        if source is None or source.root_id is None or source.relative_path is None:
            raise HTTPException(409, "Material source path is missing")
        revision = self.session.scalar(
            select(MaterialRevision)
            .where(MaterialRevision.source_id == source_id)
            .order_by(MaterialRevision.revision.desc())
        )
        if revision is None:
            raise HTTPException(409, "Material has no revision")
        if pdf_parser is not None:
            if Path(source.relative_path).suffix.lower() != ".pdf":
                raise HTTPException(422, "仅 PDF 支持选择解析器")
            if pdf_parser == "MINERU" and not self.settings.material_mineru_enabled:
                raise HTTPException(409, "MinerU 尚未启用，请先准备独立环境及本地模型")
            if revision.status in {"QUEUED", "PROCESSING"}:
                raise HTTPException(409, "资料正在解析，请等待完成或取消后再选择解析器")
            revision = MaterialRevision(
                source_id=source.id,
                revision=revision.revision + 1,
                content_hash=revision.content_hash,
                file_size=revision.file_size,
                source_modified_at=revision.source_modified_at,
                metadata_json={"extension": ".pdf", "requested_pdf_parser": pdf_parser},
            )
            self.session.add(revision)
            self.session.flush()
        self.enqueue_revision(revision)
        return revision

    def enqueue_revision(self, revision: MaterialRevision) -> MaterialProcessingJob:
        job = self.session.scalar(
            select(MaterialProcessingJob).where(MaterialProcessingJob.revision_id == revision.id)
        )
        if revision.status == "READY":
            if job is None:
                job = MaterialProcessingJob(
                    revision_id=revision.id,
                    status="READY",
                    completed_at=revision.completed_at or datetime.now(UTC),
                )
                self.session.add(job)
                self.session.commit()
            elif job.status != "READY":
                job.status = "READY"
                job.error_code = None
                job.error_message = None
                job.lease_expires_at = None
                job.completed_at = revision.completed_at or datetime.now(UTC)
                self.session.commit()
            return job
        if job is not None and job.status == "PROCESSING":
            return job
        if job is None:
            job = MaterialProcessingJob(revision_id=revision.id)
            self.session.add(job)
        elif job.status in {"FAILED", "CANCELLED", "READY"}:
            job.status = "QUEUED"
            job.attempts = 0
            job.queued_at = datetime.now(UTC)
            job.started_at = None
            job.queue_wait_ms = None
            job.processing_duration_ms = None
            job.cancel_requested_at = None
            job.error_code = None
            job.error_message = None
            job.lease_expires_at = None
            job.completed_at = None
        if revision.status != "READY":
            revision.status = "QUEUED"
            revision.error_code = None
            revision.error_message = None
            revision.completed_at = None
        self.session.commit()
        return job

    def cancel_for_goal(self, goal_id: str, source_id: str) -> MaterialProcessingJob:
        link = self.session.scalar(
            select(GoalMaterialLink).where(
                GoalMaterialLink.goal_id == goal_id,
                GoalMaterialLink.source_id == source_id,
                GoalMaterialLink.status == "ACTIVE",
            )
        )
        if link is None:
            raise HTTPException(404, "Material is not linked to this goal")
        revision = self.session.scalar(
            select(MaterialRevision)
            .where(MaterialRevision.source_id == source_id)
            .order_by(MaterialRevision.revision.desc())
        )
        if revision is None:
            raise HTTPException(404, "Material revision not found")
        job = self.session.scalar(
            select(MaterialProcessingJob).where(MaterialProcessingJob.revision_id == revision.id)
        )
        if job is None:
            raise HTTPException(409, "Material processing job not found")
        if job.status == "QUEUED":
            now = datetime.now(UTC)
            job.status = "CANCELLED"
            job.completed_at = now
            job.queue_wait_ms = _elapsed_ms(job.queued_at, now)
            job.error_code = None
            job.error_message = None
            revision.status = "CANCELLED"
            revision.error_code = None
            revision.error_message = None
            revision.completed_at = now
        elif job.status == "PROCESSING" and job.cancel_requested_at is None:
            job.cancel_requested_at = datetime.now(UTC)
        self.session.commit()
        return job

    def process_revision(
        self,
        revision: MaterialRevision,
        path: Path,
        *,
        is_cancel_requested: Callable[[], bool] | None = None,
    ) -> MaterialRevision:
        if revision.status == "READY":
            return revision
        revision.status = "PROCESSING"
        revision.error_code = None
        revision.error_message = None
        self.session.commit()
        try:

            def check_cancelled():
                if is_cancel_requested is not None and is_cancel_requested():
                    raise MaterialProcessingCancelled

            if path.stat().st_size > self.settings.material_max_file_bytes:
                raise MaterialParseError("FILE_TOO_LARGE", "文档超过允许的本地解析大小。")
            if _hash_file(path, self.settings.material_max_file_bytes) != revision.content_hash:
                raise MaterialParseError(
                    "FILE_CHANGED",
                    "资料在建立 revision 后发生变化，请刷新资料后再解析。",
                )
            if is_cancel_requested is not None and is_cancel_requested():
                raise MaterialProcessingCancelled
            from .parse_cache import MaterialParseCache, parse_cache_profile

            if MaterialParseCache(self.session, self.settings).restore(revision):
                if _hash_file(path, self.settings.material_max_file_bytes) != revision.content_hash:
                    raise MaterialParseError(
                        "FILE_CHANGED_DURING_PARSE", "复用期间资料发生变化，请刷新后重试。"
                    )
                check_cancelled()
                self.session.commit()
                return revision
            parsed = parse_material(
                path,
                timeout_seconds=self.settings.material_parse_timeout_seconds,
                memory_mb=self.settings.material_parse_memory_mb,
                max_chars=self.settings.material_max_extracted_chars,
                max_archive_uncompressed_bytes=(
                    self.settings.material_max_archive_uncompressed_bytes
                ),
                ocr_enabled=self.settings.material_ocr_enabled,
                ocr_max_pages=self.settings.material_ocr_max_pages,
                ocr_dpi=self.settings.material_ocr_dpi,
                ocr_min_confidence=self.settings.material_ocr_min_confidence,
                mineru_enabled=self.settings.material_mineru_enabled
                and revision.metadata_json.get("requested_pdf_parser") != "BASIC",
                mineru_mode="FORCE"
                if revision.metadata_json.get("requested_pdf_parser") == "MINERU"
                else "AUTO"
                if revision.metadata_json.get("requested_pdf_parser") == "AUTO"
                else self.settings.material_mineru_mode,
                mineru_tier=self.settings.material_mineru_tier,
                mineru_auto_threshold=self.settings.material_mineru_auto_threshold,
                mineru_python=self.settings.material_mineru_python,
                mineru_home=self.settings.material_mineru_home,
                mineru_timeout_seconds=self.settings.material_mineru_timeout_seconds,
                mineru_max_pages=self.settings.material_mineru_max_pages,
                check_cancelled=check_cancelled,
            )
            if _hash_file(path, self.settings.material_max_file_bytes) != revision.content_hash:
                raise MaterialParseError(
                    "FILE_CHANGED_DURING_PARSE",
                    "资料在解析过程中发生变化，已丢弃本次解析结果。",
                )
            if is_cancel_requested is not None and is_cancel_requested():
                raise MaterialProcessingCancelled
            chunks = chunk_material(
                parsed.text,
                max_chars=self.settings.material_chunk_chars,
                overlap_chars=self.settings.material_chunk_overlap_chars,
                locations=parsed.locations,
            )
            if not chunks:
                raise MaterialParseError("EMPTY_DOCUMENT", "解析成功但文档没有可检索正文。")
            semantic_vectors = None
            semantic_warning = None
            if self.settings.material_semantic_enabled:
                try:
                    from .semantic import encode_passages

                    semantic_vectors = encode_passages(
                        [chunk.section_title + "\n" + chunk.text for chunk in chunks],
                        self.settings,
                        is_cancel_requested,
                    )
                except Exception as error:
                    check_cancelled()
                    semantic_warning = str(error)[:300]
            if is_cancel_requested is not None and is_cancel_requested():
                raise MaterialProcessingCancelled
            connection = self.session.connection()
            self.session.execute(
                delete(MaterialEmbedding).where(
                    MaterialEmbedding.chunk_id.in_(
                        select(MaterialChunk.id).where(MaterialChunk.revision_id == revision.id)
                    )
                )
            )
            self.session.execute(
                delete(MaterialChunk).where(MaterialChunk.revision_id == revision.id)
            )
            self.session.execute(
                delete(MaterialProcessingUnit).where(
                    MaterialProcessingUnit.revision_id == revision.id
                )
            )
            for chunk in chunks:
                self.session.add(
                    MaterialChunk(
                        revision_id=revision.id,
                        sequence=chunk.sequence,
                        section_title=chunk.section_title,
                        text=chunk.text,
                        char_start=chunk.char_start,
                        char_end=chunk.char_end,
                        locator_json=chunk.locator_details,
                        content_hash=_hash_text(chunk.text),
                    )
                )
            self.session.flush()
            persisted_chunks = self.session.scalars(
                select(MaterialChunk)
                .where(MaterialChunk.revision_id == revision.id)
                .order_by(MaterialChunk.sequence)
            )
            connection = self.session.connection()
            connection.exec_driver_sql(
                "DELETE FROM material_chunks_fts WHERE revision_id = ?", (revision.id,)
            )
            for chunk in persisted_chunks:
                search_text = _fts_search_text(chunk.section_title + "\n" + chunk.text)
                if search_text:
                    connection.exec_driver_sql(
                        """
                        INSERT INTO material_chunks_fts(chunk_id, revision_id, search_text)
                        VALUES (?, ?, ?)
                        """,
                        (chunk.id, revision.id, search_text),
                    )
                self.session.add(
                    MaterialProcessingUnit(
                        revision_id=revision.id,
                        sequence=chunk.sequence,
                        unit_type="CHUNK",
                        range_start=chunk.char_start,
                        range_end=chunk.char_end,
                        status="SUCCEEDED",
                        checkpoint_json={"chunk_count": len(chunks)},
                    )
                )
            revision.parser_name = parsed.parser_name
            revision.parser_version = parsed.parser_version
            revision.metadata_json = {
                **revision.metadata_json,
                **parsed.metadata,
                "character_count": len(parsed.text),
                "chunk_count": len(chunks),
                "chunk_chars": self.settings.material_chunk_chars,
                "overlap_chars": self.settings.material_chunk_overlap_chars,
                "locator_count": len(parsed.locations),
                "parse_cache_profile": parse_cache_profile(self.settings, path.suffix.lower()),
            }
            if self.settings.material_semantic_enabled:
                if semantic_vectors is not None:
                    persisted = list(
                        self.session.scalars(
                            select(MaterialChunk)
                            .where(MaterialChunk.revision_id == revision.id)
                            .order_by(MaterialChunk.sequence)
                        )
                    )
                    for chunk, vector in zip(persisted, semantic_vectors, strict=True):
                        self.session.add(
                            MaterialEmbedding(
                                chunk_id=chunk.id,
                                model=self.settings.material_embedding_identity,
                                content_hash=chunk.content_hash,
                                vector_json=vector,
                            )
                        )
                    revision.metadata_json = {
                        **revision.metadata_json,
                        "semantic_status": "READY",
                        "embedding_model": self.settings.material_embedding_identity,
                    }
                else:
                    revision.metadata_json = {
                        **revision.metadata_json,
                        "semantic_status": "UNAVAILABLE",
                        "semantic_warning": semantic_warning,
                    }
            revision.status = "READY"
            revision.completed_at = datetime.now(UTC)
            self.session.commit()
            from planner_api.knowledge.store import capture
            capture(self.session, self.settings, revision=revision)
            if self.settings.knowledge_enabled:
                self.session.commit()
            return revision
        except MaterialProcessingCancelled:
            self.session.rollback()
            revision = self.session.get(MaterialRevision, revision.id)
            if revision is not None:
                revision.status = "CANCELLED"
                revision.error_code = None
                revision.error_message = None
                revision.completed_at = datetime.now(UTC)
                self.session.commit()
            return revision
        except MaterialParseError as error:
            self.session.rollback()
            self._mark_failed(revision, error.code, str(error))
            return revision
        except Exception as error:
            self.session.rollback()
            self._mark_failed(revision, "PARSE_FAILED", f"资料解析失败：{error}")
            return revision

    def search_goal_materials(
        self,
        goal_id: str,
        request: MaterialSearchRequest,
        *, _scope: dict | None = None, _semantic_results: list | None = None,
        _semantic_failed: bool = False,
    ) -> list[MaterialSearchHitRead]:
        if self.session.get(Goal, goal_id) is None:
            raise HTTPException(404, "Goal not found")
        terms = _fts_terms(request.query, limit=64)
        if not terms:
            raise HTTPException(422, "检索词不能为空")
        scope = _scope if _scope is not None else self._search_scope(goal_id, request.source_ids)
        latest_by_source = scope["latest"]
        revision_ids = scope["revision_ids"]
        if not revision_ids:
            return []
        placeholders = ", ".join(f":revision_{index}" for index in range(len(revision_ids)))
        query_terms = " OR ".join(f'"{term}"' for term in sorted(terms))
        parameters: dict[str, object] = {
            "match_query": query_terms,
            "candidate_limit": min(max(request.max_chunks * 20, 50), 300),
        }
        parameters.update(
            {f"revision_{index}": revision_id for index, revision_id in enumerate(revision_ids)}
        )
        rows = self.session.execute(
            text(
                f"""
                SELECT chunks.id, chunks.revision_id, chunks.sequence,
                       chunks.section_title, chunks.text, chunks.char_start, chunks.char_end,
                       chunks.locator_json,
                       revisions.source_id, revisions.revision,
                       bm25(material_chunks_fts) AS rank
                FROM material_chunks_fts
                JOIN material_chunks AS chunks
                  ON chunks.id = material_chunks_fts.chunk_id
                JOIN material_revisions AS revisions
                  ON revisions.id = chunks.revision_id
                WHERE material_chunks_fts MATCH :match_query
                  AND material_chunks_fts.revision_id IN ({placeholders})
                ORDER BY rank ASC, chunks.sequence ASC
                LIMIT :candidate_limit
                """
            ),
            parameters,
        )
        ranked_results: list[tuple[float, MaterialSearchHitRead]] = []
        for row in rows:
            score = _score_chunk(row.text, terms)
            if score <= 0:
                continue
            # A heading names the subject of a passage; incidental mentions in
            # repeated appendices should not outrank a section devoted to it.
            score += 0.8 * _score_chunk(row.section_title, terms)
            excerpt, excerpt_start, excerpt_end = _excerpt_for_query(
                row.text, terms, request.max_chars
            )
            locator_details = _filter_locator_details(
                _decode_locator_json(row.locator_json), excerpt_start, excerpt_end
            )
            ranked_results.append(
                (
                    float(row.rank),
                    MaterialSearchHitRead(
                        source_id=row.source_id,
                        revision_id=row.revision_id,
                        revision=row.revision,
                        sequence=row.sequence,
                        section_title=row.section_title,
                        text=excerpt,
                        score=score,
                        locator=_format_locators(
                            locator_details, row.sequence, row.char_start, row.char_end
                        ),
                        locator_details=locator_details,
                    ),
                )
            )
        ranked_results.sort(
            key=lambda item: (-item[1].score, item[0], item[1].source_id, item[1].sequence)
        )
        if self.settings.material_semantic_enabled:
            try:
                from .semantic import semantic_candidates

                if _semantic_failed:
                    raise RuntimeError("Shared semantic search unavailable")
                semantic = (_semantic_results if _semantic_results is not None else semantic_candidates(
                    self.session,
                    revision_ids,
                    request.query,
                    self.settings,
                    min(request.max_chunks * 20, 300),
                ))
                revision_map = {revision.id: revision for revision in latest_by_source.values()}
                fused = {}
                for rank, (_, hit) in enumerate(ranked_results, start=1):
                    fused[(hit.revision_id, hit.sequence)] = (1 / (60 + rank), hit)
                for rank, (similarity, chunk) in enumerate(semantic, start=1):
                    identity = (chunk.revision_id, chunk.sequence)
                    previous = fused.get(identity)
                    if previous:
                        fused[identity] = (
                            previous[0] + 1 / (60 + rank),
                            previous[1].model_copy(update={"retrieval_method": "HYBRID"}),
                        )
                        continue
                    revision = revision_map[chunk.revision_id]
                    excerpt, start, end = _excerpt_for_query(chunk.text, terms, request.max_chars)
                    details = _filter_locator_details(chunk.locator_json, start, end)
                    hit = MaterialSearchHitRead(
                        source_id=revision.source_id,
                        revision_id=revision.id,
                        revision=revision.revision,
                        sequence=chunk.sequence,
                        section_title=chunk.section_title,
                        text=excerpt,
                        score=similarity,
                        locator=_format_locators(
                            details, chunk.sequence, chunk.char_start, chunk.char_end
                        ),
                        locator_details=details,
                        retrieval_method="SEMANTIC",
                    )
                    fused[identity] = (1 / (60 + rank), hit)
                ranked_results = [
                    (-score, hit)
                    for score, hit in sorted(
                        fused.values(),
                        key=lambda item: (-item[0], item[1].revision_id, item[1].sequence),
                    )
                ]
                self.semantic_status = "READY" if semantic else "NO_MATCH_OR_UNINDEXED"
            except Exception as error:
                self.semantic_status = "FALLBACK"
                logging.getLogger(__name__).warning(
                    "Material semantic search fell back to FTS: %s", str(error)[:300]
                )
        if self.settings.local_model_mode == "SHADOW" or self.settings.local_model_dataset_mode == "CAPTURE":
            from planner_api.local_model_shadow import shadow_material_rerank

            shadow_material_rerank(
                self.settings, request.query,
                [hit.text for _, hit in ranked_results[:24]],
                group_id=goal_id,
            )
        if request.diversify_sources:
            by_source: dict[str, list] = {}
            for item in ranked_results:
                by_source.setdefault(item[1].source_id, []).append(item)
            ranked_results = [
                items[offset]
                for offset in range(max((len(items) for items in by_source.values()), default=0))
                for items in by_source.values()
                if offset < len(items)
            ]
        selected: list[MaterialSearchHitRead] = []
        total_chars = 0
        for _, hit in ranked_results:
            if len(selected) >= request.max_chunks or total_chars >= request.max_total_chars:
                break
            text_budget = min(request.max_chars, request.max_total_chars - total_chars)
            excerpt, _, _ = _excerpt_for_query(hit.text, terms, text_budget)
            selected.append(hit.model_copy(update={"text": excerpt}))
            total_chars += len(excerpt)
        return selected

    def _resolve_path(self, source: MaterialSource) -> Path:
        root = self.session.get(MaterialRoot, source.root_id)
        if root is None or root.status != "ACTIVE":
            raise HTTPException(409, "Material root is disabled")
        root_path = Path(root.canonical_path).resolve(strict=True)
        try:
            resolved = (root_path / source.relative_path).resolve(strict=True)
            resolved.relative_to(root_path)
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(422, "资料文件不存在或已超出授权目录") from error
        if not resolved.is_file():
            raise HTTPException(422, "资料路径必须指向文件")
        return resolved

    def _mark_failed(self, revision: MaterialRevision, code: str, message: str) -> None:
        revision.status = "FAILED"
        revision.error_code = code
        revision.error_message = message[:2000]
        revision.completed_at = datetime.now(UTC)
        self.session.commit()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_file(path: Path, max_bytes: int) -> str:
    digest = hashlib.sha256()
    total_bytes = 0
    with path.open("rb") as stream:
        while block := stream.read(min(1024 * 1024, max_bytes - total_bytes + 1)):
            total_bytes += len(block)
            if total_bytes > max_bytes:
                raise MaterialParseError("FILE_TOO_LARGE", "文档超过允许的本地解析大小。")
            digest.update(block)
    return digest.hexdigest()


def _fts_terms(value: str, limit: int | None = None) -> set[str]:
    terms = list(re.findall(r"[a-z0-9_]+", value.casefold()))
    for sequence in re.findall(r"[\u4e00-\u9fff]+", value):
        if len(sequence) == 1:
            terms.append(sequence)
        else:
            terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    stop_words = {"a", "an", "and", "for", "in", "is", "of", "on", "or", "the", "to"}
    unique = [term for term in dict.fromkeys(terms) if term and term not in stop_words]
    return set(unique[:limit] if limit is not None else unique)


def _fts_search_text(value: str) -> str:
    return " ".join(sorted(_fts_terms(value)))


def _score_chunk(text: str, terms: set[str]) -> float:
    content_terms = _fts_terms(text)
    overlap = len(content_terms & terms)
    if not overlap:
        return 0.0
    return round(overlap / max(len(terms), 1) * 100, 3)


def _format_locators(
    details: list[dict[str, object]] | None,
    sequence: int,
    char_start: int,
    char_end: int,
) -> str:
    labels: list[str] = []
    for detail in details or []:
        kind = detail.get("kind")
        if kind in {"pdf_page", "pdf_pages"}:
            start = detail.get("page_start", detail.get("page"))
            end = detail.get("page_end", start)
            labels.append(f"PDF 第 {start} 页" if start == end else f"PDF 第 {start}-{end} 页")
        elif kind == "pdf_table_row":
            labels.append(
                f"PDF 第 {detail.get('page')} 页表格 {detail.get('table')} "
                f"第 {detail.get('row')} 行"
            )
        elif kind in {"pptx_slide", "pptx_text_shape"}:
            labels.append(f"第 {detail.get('slide')} 张幻灯片")
        elif kind in {"xlsx_row", "xlsx_rows"}:
            sheet = detail.get("sheet", "")
            start = detail.get("row_start")
            end = detail.get("row_end", start)
            row_label = f"第 {start} 行" if start == end else f"第 {start}-{end} 行"
            labels.append(f"工作表 {sheet} {row_label}")
        elif kind == "xlsx_sheet":
            labels.append(f"工作表 {detail.get('sheet')}")
        elif kind == "docx_table_row":
            labels.append(f"表格 {detail.get('table')} 第 {detail.get('row')} 行")
        elif kind == "docx_paragraph":
            headings = detail.get("heading_path")
            if isinstance(headings, list) and headings:
                labels.append("章节 " + " / ".join(str(value) for value in headings[:4]))
            else:
                labels.append(f"段落 {detail.get('paragraph')}")
        elif kind == "csv_row":
            labels.append(f"CSV 第 {detail.get('row')} 行")
    if labels:
        return "；".join(dict.fromkeys(labels))[:500]
    return f"chunk:{sequence} chars:{char_start}-{char_end}"


def _decode_locator_json(value: object) -> list[dict[str, object]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _filter_locator_details(
    details: list[dict[str, object]], excerpt_start: int, excerpt_end: int
) -> list[dict[str, object]]:
    filtered = []
    for detail in details:
        start = detail.get("chunk_text_start")
        end = detail.get("chunk_text_end")
        if isinstance(start, int) and isinstance(end, int):
            if start >= excerpt_end or end <= excerpt_start:
                continue
        filtered.append(detail)
    return filtered or details[:1]


def _excerpt_for_query(text: str, terms: set[str], max_chars: int) -> tuple[str, int, int]:
    if len(text) <= max_chars:
        return text, 0, len(text)

    folded_text = text.casefold()
    positions: list[int] = []
    for term in terms:
        folded_term = term.casefold()
        if not folded_term:
            continue
        cursor = 0
        while (position := folded_text.find(folded_term, cursor)) >= 0:
            positions.append(position)
            cursor = position + max(len(folded_term), 1)
    max_start = len(text) - max_chars
    candidates = {0}
    for position in positions:
        candidates.add(max(0, min(position - max_chars // 4, max_start)))

    best_start = 0
    best_score = (-1.0, -1, 0)
    for start in candidates:
        excerpt = text[start : start + max_chars]
        overlap = _score_chunk(excerpt, terms)
        matched_terms = len(_fts_terms(excerpt) & terms)
        score = (overlap, matched_terms, -start)
        if score > best_score:
            best_start, best_score = start, score
    return text[best_start : best_start + max_chars], best_start, best_start + max_chars


def _elapsed_ms(start: datetime | None, end: datetime) -> int | None:
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0, round((end - start).total_seconds() * 1000))

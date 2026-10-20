from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.ai import ProviderUnavailableError
from planner_api.models import (
    Goal,
    GoalMaterialLink,
    MaterialAnalysis,
    MaterialSource,
    MaterialRevision,
    RequirementChange,
    RequirementFact,
    utc_now,
)

from .analysis_ai import (
    MaterialAnalysisContext,
    MaterialAnalysisPassage,
    MaterialAnalysisProvider,
    MATERIAL_READING_RULES,
)
from .processing import MaterialProcessingService
from .reranking import guarded_order
from .retrieval_cache import fingerprint
from .schemas import (
    MaterialAnalysisRead,
    MaterialAnalyzeRequest,
    MaterialCitationRead,
    MaterialConflictRead,
    MaterialFindingConfirmRequest,
    MaterialFindingRead,
    MaterialKeyPointRead,
    MaterialQueryCoverageRead,
    MaterialSearchRequest,
    MaterialSearchHitRead,
)


class MaterialAnalysisService:
    def __init__(
        self,
        session: Session,
        provider: MaterialAnalysisProvider,
        processing: MaterialProcessingService | None = None,
    ) -> None:
        self.session = session
        self.provider = provider
        self.processing = processing or MaterialProcessingService(session)
        self.rerank_status: dict = {"status": "NOT_RUN"}

    def list_analyses(self, goal_id: str) -> list[MaterialAnalysisRead]:
        self._goal(goal_id)
        rows = self.session.scalars(
            select(MaterialAnalysis)
            .where(MaterialAnalysis.goal_id == goal_id)
            .order_by(MaterialAnalysis.created_at.desc())
            .limit(20)
        )
        return [self._read(row) for row in rows]

    def prepare_conversation_context(
        self, goal_id: str, query: str, source_ids: list[str]
    ) -> dict[str, object]:
        linked = set(self.session.scalars(select(GoalMaterialLink.source_id).where(GoalMaterialLink.goal_id == goal_id, GoalMaterialLink.status == "ACTIVE")))
        if set(source_ids) - linked:
            raise HTTPException(422, "资料未关联到当前规划")
        revisions = list(self.session.scalars(select(MaterialRevision).where(MaterialRevision.source_id.in_(source_ids)).order_by(MaterialRevision.revision.desc())))
        latest = {}
        for revision in revisions:
            latest.setdefault(revision.source_id, revision)
        if any(source_id not in latest or latest[source_id].status != "READY" for source_id in source_ids):
            raise HTTPException(409, "所选资料尚未完成解析，请解析完成后发送")
        # The request Schema bounds the standalone analysis UI. Conversation
        # questions may be longer: pass the complete text to query planning,
        # rather than quietly deleting a trailing exclusion or second topic.
        question = query if len(query.strip()) >= 2 else f"资料 {query}"
        request = MaterialAnalyzeRequest(
            query=question if len(question) <= 2000 else "根据完整本次对话检索资料",
            source_ids=source_ids,
            max_chunks=12,
            max_total_chars=18000,
        )
        retrieval_failure = None
        try:
            passages, queries, groups = self._retrieve(goal_id, request, user_query=question)
        except HTTPException as error:
            if error.status_code != 409:
                raise
            passages, queries, groups = [], [question], [[]]
            if isinstance(error.detail, dict):
                retrieval_failure = error.detail
        completed_scope = getattr(self.processing, "last_search_scope", None)
        if passages and completed_scope:
            latest = completed_scope["latest"]
        return {
            "query": query,
            "source_ids": list(dict.fromkeys(source_ids)),
            "sources": [
                {
                    "source_id": source.id,
                    "title": source.title,
                    "relative_path": source.relative_path or "",
                }
                for source in self.session.scalars(
                    select(MaterialSource).where(MaterialSource.id.in_(source_ids))
                )
            ],
            "passages": [asdict(passage) for passage in passages],
            "revisions": [{"source_id": revision.source_id, "revision_id": revision.id, "revision": revision.revision, "warnings": revision.metadata_json.get("warnings", []), "unread_pages": revision.metadata_json.get("unread_pages", [])} for revision in latest.values()],
            "semantic_status": self.processing.semantic_status,
            "rerank_status": dict(self.rerank_status),
            "retrieval_status": dict(getattr(self.processing, "retrieval_status", {})),
            "query_plan_status": dict(getattr(self.provider, "query_plan_status", {})),
            "retrieval_failure": retrieval_failure,
            # Shared by clarification and execution discussion, not just the
            # optional analysis dialog. It carries no model-private details.
            "reading_rules": MATERIAL_READING_RULES,
            "note": (retrieval_failure["message"] if retrieval_failure else
                     "本次检索未找到支持该问题的证据；只能说明当前检索未覆盖，不能推断全文不存在或声称读完全文。")
            if not passages
            else None,
            "retrieval_coverage": [
                {"query": topic, "retrieved_chunks": sum(
                    (hit.revision_id, hit.sequence, hit.text, hit.locator) in self._selected_evidence
                    for hit in group)}
                for topic, group in zip(queries, groups, strict=True)
            ],
        }

    def _retrieve(
        self, goal_id: str, request: MaterialAnalyzeRequest, *, user_query: str | None = None
    ) -> tuple[list[MaterialAnalysisPassage], list[str], list[list[MaterialSearchHitRead]]]:
        self.rerank_status = {"status": "NOT_RUN"}
        self._selected_evidence = set()
        self._goal(goal_id)
        access = getattr(self.processing, "validate_search_access", None)
        if access:
            access(goal_id, request.source_ids)
        if request.source_ids is not None:
            linked_ids = set(
                self.session.scalars(
                    select(GoalMaterialLink.source_id).where(
                        GoalMaterialLink.goal_id == goal_id,
                        GoalMaterialLink.status == "ACTIVE",
                    )
                )
            )
            if set(request.source_ids) - linked_ids:
                raise HTTPException(422, "分析范围包含未关联到当前规划的资料")
            if not request.source_ids:
                raise HTTPException(409, "请至少选择一份已解析资料。")
        question = user_query if user_query is not None else request.query
        scoped = getattr(self.provider, "plan_queries_scoped", None)
        planned = scoped(question, {"goal_id": goal_id, "source_ids": request.source_ids,
            "database": fingerprint(str(self.session.get_bind().url))}) if scoped else self.provider.plan_queries(question)
        queries = list(dict.fromkeys(planned))[
            : min(4, request.max_chunks)
        ] or [question]
        if any(len(query) > 1000 for query in queries):
            # A provider outage returns the original question. Long fallback
            # text cannot fit a search request, and truncating it would hide
            # unsearched constraints while pretending retrieval succeeded.
            raise HTTPException(409, {
                "code": "MATERIAL_QUERY_PLAN_INCOMPLETE",
                "message": "完整问题已保留，但暂未整理出能覆盖问题的检索词。本次未取得资料证据，不能据此推断资料中不存在相关内容。",
                "query_chars": len(question),
            })
        searches = [MaterialSearchRequest(
                    query=query,
                    source_ids=request.source_ids,
                    max_chunks=request.max_chunks,
                    max_chars=min(2200, max(200, request.max_total_chars // len(queries))),
                    max_total_chars=request.max_total_chars,
                    diversify_sources=True,
                )
            for query in queries
        ]
        shared = getattr(self.processing, "search_goal_materials_many", None)
        groups = (shared(goal_id, searches) if shared else
                  [self.processing.search_goal_materials(goal_id, search) for search in searches])
        # Round-robin topics so the first part of a compound question cannot consume the context.
        hits = []
        seen = set()
        total_chars = 0
        for offset in range(request.max_chunks):
            for group in groups:
                if offset >= len(group) or len(hits) >= request.max_chunks:
                    continue
                hit = group[offset]
                # Same chunk identity does not prove the same excerpt. Different
                # topic windows/locators must survive with distinct citation IDs.
                identity = (hit.revision_id, hit.sequence, hit.text, hit.locator)
                if identity in seen or total_chars + len(hit.text) > request.max_total_chars:
                    continue
                hits.append(hit)
                seen.add(identity)
                total_chars += len(hit.text)
        self._selected_evidence = seen
        if not hits:
            raise HTTPException(409, "没有找到已解析的相关资料，请先完成解析或换一个问题。")
        variants = {}
        for group in groups:
            for hit in group:
                variants.setdefault((hit.revision_id, hit.sequence), set()).add((hit.text, hit.locator))

        def passage_id(hit):
            base = self._passage_id(hit.source_id, hit.revision_id, hit.sequence)
            return (base + ":" + fingerprint([hit.text, hit.locator])[:16]
                    if len(variants[(hit.revision_id, hit.sequence)]) > 1 else base)

        if isinstance(self.session, Session):
            sources = {row.id: row for row in self.session.execute(select(MaterialSource.id,
                MaterialSource.title, MaterialSource.relative_path).where(
                MaterialSource.id.in_({hit.source_id for hit in hits})))}
        else:
            sources = {hit.source_id: self.session.get(MaterialSource, hit.source_id) for hit in hits}
        passages = [
            MaterialAnalysisPassage(
                passage_id=passage_id(hit),
                source_id=hit.source_id,
                revision_id=hit.revision_id,
                sequence=hit.sequence,
                section_title=hit.section_title,
                locator=hit.locator,
                text=hit.text,
                source_title=sources[hit.source_id].title,
                relative_path=sources[hit.source_id].relative_path or "",
            )
            for hit in hits
        ]
        anchors = {
            passage_id(hit)
            for group in groups for hit in group[:1]
        }
        # Selection, source diversity, per-topic allocation and text budgets are
        # already final. BGE has no opportunity to displace a selected citation.
        passages, self.rerank_status = guarded_order(
            question, passages, self.processing.settings, topic_anchors=anchors,
            cache_scope=({"goal_id": goal_id, "source_ids": request.source_ids,
                "database": fingerprint(str(self.session.get_bind().url))}
                if isinstance(self.session, Session) else None),
        )
        return passages, queries, groups

    def analyze(self, goal_id: str, request: MaterialAnalyzeRequest) -> MaterialAnalysisRead:
        passages, queries, groups = self._retrieve(goal_id, request)
        seen = {(p.revision_id, p.sequence, p.text, p.locator) for p in passages}
        try:
            result = self.provider.analyze(
                MaterialAnalysisContext(query=request.query, passages=passages)
            )
        except ProviderUnavailableError as error:
            raise HTTPException(503, str(error)) from error
        valid_ids = {passage.passage_id for passage in passages}
        source_by_passage = {passage.passage_id: passage.source_id for passage in passages}
        conflicts = [
            conflict.model_copy(
                update={
                    "citation_ids": [item for item in conflict.citation_ids if item in valid_ids]
                }
            )
            for conflict in result.conflicts
            if len({source_by_passage[item] for item in conflict.citation_ids if item in valid_ids})
            >= 2
        ]
        blocked_ids = {item for conflict in conflicts for item in conflict.citation_ids}
        result = result.model_copy(
            update={
                "key_points": [
                    point.model_copy(
                        update={
                            "citation_ids": [
                                item for item in point.citation_ids if item in valid_ids
                            ]
                        }
                    )
                    for point in result.key_points
                ],
                "findings": [
                    finding.model_copy(
                        update={
                            "citation_ids": [
                                item for item in finding.citation_ids if item in valid_ids
                            ]
                        }
                    )
                    for finding in result.findings
                ],
                "conflicts": conflicts,
            }
        )
        row = MaterialAnalysis(
            goal_id=goal_id,
            query=request.query,
            status="COMPLETED",
            source_count=len(passages),
            result_json={
                **result.model_dump(mode="json"),
                "findings": [
                    {
                        **finding.model_dump(mode="json"),
                        "status": "BLOCKED"
                        if not finding.citation_ids
                        or blocked_ids.intersection(finding.citation_ids)
                        else "PROPOSED",
                    }
                    for finding in result.findings
                ],
                "file_count": len({passage.source_id for passage in passages}),
                "rerank_status": dict(self.rerank_status),
                "retrieval_status": dict(getattr(self.processing, "retrieval_status", {})),
                "query_plan_status": dict(getattr(self.provider, "query_plan_status", {})),
                "retrieval_coverage": [
                    {
                        "query": query,
                        "retrieved_chunks": sum(
                            (hit.revision_id, hit.sequence, hit.text, hit.locator) in seen for hit in group
                        ),
                    }
                    for query, group in zip(queries, groups, strict=True)
                ],
                "passages": [self._passage_json(passage) for passage in passages],
            },
        )
        self.session.add(row)
        self.session.commit()
        return self._read(row)

    def confirm_findings(
        self,
        goal_id: str,
        analysis_id: str,
        request: MaterialFindingConfirmRequest,
    ) -> MaterialAnalysisRead:
        self._goal(goal_id)
        row = self.session.scalar(
            select(MaterialAnalysis).where(
                MaterialAnalysis.id == analysis_id,
                MaterialAnalysis.goal_id == goal_id,
            )
        )
        if row is None:
            raise HTTPException(404, "资料分析不存在")
        findings = deepcopy(row.result_json.get("findings", []))
        passages = {
            item["passage_id"]: item
            for item in row.result_json.get("passages", [])
            if isinstance(item, dict)
        }
        indexes = list(dict.fromkeys(request.finding_indexes))
        for index in indexes:
            if index < 0 or index >= len(findings):
                raise HTTPException(422, f"资料事实编号无效：{index}")
            if findings[index].get("status") == "BLOCKED" or not any(
                item in passages for item in findings[index].get("citation_ids", [])
            ):
                raise HTTPException(409, "该事实缺少有效引用或存在跨文件冲突，请先核实资料。")
        for index in indexes:
            finding = findings[index]
            finding["status"] = "CONFIRMED"
            field_key = f"material_finding:{analysis_id}:{index}"
            existing = self.session.scalar(
                select(RequirementFact).where(
                    RequirementFact.goal_id == goal_id,
                    RequirementFact.field_key == field_key,
                )
            )
            citation_text = [
                self._citation_text(passages[citation_id])
                for citation_id in finding.get("citation_ids", [])
                if citation_id in passages
            ]
            value = {
                "text": finding.get("text", ""),
                "category": finding.get("category", "OTHER"),
                "citations": citation_text,
                "analysis_id": analysis_id,
            }
            if existing is None:
                existing = RequirementFact(
                    goal_id=goal_id,
                    category="MATERIAL_FINDING",
                    field_key=field_key,
                    value_json=value,
                    source="MATERIAL_CONFIRMED",
                    status="CONFIRMED",
                    confidence=float(finding.get("confidence", 0.5)),
                    operation="ADD",
                    change_reason="用户确认资料分析结果",
                    confirmed_at=utc_now(),
                )
                self.session.add(existing)
                self.session.flush()
                self.session.add(
                    RequirementChange(
                        goal_id=goal_id,
                        field_key=field_key,
                        operation="ADD",
                        reason="用户确认资料分析结果",
                        new_fact_id=existing.id,
                    )
                )
            elif existing.status != "CONFIRMED":
                existing.status = "CONFIRMED"
                existing.value_json = value
                existing.confirmed_at = utc_now()
        row.result_json = {**row.result_json, "findings": findings}
        row.updated_at = datetime.now(UTC)
        self.session.commit()
        return self._read(row)

    @staticmethod
    def _passage_id(source_id: str, revision_id: str, sequence: int) -> str:
        return f"material:{source_id}:{revision_id}:{sequence}"

    @staticmethod
    def _passage_json(passage: MaterialAnalysisPassage) -> dict[str, object]:
        return {
            "passage_id": passage.passage_id,
            "source_id": passage.source_id,
            "source_title": passage.source_title,
            "relative_path": passage.relative_path,
            "revision_id": passage.revision_id,
            "sequence": passage.sequence,
            "section_title": passage.section_title,
            "locator": passage.locator,
        }

    @staticmethod
    def _citation_text(passage: dict[str, object]) -> dict[str, object]:
        return {
            "source_id": passage.get("source_id"),
            "source_title": passage.get("source_title", ""),
            "relative_path": passage.get("relative_path", ""),
            "revision_id": passage.get("revision_id"),
            "sequence": passage.get("sequence"),
            "section_title": passage.get("section_title", ""),
            "locator": passage.get("locator", ""),
        }

    def _read(self, row: MaterialAnalysis) -> MaterialAnalysisRead:
        passages = {
            item["passage_id"]: item
            for item in row.result_json.get("passages", [])
            if isinstance(item, dict) and isinstance(item.get("passage_id"), str)
        }

        def citations(ids: list[str]) -> list[MaterialCitationRead]:
            return [
                MaterialCitationRead.model_validate(passages[item])
                for item in ids
                if item in passages
            ]

        result = row.result_json
        return MaterialAnalysisRead(
            id=row.id,
            goal_id=row.goal_id,
            query=row.query,
            status=row.status,
            summary=str(result.get("summary", "")),
            key_points=[
                MaterialKeyPointRead(
                    title=str(item.get("title", "")),
                    detail=str(item.get("detail", "")),
                    citations=citations(list(item.get("citation_ids", []))),
                )
                for item in result.get("key_points", [])
            ],
            findings=[
                MaterialFindingRead(
                    text=str(item.get("text", "")),
                    category=str(item.get("category", "OTHER")),
                    confidence=float(item.get("confidence", 0.5)),
                    status=str(item.get("status", "PROPOSED")),
                    citations=citations(list(item.get("citation_ids", []))),
                )
                for item in result.get("findings", [])
            ],
            open_questions=[str(item) for item in result.get("open_questions", [])],
            source_count=row.source_count,
            file_count=int(
                result.get("file_count", len({item["source_id"] for item in passages.values()}))
            ),
            conflicts=[
                MaterialConflictRead(
                    description=item["description"],
                    citations=citations(item.get("citation_ids", [])),
                )
                for item in result.get("conflicts", [])
            ],
            retrieval_coverage=[
                MaterialQueryCoverageRead.model_validate(item)
                for item in result.get("retrieval_coverage", [])
            ],
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _goal(self, goal_id: str) -> Goal:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(404, "Goal not found")
        return goal

from __future__ import annotations

import difflib

from fastapi import HTTPException
from sqlalchemy import func, select

from planner_api.models import MaterialChunk, MaterialRevision

from .schemas import MaterialDiffRead, MaterialReaderRead
from .service import MaterialService


class MaterialReadingService(MaterialService):
    def revisions(self, goal_id: str, source_id: str):
        self._active_link(goal_id, source_id)
        return [
            self._revision_read(row, None)
            for row in self.session.scalars(
                select(MaterialRevision)
                .where(MaterialRevision.source_id == source_id)
                .order_by(MaterialRevision.revision.desc())
            )
        ]

    def _version(self, goal_id: str, source_id: str, revision_id: str | None):
        self._active_link(goal_id, source_id)
        row = (
            self.session.get(MaterialRevision, revision_id)
            if revision_id
            else self._latest_revision(source_id)
        )
        if row is None or row.source_id != source_id:
            raise HTTPException(404, "资料版本不存在")
        if row.status != "READY":
            raise HTTPException(409, "资料版本尚未完成解析")
        return row

    def read(
        self, goal_id: str, source_id: str, revision_id=None, offset=0, limit=12000, outline=True
    ):
        row = self._version(goal_id, source_id, revision_id)
        total = (
            self.session.scalar(
                select(func.max(MaterialChunk.char_end)).where(MaterialChunk.revision_id == row.id)
            )
            or 0
        )
        end = min(total, offset + limit)
        cursor = min(offset, total)
        pieces = []
        for chunk in self.session.scalars(
            select(MaterialChunk)
            .where(
                MaterialChunk.revision_id == row.id,
                MaterialChunk.char_end > cursor,
                MaterialChunk.char_start < end,
            )
            .order_by(MaterialChunk.sequence)
        ):
            start = max(cursor, chunk.char_start)
            if start >= end:
                break
            if start > cursor:
                pieces.append("\n" * (start - cursor))
            stop = min(end, chunk.char_end)
            pieces.append(chunk.text[start - chunk.char_start : stop - chunk.char_start])
            cursor = stop
        headings = []
        if outline:
            previous = None
            for sequence, title, start, locations in self.session.execute(
                select(
                    MaterialChunk.sequence,
                    MaterialChunk.section_title,
                    MaterialChunk.char_start,
                    MaterialChunk.locator_json,
                )
                .where(MaterialChunk.revision_id == row.id)
                .order_by(MaterialChunk.sequence)
            ):
                if not title:
                    pages = [item.get("page") for item in locations if item.get("page")]
                    title = f"第 {pages[0]} 页" if pages else ""
                if not title:
                    continue
                if title != previous:
                    headings.append({"sequence": sequence, "title": title, "offset": start})
                    previous = title
                if len(headings) > 1000:
                    break
        material = next(m for m in self.list_goal_materials(goal_id) if m.source_id == source_id)
        metadata = row.metadata_json or {}
        warnings = list(metadata.get("warnings", []))
        if metadata.get("unread_pages"):
            warnings.append(f"未完整读取的扫描页：{metadata['unread_pages']}")
        return MaterialReaderRead(
            source_id=source_id,
            title=material.title,
            revision=self._revision_read(row, None),
            offset=offset,
            next_offset=end if end < total else None,
            total_chars=total,
            text="".join(pieces),
            outline=headings[:1000],
            outline_truncated=len(headings) > 1000,
            warnings=warnings,
        )

    def compare(self, goal_id: str, source_id: str, from_id: str, to_id: str):
        before = self.read(goal_id, source_id, from_id, limit=100000, outline=False)
        after = self.read(goal_id, source_id, to_id, limit=100000, outline=False)
        a, b = before.text.splitlines(), after.text.splitlines()
        coverage_truncated = (
            before.next_offset is not None
            or after.next_offset is not None
            or len(a) > 3000
            or len(b) > 3000
        )
        a, b = a[:3000], b[:3000]
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=True)
        lines = []
        added = removed = 0
        output_truncated = False
        for group in matcher.get_grouped_opcodes(n=2):
            for tag, i, j, k, stop in group:
                if tag in {"delete", "replace"}:
                    removed += j - i
                if tag in {"insert", "replace"}:
                    added += stop - k
                entries = []
                if tag == "equal":
                    entries = [
                        {
                            "kind": "context",
                            "text": value,
                            "old_line": i + n + 1,
                            "new_line": k + n + 1,
                        }
                        for n, value in enumerate(a[i:j])
                    ]
                else:
                    entries = [
                        {"kind": "removed", "text": value, "old_line": i + n + 1}
                        for n, value in enumerate(a[i:j])
                    ]
                    entries += [
                        {"kind": "added", "text": value, "new_line": k + n + 1}
                        for n, value in enumerate(b[k:stop])
                    ]
                for entry in entries:
                    if len(lines) < 500:
                        lines.append({**entry, "text": entry["text"][:2000]})
                    else:
                        output_truncated = True
                    output_truncated |= len(entry["text"]) > 2000
        return MaterialDiffRead(
            source_id=source_id,
            from_revision_id=from_id,
            to_revision_id=to_id,
            lines=lines,
            added_lines=added,
            removed_lines=removed,
            truncated=coverage_truncated or output_truncated,
            compared_chars=[len("\n".join(a)), len("\n".join(b))],
        )

    def index(self, goal_id: str, source_id: str):
        from .semantic import index_chunks

        row = self._version(goal_id, source_id, None)
        if not self.settings.material_semantic_enabled:
            raise HTTPException(409, "尚未启用本地语义检索")
        try:
            count = index_chunks(
                self.session,
                list(
                    self.session.scalars(
                        select(MaterialChunk).where(MaterialChunk.revision_id == row.id)
                    )
                ),
                self.settings,
            )
            row.metadata_json = {
                **row.metadata_json,
                "semantic_status": "READY",
                "embedding_model": self.settings.material_embedding_identity,
            }
            self.session.commit()
            return {"revision_id": row.id, "indexed_chunks": count, "status": "READY"}
        except Exception as error:
            self.session.rollback()
            raise HTTPException(503, "本地语义模型不可用，请检查模型缓存或预算。") from error

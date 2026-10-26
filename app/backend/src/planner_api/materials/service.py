from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from planner_api.config import Settings, get_settings
from planner_api.models import (
    Goal,
    GoalMaterialLink,
    MaterialProcessingJob,
    MaterialRevision,
    MaterialRoot,
    MaterialSource,
)

from .schemas import (
    GoalMaterialRead,
    MaterialRevisionRead,
    MaterialRootCreate,
    MaterialRootRead,
    MaterialRootScanRead,
    MaterialScanFileRead,
    MaterialBatchReferenceCreate,
    MaterialBatchReferenceRead,
    MaterialBatchReferenceError,
)

SUPPORTED_EXTENSIONS = {
    ".csv",
    ".docx",
    ".html",
    ".htm",
    ".json",
    ".md",
    ".markdown",
    ".pdf",
    ".pptx",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
}

EXCLUDED_SCAN_DIRECTORIES = {"node_modules", "dist", "build", "__pycache__", "coverage"}


class MaterialService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def list_roots(self) -> list[MaterialRootRead]:
        rows = self.session.scalars(select(MaterialRoot).order_by(MaterialRoot.created_at))
        return [self._root_read(row) for row in rows]

    def create_root(self, payload: MaterialRootCreate) -> MaterialRootRead:
        path = Path(payload.path).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise HTTPException(422, "本地资料目录不存在或无法访问") from error
        if not resolved.is_dir():
            raise HTTPException(422, "本地资料根路径必须是目录")
        canonical = str(resolved)
        row = self.session.scalar(
            select(MaterialRoot).where(MaterialRoot.canonical_path == canonical)
        )
        if row is None:
            row = MaterialRoot(display_name=payload.display_name, canonical_path=canonical)
            self.session.add(row)
        else:
            row.display_name = payload.display_name
            row.status = "ACTIVE"
        self.session.commit()
        return self._root_read(row)

    def disable_root(self, root_id: str) -> None:
        row = self.session.get(MaterialRoot, root_id)
        if row is None:
            raise HTTPException(404, "Material root not found")
        row.status = "DISABLED"
        self.session.commit()

    def scan_root(self, goal_id: str, root_id: str) -> MaterialRootScanRead:
        self._goal(goal_id)
        root = self._active_root(root_id)
        try:
            root_path = Path(root.canonical_path).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise HTTPException(422, "资料目录不存在或无法访问") from error
        if not root_path.is_dir():
            raise HTTPException(422, "资料根路径已不是目录")
        linked = {
            item.relative_path: item
            for item in self.list_goal_materials(goal_id)
            if item.root_id == root_id
        }
        result = MaterialRootScanRead(root_id=root_id, scanned_at=datetime.now(UTC))
        visited = 0

        def traversal_error(_error: OSError) -> None:
            result.truncated = True

        # Do not follow links/junctions. Cap traversal as well as the returned list
        # so a large workspace cannot turn a directory preview into an unbounded job.
        for directory, subdirs, names in os.walk(
            root_path, followlinks=False, onerror=traversal_error
        ):
            base = Path(directory)
            subdirs[:] = sorted(
                name
                for name in subdirs
                if not name.startswith(".")
                and name not in EXCLUDED_SCAN_DIRECTORIES
                and not (base / name).is_symlink()
                and not (base / name).is_junction()
            )
            visited += len(subdirs)
            for name in sorted(names):
                visited += 1
                if visited > 10000 or len(result.files) >= 2000:
                    result.truncated = True
                    break
                path = base / name
                if name.startswith(".") or path.is_symlink() or path.is_junction():
                    continue
                try:
                    path.resolve(strict=True).relative_to(root_path)
                    stat = path.stat()
                    if not path.is_file():
                        continue
                except (OSError, RuntimeError, ValueError):
                    result.truncated = True
                    continue
                extension = path.suffix.lower()
                reason = None
                if extension not in SUPPORTED_EXTENSIONS:
                    reason = "暂不支持此文件类型"
                elif stat.st_size > self.settings.material_max_file_bytes:
                    reason = "文件超过解析大小上限"
                relative = path.relative_to(root_path).as_posix()
                material = linked.get(relative)
                result.files.append(
                    MaterialScanFileRead(
                        relative_path=relative,
                        title=name,
                        extension=extension,
                        file_size=stat.st_size,
                        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                        supported=reason is None,
                        reason=reason,
                        linked=material is not None,
                        source_id=material.source_id if material else None,
                        revision_status=(
                            material.latest_revision.status
                            if material and material.latest_revision
                            else None
                        ),
                    )
                )
            if visited > 10000 or len(result.files) >= 2000:
                break
        return result

    def reference_local_files(
        self,
        goal_id: str,
        payload: MaterialBatchReferenceCreate,
    ) -> MaterialBatchReferenceRead:
        from .processing import MaterialProcessingService

        self._goal(goal_id)
        self._active_root(payload.root_id)
        result = MaterialBatchReferenceRead()
        processing = MaterialProcessingService(self.session, self.settings)
        for relative_path in payload.relative_paths:
            try:
                material = self.reference_local_file(
                    goal_id,
                    root_id=payload.root_id,
                    relative_path=relative_path,
                    scope=payload.scope,
                    usage_policy=payload.usage_policy,
                    pinned=payload.pinned,
                )
                processing.process_for_goal(goal_id, material.source_id)
                result.imported.append(material)
            except (HTTPException, OSError, RuntimeError) as error:
                self.session.rollback()
                message = (
                    str(error.detail)
                    if isinstance(error, HTTPException)
                    else "文件无法访问，请重试"
                )
                result.errors.append(
                    MaterialBatchReferenceError(
                        relative_path=relative_path,
                        message=message,
                    )
                )
        return result

    def upload_file(self, goal_id: str, filename: str, content: bytes) -> GoalMaterialRead:
        """Store an explicitly selected browser file in the app-owned material root."""
        self._goal(goal_id)
        if len(content) > self.settings.material_max_file_bytes:
            limit_mib = self.settings.material_max_file_bytes // (1024 * 1024)
            raise HTTPException(413, f"本地资料超过 {limit_mib} MiB 文件大小上限")
        normalized = self._normalize_uploaded_path(filename)
        extension = Path(normalized).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise HTTPException(422, f"暂不支持 {extension or '无扩展名'} 文件")
        root_path = Path(self.settings.material_upload_dir)
        app_root = root_path.parent
        app_root.mkdir(parents=True, exist_ok=True)
        self._check_upload_destination(app_root, root_path)
        root_path.mkdir(parents=True, exist_ok=True)
        root_path = root_path.resolve(strict=True)
        root = self.session.scalar(
            select(MaterialRoot).where(MaterialRoot.canonical_path == str(root_path))
        )
        if root is None:
            root = MaterialRoot(display_name="对话上传资料", canonical_path=str(root_path))
            self.session.add(root)
            self.session.flush()
        else:
            root.status = "ACTIVE"
        stored_relative = f"goal-{goal_id}/{normalized}"
        stored_path = root_path / stored_relative
        # Reject existing symlinks/junctions before mkdir or temporary writes.
        self._check_upload_destination(root_path, stored_path)
        stored_path.parent.mkdir(parents=True, exist_ok=True)
        self._check_upload_destination(root_path, stored_path)
        with tempfile.NamedTemporaryFile(dir=stored_path.parent, delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        try:
            self._check_upload_destination(root_path, stored_path)
            os.replace(temporary_path, stored_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        material = self.reference_local_file(
            goal_id,
            root_id=root.id,
            relative_path=stored_relative,
            scope="ALL",
            usage_policy="MANUAL_ONLY",
            pinned=False,
        )
        from .processing import MaterialProcessingService

        MaterialProcessingService(self.session, self.settings).process_for_goal(
            goal_id, material.source_id
        )
        return next(
            item
            for item in self.list_goal_materials(goal_id)
            if item.source_id == material.source_id
        )

    @staticmethod
    def _check_upload_destination(root: Path, destination: Path):
        try:
            destination.relative_to(root)
            destination.resolve().relative_to(root.resolve(strict=True))
            for part in (destination, *destination.parents):
                if part == root:
                    break
                if part.is_symlink() or part.is_junction():
                    raise ValueError("Linked upload directory")
        except (ValueError, OSError) as error:
            raise HTTPException(422, "上传目录包含越界路径或目录链接") from error

    @staticmethod
    def _normalize_uploaded_path(filename: str) -> str:
        value = filename.replace("\\", "/").strip()
        if value.startswith("/") or any(ord(char) < 32 for char in value):
            raise HTTPException(422, "上传文件名包含非法路径")
        parts = [part for part in value.split("/") if part not in {"", "."}]
        if not parts or any(part == ".." or any(char in part for char in ':*?"<>|')
                            or part.endswith((".", " ")) or Path(part).is_reserved()
                            for part in parts):
            raise HTTPException(422, "上传文件名包含非法路径")
        normalized = "/".join(parts)
        if len(normalized) > 1800:
            raise HTTPException(422, "上传文件路径过长")
        return normalized

    def list_goal_materials(self, goal_id: str) -> list[GoalMaterialRead]:
        self._goal(goal_id)
        links = list(
            self.session.scalars(
                select(GoalMaterialLink)
                .where(GoalMaterialLink.goal_id == goal_id)
                .order_by(GoalMaterialLink.created_at)
            )
        )
        return [self._material_read(link) for link in links]

    def reference_local_file(
        self,
        goal_id: str,
        *,
        root_id: str,
        relative_path: str,
        scope: str,
        usage_policy: str,
        pinned: bool,
    ) -> GoalMaterialRead:
        self._goal(goal_id)
        root = self._active_root(root_id)
        file_path, normalized_relative = self._resolve_local_file(root, relative_path)
        fingerprint = self._fingerprint(file_path)
        source = self.session.scalar(
            select(MaterialSource).where(
                MaterialSource.root_id == root.id,
                MaterialSource.relative_path == normalized_relative,
            )
        )
        if source is None:
            mime_type, _ = mimetypes.guess_type(file_path.name)
            source = MaterialSource(
                root_id=root.id,
                relative_path=normalized_relative,
                canonical_uri=f"local-material://{root.id}/{normalized_relative}",
                title=file_path.name,
                mime_type=mime_type or "application/octet-stream",
            )
            self.session.add(source)
            self.session.flush()
        else:
            source.status = "ACTIVE"
            source.title = file_path.name

        latest = self._latest_revision(source.id)
        if latest is None or latest.content_hash != fingerprint["content_hash"]:
            revision_number = (latest.revision + 1) if latest else 1
            latest = MaterialRevision(
                source_id=source.id,
                revision=revision_number,
                content_hash=fingerprint["content_hash"],
                file_size=fingerprint["file_size"],
                source_modified_at=fingerprint["source_modified_at"],
                status="QUEUED",
                metadata_json={"extension": file_path.suffix.lower()},
            )
            self.session.add(latest)
            self.session.flush()
            from .parse_cache import MaterialParseCache

            MaterialParseCache(self.session, self.settings).restore(latest)

        link = self.session.scalar(
            select(GoalMaterialLink).where(
                GoalMaterialLink.goal_id == goal_id,
                GoalMaterialLink.source_id == source.id,
            )
        )
        if link is None:
            link = GoalMaterialLink(goal_id=goal_id, source_id=source.id)
            self.session.add(link)
        link.scope = scope
        link.usage_policy = usage_policy
        link.pinned = pinned
        link.status = "ACTIVE"
        self.session.commit()
        return self._material_read(link)

    def refresh_local_file(self, goal_id: str, source_id: str) -> GoalMaterialRead:
        link = self._active_link(goal_id, source_id)
        source = self.session.get(MaterialSource, source_id)
        if source is None or source.kind != "LOCAL_FILE" or not source.root_id:
            raise HTTPException(422, "Only local file materials can be refreshed")
        if not source.relative_path:
            raise HTTPException(409, "Local material path is missing")
        return self.reference_local_file(
            goal_id,
            root_id=source.root_id,
            relative_path=source.relative_path,
            scope=link.scope,
            usage_policy=link.usage_policy,
            pinned=link.pinned,
        )

    def unlink_goal_material(self, goal_id: str, source_id: str) -> None:
        link = self._active_link(goal_id, source_id)
        self.session.delete(link)
        self.session.commit()

    def _goal(self, goal_id: str) -> Goal:
        goal = self.session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(404, "Goal not found")
        return goal

    def _active_root(self, root_id: str) -> MaterialRoot:
        root = self.session.get(MaterialRoot, root_id)
        if root is None:
            raise HTTPException(404, "Material root not found")
        if root.status != "ACTIVE":
            raise HTTPException(409, "Material root is disabled")
        return root

    def _active_link(self, goal_id: str, source_id: str) -> GoalMaterialLink:
        self._goal(goal_id)
        link = self.session.scalar(
            select(GoalMaterialLink).where(
                GoalMaterialLink.goal_id == goal_id,
                GoalMaterialLink.source_id == source_id,
                GoalMaterialLink.status == "ACTIVE",
            )
        )
        if link is None:
            raise HTTPException(404, "Material is not linked to this goal")
        return link

    @staticmethod
    def _resolve_local_file(root: MaterialRoot, relative_path: str) -> tuple[Path, str]:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise HTTPException(422, "只能使用资料根目录内的相对路径")
        root_path = Path(root.canonical_path).resolve(strict=True)
        try:
            resolved = (root_path / candidate).resolve(strict=True)
            normalized = resolved.relative_to(root_path).as_posix()
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(422, "文件不存在或超出已授权资料目录") from error
        if not resolved.is_file():
            raise HTTPException(422, "资料路径必须指向文件")
        if resolved.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise HTTPException(422, f"暂不支持 {resolved.suffix or '无扩展名'} 文件")
        return resolved, normalized

    def _fingerprint(self, path: Path) -> dict[str, object]:
        before = path.stat()
        if before.st_size > self.settings.material_max_file_bytes:
            limit_mib = self.settings.material_max_file_bytes // (1024 * 1024)
            raise HTTPException(413, f"本地资料超过 {limit_mib} MiB 文件大小上限")
        digest = hashlib.sha256()
        total_bytes = 0
        with path.open("rb") as stream:
            while block := stream.read(
                min(
                    self.settings.material_hash_chunk_bytes,
                    self.settings.material_max_file_bytes - total_bytes + 1,
                )
            ):
                total_bytes += len(block)
                if total_bytes > self.settings.material_max_file_bytes:
                    limit_mib = self.settings.material_max_file_bytes // (1024 * 1024)
                    raise HTTPException(413, f"本地资料超过 {limit_mib} MiB 文件大小上限")
                digest.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise HTTPException(409, "文件在读取过程中发生变化，请重试")
        return {
            "content_hash": digest.hexdigest(),
            "file_size": after.st_size,
            "source_modified_at": datetime.fromtimestamp(after.st_mtime, tz=UTC),
        }

    def _latest_revision(self, source_id: str) -> MaterialRevision | None:
        return self.session.scalar(
            select(MaterialRevision)
            .where(MaterialRevision.source_id == source_id)
            .order_by(MaterialRevision.revision.desc())
        )

    def _material_read(self, link: GoalMaterialLink) -> GoalMaterialRead:
        source = self.session.get(MaterialSource, link.source_id)
        if source is None:
            raise HTTPException(409, "Linked material source is missing")
        latest = self._latest_revision(source.id)
        job = (
            self.session.scalar(
                select(MaterialProcessingJob).where(MaterialProcessingJob.revision_id == latest.id)
            )
            if latest is not None
            else None
        )
        return GoalMaterialRead(
            link_id=link.id,
            source_id=source.id,
            kind=source.kind,
            title=source.title,
            mime_type=source.mime_type,
            root_id=source.root_id,
            relative_path=source.relative_path,
            canonical_uri=source.canonical_uri,
            source_status=source.status,
            scope=link.scope,
            usage_policy=link.usage_policy,
            pinned=link.pinned,
            link_status=link.status,
            latest_revision=self._revision_read(latest, job) if latest else None,
        )

    @staticmethod
    def _root_read(row: MaterialRoot) -> MaterialRootRead:
        return MaterialRootRead(
            id=row.id,
            display_name=row.display_name,
            path=row.canonical_path,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _revision_read(
        row: MaterialRevision, job: MaterialProcessingJob | None = None
    ) -> MaterialRevisionRead:
        return MaterialRevisionRead(
            id=row.id,
            revision=row.revision,
            content_hash=row.content_hash,
            file_size=row.file_size,
            source_modified_at=row.source_modified_at,
            parser_name=row.parser_name,
            parser_version=row.parser_version,
            status=row.status,
            metadata=dict(row.metadata_json or {}),
            processing=(
                {
                    "status": job.status,
                    "attempts": job.attempts,
                    "queued_at": job.queued_at,
                    "started_at": job.started_at,
                    "completed_at": job.completed_at,
                    "queue_wait_ms": job.queue_wait_ms,
                    "processing_duration_ms": job.processing_duration_ms,
                    "cancellation_requested": job.cancel_requested_at is not None,
                }
                if job is not None
                else None
            ),
            error_code=row.error_code,
            error_message=row.error_message,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )

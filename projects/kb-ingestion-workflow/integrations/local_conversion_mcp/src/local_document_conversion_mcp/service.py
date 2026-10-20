from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import time
import unicodedata
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .config import ServiceSettings
from .errors import ServiceError
from .ledger import Ledger


ROUTES = {
    ".zip": "image_zip_local_ocr",
    ".pdf": "local_mineru",
    ".jpg": "local_mineru",
    ".jpeg": "local_mineru",
    ".png": "local_mineru",
    ".tif": "local_mineru",
    ".tiff": "local_mineru",
    ".webp": "local_mineru",
    ".heic": "heic_to_local_mineru",
    ".heif": "heic_to_local_mineru",
    ".doc": "doc_to_docx",
    ".docx": "docx_direct",
    ".ppt": "manual_review",
    ".pptx": "pptx_text_extract",
    ".xls": "manual_review",
    ".xlsx": "xlsx_direct",
    ".txt": "text_direct",
    ".md": "text_direct",
    ".markdown": "text_direct",
    ".csv": "text_direct",
    ".webarchive": "webarchive_offline",
}

DUPLICATE_ACTIONS = {"not_applicable", "reuse_existing", "convert_as_new_version", "cancel"}
DUPLICATE_CONFIRMATION_KINDS = {"exact", "same_content", "possible_new_version"}
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{8,160}\Z")
DOCUMENT_LANGUAGES = {
    "ch", "ch_server", "ch_lite", "en", "korean", "japan", "chinese_cht",
    "ta", "te", "ka", "th", "el", "latin", "arabic", "east_slavic", "cyrillic", "devanagari",
}
DOCUMENT_PROFILES = {
    "general_text": "auto",
    "digital_text": "txt",
    "scanned_text": "ocr",
}


class ConversionService:
    def __init__(self, settings: ServiceSettings, *, custody_resolver=None, recover_interrupted=True,
                 custody_pdf_route="pdf_text_local") -> None:
        if custody_pdf_route not in {"pdf_text_local", "pdf_mineru_local"}:
            raise ValueError("unsupported custody PDF route")
        self.custody_pdf_route = custody_pdf_route
        self.settings = settings
        self.ledger = Ledger(settings.ledger_path)
        self.custody_resolver = custody_resolver
        self.recovered_task_count = self.ledger.recover_interrupted_tasks(updated_at=_utc_now().isoformat()) if recover_interrupted else 0

    def inspect_submission(self, *, source_kind: str, source_relpath: str, topic: str | None = None) -> dict[str, Any]:
        request_id = _new_id("req")
        source = self._resolve_source(source_kind, source_relpath)
        route = ROUTES.get(source.suffix.lower())
        if source_kind == "custody_v21" and source.suffix.lower() == ".pdf":
            route = self.custody_pdf_route
        if source_kind == "custody_v21" and source.suffix.lower() == ".zip":
            route = "markdown_bundle_offline"
        if route is None:
            raise ServiceError("UNSUPPORTED_FORMAT", f"当前不支持文件格式：{source.suffix or '无扩展名'}")

        stat = source.stat()
        if stat.st_size > self.settings.maximum_source_bytes:
            raise ServiceError("SOURCE_TOO_LARGE", "文件超过本机转换服务允许的单文件上限，任务未创建")
        sha256 = _sha256_file(source)
        normalized = _normalize_filename(source.name)
        matches = self.ledger.find_documents(normalized_filename=normalized, sha256=sha256)
        duplicate = _classify_duplicates(matches, normalized_filename=normalized, sha256=sha256)
        now = _utc_now()
        inspection_id = _new_id("inspect")
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        record = {
            "inspection_id": inspection_id,
            "source_kind": source_kind,
            "source_relpath": PurePosixPath(source_relpath).as_posix(),
            "filename": source.name,
            "normalized_filename": normalized,
            "media_type": media_type,
            "size_bytes": stat.st_size,
            "sha256": sha256,
            "route": route,
            "stat_device": stat.st_dev,
            "stat_inode": stat.st_ino,
            "stat_mtime_ns": stat.st_mtime_ns,
            "duplicate": duplicate,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self.settings.inspection_ttl_seconds)).isoformat(),
        }
        self.ledger.create_inspection(record)
        next_action = "confirm_duplicate" if duplicate["requires_confirmation"] else "submit_conversion"
        allowed_duplicate_actions = (
            ["reuse_existing", "convert_as_new_version", "cancel"]
            if duplicate["requires_confirmation"]
            else ["not_applicable", "cancel"]
        )
        return {
            "schema_version": "local-document-conversion/v1",
            "ok": True,
            "request_id": request_id,
            "task_id": None,
            "status": "awaiting_duplicate_confirmation" if duplicate["requires_confirmation"] else "planned",
            "message_zh": _inspection_message(duplicate, source.name),
            "next_action": next_action,
            "allowed_duplicate_actions": allowed_duplicate_actions,
            "inspection_id": inspection_id,
            "source": {
                "filename": source.name,
                "media_type": media_type,
                "size_bytes": stat.st_size,
                "sha256": sha256,
                "route": route,
                "topic": topic,
            },
            "duplicate": duplicate,
        }

    def submit_conversion(
        self,
        *,
        inspection_id: str,
        duplicate_action: str,
        requested_by: str,
        idempotency_key: str,
        document_language: str = "ch",
        document_profile: str = "general_text",
    ) -> dict[str, Any]:
        request_id = _new_id("req")
        if duplicate_action not in DUPLICATE_ACTIONS:
            raise ServiceError("INVALID_DUPLICATE_ACTION", "重复处理选择无效")
        if not requested_by or len(requested_by) > 128:
            raise ServiceError("INVALID_REQUESTED_BY", "任务调用方标识无效")
        if not IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
            raise ServiceError(
                "INVALID_IDEMPOTENCY_KEY",
                "幂等键必须为 8—160 位，只能包含字母、数字、点、下划线、冒号或连字符",
            )
        if document_language not in DOCUMENT_LANGUAGES:
            raise ServiceError("INVALID_DOCUMENT_LANGUAGE", "文档语言不在本机 MinerU 允许范围内")
        if document_profile not in DOCUMENT_PROFILES:
            raise ServiceError("INVALID_DOCUMENT_PROFILE", "文档类型不在本机转换服务允许范围内")
        parser_backend = "pipeline"
        parse_method = DOCUMENT_PROFILES[document_profile]

        existing = self.ledger.get_task_by_idempotency(
            requested_by=requested_by,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            self._verify_idempotent_replay(
                existing,
                inspection_id=inspection_id,
                duplicate_action=duplicate_action,
                document_language=document_language,
                document_profile=document_profile,
            )
            return self._task_result(existing["task_id"], request_id=request_id, idempotent_replay=True)

        inspection = self.ledger.get_inspection(inspection_id)
        if inspection is None:
            raise ServiceError("INSPECTION_NOT_FOUND", "没有找到投递检查记录，请重新检查文件")
        if inspection['route'] == 'image_zip_local_ocr':
            parse_method = 'ocr'
        if inspection['route'] == 'pdf_text_local':
            if document_profile == 'scanned_text':
                raise ServiceError('PDF_OCR_UNAVAILABLE', '本轮 PDF 路线仅抽取本机文本层；尚未验证 OCR')
            parse_method = 'txt'
        if datetime.fromisoformat(inspection["expires_at"]) <= _utc_now():
            raise ServiceError("INSPECTION_EXPIRED", "投递检查已经过期，请重新检查文件")

        source = self._resolve_source(inspection["source_kind"], inspection["source_relpath"])
        self._verify_source_unchanged(source, inspection)
        duplicate = inspection["duplicate"]
        requires_confirmation = duplicate["kind"] in DUPLICATE_CONFIRMATION_KINDS
        if requires_confirmation and duplicate_action == "not_applicable":
            raise ServiceError("DUPLICATE_CONFIRMATION_REQUIRED", "检测到已有文档，请先选择复用、重新转换或取消")
        if not requires_confirmation and duplicate_action not in {"not_applicable", "cancel"}:
            raise ServiceError("INVALID_DUPLICATE_ACTION", "当前文件没有需要复用或作为新版本处理的重复项")

        task_id = _new_id("task")
        now = _utc_now().isoformat()
        reused_from: str | None = None
        if duplicate_action == "cancel":
            status = "cancelled"
            message = "已取消，本机没有启动转换任务"
        elif duplicate_action == "reuse_existing":
            matches = duplicate.get("matches", [])
            if not matches:
                raise ServiceError("DUPLICATE_MATCH_NOT_FOUND", "没有可复用的既有转换结果")
            reused_from = str(matches[0]["last_task_id"])
            status = "ready_candidate"
            message = f"已复用既有候选资料包：{matches[0]['document_name']}"
        else:
            self._check_storage_capacity()
            status = "queued"
            message = "任务已进入本机转换队列"

        task, replayed = self.ledger.create_task_idempotent(
            {
                "task_id": task_id,
                "inspection_id": inspection_id,
                "idempotency_key": idempotency_key,
                "status": status,
                "duplicate_action": duplicate_action,
                "reused_from_task_id": reused_from,
                "requested_by": requested_by,
                "created_at": now,
                "updated_at": now,
                "message_zh": message,
                "document_language": document_language,
                "document_profile": document_profile,
                "parser_backend": parser_backend,
                "parse_method": parse_method,
            }
        )
        if replayed:
            self._verify_idempotent_replay(
                task,
                inspection_id=inspection_id,
                duplicate_action=duplicate_action,
                document_language=document_language,
                document_profile=document_profile,
            )
        return self._task_result(task["task_id"], request_id=request_id, idempotent_replay=replayed)

    def confirm_duplicate_submission(
        self,
        *,
        source_kind: str,
        source_relpath: str,
        duplicate_action: str,
        requested_by: str,
        idempotency_key: str,
        document_language: str = "ch",
        document_profile: str = "general_text",
    ) -> dict[str, Any]:
        """Re-inspect and submit one confirmed duplicate as one service operation."""
        if duplicate_action not in {"reuse_existing", "convert_as_new_version", "cancel"}:
            raise ServiceError(
                "INVALID_DUPLICATE_ACTION",
                "重复确认只能选择复用已有成果、作为新版本转换或取消",
            )
        normalized_relpath = _validate_relpath(source_relpath).as_posix()

        existing = self.ledger.get_task_by_idempotency(
            requested_by=requested_by,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            original = self.ledger.get_inspection(existing["inspection_id"])
            if (
                original is None
                or original["source_kind"] != source_kind
                or original["source_relpath"] != normalized_relpath
            ):
                raise ServiceError(
                    "IDEMPOTENCY_CONFLICT",
                    "该幂等键已用于另一份来源文件，请更换幂等键后重试",
                )
            return self.submit_conversion(
                inspection_id=existing["inspection_id"],
                duplicate_action=duplicate_action,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                document_language=document_language,
                document_profile=document_profile,
            )

        inspection = self.inspect_submission(
            source_kind=source_kind,
            source_relpath=normalized_relpath,
        )
        if not inspection["duplicate"]["requires_confirmation"]:
            raise ServiceError(
                "DUPLICATE_STATE_CHANGED",
                "文件的重复状态已经变化，请重新发起转换任务",
            )
        return self.submit_conversion(
            inspection_id=inspection["inspection_id"],
            duplicate_action=duplicate_action,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            document_language=document_language,
            document_profile=document_profile,
        )

    def get_task_status(self, *, task_id: str) -> dict[str, Any]:
        return self._task_result(task_id, request_id=_new_id("req"))

    def wait_task_update(
        self,
        *,
        task_id: str,
        after_updated_at: str | None = None,
        timeout_seconds: int = 20,
    ) -> dict[str, Any]:
        if timeout_seconds < 1 or timeout_seconds > 55:
            raise ServiceError("INVALID_WAIT_TIMEOUT", "等待时间必须为 1—55 秒")
        terminal = {"ready_candidate", "needs_review", "failed", "cancelled"}
        deadline = time.monotonic() + timeout_seconds
        while True:
            task = self.ledger.get_task(task_id)
            if task is None:
                raise ServiceError("TASK_NOT_FOUND", "没有找到该任务")
            changed = after_updated_at is None or task["updated_at"] != after_updated_at
            if changed or task["status"] in terminal or time.monotonic() >= deadline:
                result = self._task_result(task_id, request_id=_new_id("req"))
                result["changed"] = changed
                result["wait_timed_out"] = not changed and task["status"] not in terminal
                return result
            time.sleep(0.25)

    def cancel_task(self, *, task_id: str, reason_zh: str | None = None) -> dict[str, Any]:
        task = self.ledger.get_task(task_id)
        if task is None:
            raise ServiceError("TASK_NOT_FOUND", "没有找到该任务")
        if task["status"] == "queued":
            message = "任务已取消，未删除原文件或历史结果"
            if reason_zh:
                message = f"{message}；原因：{reason_zh[:160]}"
            self.ledger.update_task_status(
                task_id,
                status="cancelled",
                message_zh=message,
                updated_at=_utc_now().isoformat(),
            )
        elif task["status"] in {"preparing", "preprocessing", "converting", "quality_checking"}:
            message = "已请求取消，正在安全停止本机转换"
            if reason_zh:
                message = f"{message}；原因：{reason_zh[:160]}"
            self.ledger.request_cancel(task_id, message_zh=message, updated_at=_utc_now().isoformat())
        elif task["status"] not in {"cancelled", "ready_candidate", "needs_review", "failed"}:
            raise ServiceError("TASK_NOT_CANCELLABLE", "当前阶段暂不支持取消")
        return self._task_result(task_id, request_id=_new_id("req"))

    def get_quality_report(self, *, task_id: str) -> dict[str, Any]:
        task = self._require_terminal_task(task_id)
        effective = self._effective_task(task)
        quality = _decode_json(effective.get("quality_json"))
        if quality is None:
            raise ServiceError("QUALITY_REPORT_NOT_READY", "该任务尚无可读取的质量报告")
        return {
            "schema_version": "local-document-conversion/v1",
            "ok": True,
            "request_id": _new_id("req"),
            "task_id": task_id,
            "status": task["status"],
            "message_zh": "已读取本机质量报告",
            "next_action": "list_result_files" if task["status"] == "ready_candidate" else None,
            "quality": quality,
        }

    def list_result_files(self, *, task_id: str) -> dict[str, Any]:
        task = self._require_terminal_task(task_id)
        effective = self._effective_task(task)
        artifacts = self.ledger.list_artifacts(effective["task_id"])
        return {
            "schema_version": "local-document-conversion/v1",
            "ok": True,
            "request_id": _new_id("req"),
            "task_id": task_id,
            "status": task["status"],
            "message_zh": f"找到 {len(artifacts)} 个允许取回的候选成果",
            "next_action": "prepare_delivery" if artifacts else None,
            "files": [_public_artifact(item) for item in artifacts],
        }

    def prepare_delivery(self, *, task_id: str, delivery_mode: str = "recommended") -> dict[str, Any]:
        if delivery_mode not in {"recommended", "package", "report_only"}:
            raise ServiceError("INVALID_DELIVERY_MODE", "交付模式无效")
        task = self._require_terminal_task(task_id)
        if task.get('error_code') == 'IMAGE_ZIP_CONVERSION_FAILED' and delivery_mode != 'report_only':
            raise ServiceError('IMAGE_ZIP_CONVERSION_FAILED', '图片文章转换未完整完成，目前只可取回质量报告，不能交付候选文章')
        effective = self._effective_task(task)
        artifacts = self.ledger.list_artifacts(effective["task_id"])
        if delivery_mode == "report_only":
            artifacts = [item for item in artifacts if item["purpose"] == "quality_report"]
        if not artifacts:
            raise ServiceError("NO_DELIVERABLE_FILES", "该任务没有可交付成果")

        batch = self._resolve_task_batch(effective)
        delivery_dir = self.settings.delivery_root / task_id
        delivery_dir.mkdir(parents=True, exist_ok=True)
        package_required = delivery_mode == "package" or (
            delivery_mode == "recommended" and (len(artifacts) > 3 or any(item["size_bytes"] > 20 * 1024**2 for item in artifacts))
        )
        delivered: list[dict[str, Any]] = []
        if package_required:
            package = delivery_dir / f"{task_id}_候选资料包.zip"
            with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for item in artifacts:
                    source = _resolve_artifact_path(batch, item["relative_path"])
                    archive.write(source, arcname=item["relative_path"])
            delivered.append(_delivery_record(package, self.settings.delivery_root, self.settings.maximum_delivery_bytes))
        else:
            for item in artifacts:
                source = _resolve_artifact_path(batch, item["relative_path"])
                destination = delivery_dir / Path(item["relative_path"]).name
                if not destination.exists() or _sha256_file(destination) != item["sha256"]:
                    shutil.copy2(source, destination)
                delivered.append(
                    _delivery_record(destination, self.settings.delivery_root, self.settings.maximum_delivery_bytes)
                )
        sendable = all(item["feishu_sendable"] for item in delivered)
        return {
            "schema_version": "local-document-conversion/v1",
            "ok": True,
            "request_id": _new_id("req"),
            "task_id": task_id,
            "status": "delivery_ready" if sendable else "delivery_too_large",
            "message_zh": "候选成果已准备到飞书交付目录" if sendable else "候选成果已打包，但超过当前飞书交付上限",
            "next_action": None,
            "delivery_files": delivered,
        }

    def _task_result(
        self,
        task_id: str,
        *,
        request_id: str,
        idempotent_replay: bool = False,
    ) -> dict[str, Any]:
        task = self.ledger.get_task(task_id)
        if task is None:
            raise ServiceError("TASK_NOT_FOUND", "没有找到该任务")
        return {
            "schema_version": "local-document-conversion/v1",
            "ok": True,
            "request_id": request_id,
            "task_id": task_id,
            "status": task["status"],
            "message_zh": task["message_zh"],
            "next_action": _next_action(task["status"]),
            "progress_percent": task.get("progress_percent") if task["progress_percent"] is not None else (0 if task["status"] == "queued" else None),
            "current_stage": task.get("current_stage") or task["status"],
            "queue_position": self.ledger.count_queued_before(task_id) + 1 if task["status"] == "queued" else None,
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "reused_from_task_id": task["reused_from_task_id"],
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "error_code": task.get("error_code"),
            "next_poll_after_seconds": 10 if task["status"] in {"queued", "preparing", "preprocessing", "converting", "quality_checking", "cancelling"} else None,
            "idempotent_replay": idempotent_replay,
            "conversion_profile": {
                "document_language": task.get("document_language") or "ch",
                "document_profile": task.get("document_profile") or "general_text",
                "parser_backend": task.get("parser_backend") or "pipeline",
                "parse_method": task.get("parse_method") or "auto",
            },
        }

    def _require_terminal_task(self, task_id: str) -> dict[str, Any]:
        task = self.ledger.get_task(task_id)
        if task is None:
            raise ServiceError("TASK_NOT_FOUND", "没有找到该任务")
        if task["status"] not in {"ready_candidate", "needs_review", "failed", "cancelled"}:
            raise ServiceError("TASK_NOT_FINISHED", "任务尚未结束，请稍后查询状态")
        return task

    def _effective_task(self, task: dict[str, Any]) -> dict[str, Any]:
        reused = task.get("reused_from_task_id")
        if not reused:
            return task
        source = self.ledger.get_task(reused)
        if source is None:
            raise ServiceError("REUSED_TASK_NOT_FOUND", "既有候选资料包记录已不存在")
        return source

    def _resolve_task_batch(self, task: dict[str, Any]) -> Path:
        relpath = task.get("batch_relpath")
        if not relpath:
            raise ServiceError("RESULT_NOT_READY", "任务还没有生成候选资料包")
        rel = _validate_relpath(relpath)
        root = self.settings.output_root.resolve(strict=True)
        candidate = root.joinpath(*rel.parts).resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ServiceError("RESULT_OUTSIDE_ALLOWED_ROOT", "成果路径超出允许目录") from exc
        if not candidate.is_dir():
            raise ServiceError("RESULT_NOT_READY", "候选资料包目录不可用")
        return candidate

    def _verify_idempotent_replay(
        self,
        task: dict[str, Any],
        *,
        inspection_id: str,
        duplicate_action: str,
        document_language: str,
        document_profile: str,
    ) -> None:
        if (
            task["inspection_id"] != inspection_id
            or task["duplicate_action"] != duplicate_action
            or (task.get("document_language") or "ch") != document_language
            or (task.get("document_profile") or "general_text") != document_profile
        ):
            raise ServiceError(
                "IDEMPOTENCY_CONFLICT",
                "该幂等键已用于另一组任务参数，请更换幂等键后重试",
            )

    def _resolve_source(self, source_kind: str, source_relpath: str) -> Path:
        if source_kind == "custody_v21":
            if self.custody_resolver is None:
                raise ServiceError("CUSTODY_REQUIRED", "V2.1 转换需要已接管原件绑定")
            return self.custody_resolver(source_relpath)
        root_config = self.settings.source_roots.get(source_kind)
        if root_config is None:
            raise ServiceError("SOURCE_OUTSIDE_ALLOWED_ROOT", "来源类型不在允许范围内")
        rel = _validate_relpath(source_relpath)
        try:
            root = root_config.path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ServiceError("SOURCE_NOT_FOUND", "投递目录当前不可用") from exc
        candidate = root.joinpath(*rel.parts)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ServiceError("SOURCE_NOT_FOUND", "没有找到文件，或文件尚未完整下载") from exc
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ServiceError("SOURCE_OUTSIDE_ALLOWED_ROOT", "文件路径超出允许目录") from exc
        if not resolved.is_file():
            raise ServiceError("SOURCE_NOT_FOUND", "来源不是可处理的普通文件")
        return resolved

    def _verify_source_unchanged(self, source: Path, inspection: dict[str, Any]) -> None:
        stat = source.stat()
        stable = (
            stat.st_dev == inspection["stat_device"]
            and stat.st_ino == inspection["stat_inode"]
            and stat.st_mtime_ns == inspection["stat_mtime_ns"]
            and stat.st_size == inspection["size_bytes"]
        )
        if not stable or _sha256_file(source) != inspection["sha256"]:
            raise ServiceError("SOURCE_CHANGED_AFTER_INSPECTION", "检查后文件发生变化，请重新提交检查")

    def _check_storage_capacity(self) -> None:
        self.settings.output_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.settings.output_root).free
        if free < self.settings.minimum_free_bytes:
            raise ServiceError("STORAGE_CAPACITY_LOW", "本机剩余空间低于安全阈值，任务未启动")


def _validate_relpath(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise ServiceError("SOURCE_OUTSIDE_ALLOWED_ROOT", "来源路径格式无效")
    rel = PurePosixPath(value)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ServiceError("SOURCE_OUTSIDE_ALLOWED_ROOT", "只允许投递目录中的安全相对路径")
    return rel


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return " ".join(normalized.split())


def _classify_duplicates(
    matches: list[dict[str, Any]], *, normalized_filename: str, sha256: str
) -> dict[str, Any]:
    exact = [row for row in matches if row["sha256"] == sha256 and row["normalized_filename"] == normalized_filename]
    same_content = [row for row in matches if row["sha256"] == sha256 and row not in exact]
    new_version = [row for row in matches if row["normalized_filename"] == normalized_filename and row["sha256"] != sha256]
    if exact:
        kind, selected = "exact", exact
    elif same_content:
        kind, selected = "same_content", same_content
    elif new_version:
        kind, selected = "possible_new_version", new_version
    else:
        kind, selected = "none", []
    public_matches = [
        {
            "document_name": row["filename"],
            "relationship": {
                "exact": "同名同内容",
                "same_content": "内容相同但文件名不同",
                "possible_new_version": "文件名相同但内容不同，可能是新版本",
            }.get(kind, "名称相似"),
            "last_task_id": row["task_id"],
            "candidate_location_label": row["candidate_location_label"],
            "archive_location_label": row["archive_location_label"],
            "updated_at": row["updated_at"],
        }
        for row in selected
    ]
    return {
        "kind": kind,
        "requires_confirmation": kind in DUPLICATE_CONFIRMATION_KINDS,
        "matches": public_matches,
    }


def _inspection_message(duplicate: dict[str, Any], filename: str) -> str:
    if duplicate["requires_confirmation"]:
        match = duplicate["matches"][0]
        location = match["archive_location_label"] or match["candidate_location_label"]
        return f"检测到可能重复的文件：{match['document_name']}（{location}），请确认是否仍要转换"
    return f"文件检查通过：{filename}"


def _next_action(status: str) -> str | None:
    return {
        "queued": "wait_task_update",
        "preparing": "wait_task_update",
        "preprocessing": "wait_task_update",
        "converting": "wait_task_update",
        "quality_checking": "wait_task_update",
        "cancelling": "wait_task_update",
        "ready_candidate": "list_result_files",
        "needs_review": "get_quality_report",
    }.get(status)


def _decode_json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _public_artifact(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": item["artifact_id"],
        "display_name": item["display_name"],
        "relative_path": item["relative_path"],
        "media_type": item["media_type"],
        "size_bytes": item["size_bytes"],
        "sha256": item["sha256"],
        "purpose": item["purpose"],
        "feishu_sendable": bool(item["feishu_sendable"]),
    }


def _resolve_artifact_path(batch: Path, relative_path: str) -> Path:
    rel = _validate_relpath(relative_path)
    candidate = batch.joinpath(*rel.parts).resolve(strict=True)
    try:
        candidate.relative_to(batch.resolve(strict=True))
    except ValueError as exc:
        raise ServiceError("RESULT_OUTSIDE_ALLOWED_ROOT", "成果文件路径超出允许目录") from exc
    if not candidate.is_file():
        raise ServiceError("RESULT_NOT_READY", "成果文件当前不可用")
    return candidate


def _delivery_record(path: Path, delivery_root: Path, maximum_bytes: int) -> dict[str, Any]:
    size = path.stat().st_size
    relative = path.resolve().relative_to(delivery_root.resolve()).as_posix()
    return {
        "display_name": path.name,
        "delivery_relpath": relative,
        "outbound_tool_path": f"outputs/{relative}",
        "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "size_bytes": size,
        "sha256": _sha256_file(path),
        "feishu_sendable": size <= maximum_bytes,
    }


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> datetime:
    return datetime.now(UTC)

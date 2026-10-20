from __future__ import annotations

import argparse
import csv
from contextlib import ExitStack
import hashlib
import io
import json
import mimetypes
import os
import shutil
import signal
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .config import ServiceSettings
from .ledger import Ledger


class TaskCancelled(RuntimeError):
    pass


class ConversionWorker:
    """Single-worker adapter around the stable local kb_workflow pipeline."""

    def __init__(self, settings: ServiceSettings, *, worker_id: str | None = None,
                 accepted_input=None, record_extraction=None, process_accepted=None) -> None:
        self.settings = settings
        self.ledger = Ledger(settings.ledger_path)
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.accepted_input = accepted_input
        self.record_extraction = record_extraction
        self.process_accepted = process_accepted
        # V2.1 supplies a held execution fence; subprocesses keep it alive if
        # their parent dies. Legacy workers have no extra descriptors.
        self.stage_guard_fds = ()

    def run_once(self) -> bool:
        task = self.ledger.claim_next_task(worker_id=self.worker_id, updated_at=_now())
        if task is None:
            return False
        self._execute(task)
        return True

    def run_forever(self) -> None:
        while True:
            if not self.run_once():
                time.sleep(self.settings.worker_poll_seconds)

    def _execute(self, task: dict[str, Any]) -> None:
        task_id = task["task_id"]
        leases = ExitStack()
        try:
            inspection = self.ledger.get_inspection(task["inspection_id"])
            if inspection is None:
                raise RuntimeError("inspection missing")
            binding = leases.enter_context(self.accepted_input(inspection)) if self.accepted_input else None
            if inspection["source_kind"] == "custody_v21" and binding is None:
                raise RuntimeError("V2.1 task requires accepted asset resolver; path fallback forbidden")
            source = Path(binding["path"]) if binding else self._resolve_source(inspection)
            if binding is None:
                self._verify_hash(source, inspection["sha256"])
            task_root = self.settings.output_root / "tasks" / task_id
            inbox = task_root / "input"
            batches = task_root / "batches"
            logs = task_root / "private_logs"
            inbox.mkdir(parents=True, exist_ok=True)
            batches.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)
            staged_name = source.name.lstrip(".") or f"source{source.suffix.lower()}"
            if binding:
                with (inbox / staged_name).open("xb") as destination:
                    shutil.copyfileobj(binding["stream"], destination)
            else:
                shutil.copy2(source, inbox / staged_name)
            self._verify_hash(inbox / staged_name, inspection["sha256"])
            config_path = task_root / "workflow-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "icloud_inbox": str(inbox),
                        "local_transfer_station": str(batches),
                        "conversion_tool_workspace": str(self.settings.conversion_tool_workspace),
                        "result_prefix": "候选",
                        "default_topic": task_id,
                        "markdown_preview_rows_per_sheet": 200,
                        "skip_name_prefixes": [".", "~$"],
                        "skip_name_contains": ["__MACOSX"],
                        "mineru_backend": task.get("parser_backend") or "pipeline",
                        "mineru_parse_method": task.get("parse_method") or "auto",
                        "mineru_language": task.get("document_language") or "ch",
                        "custody_markdown_bundle": inspection["source_kind"] == "custody_v21" and inspection["route"] == "markdown_bundle_offline",
                        "preserve_webarchive_images": inspection["source_kind"] == "custody_v21",
                        "pdf_text_with_map": inspection["source_kind"] == "custody_v21" and inspection["route"] == "pdf_text_local",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            self._set_progress(task_id, "preparing", 10, "正在复制并登记受控源文件")
            self._run_stage(
                task_id,
                "prepare",
                [
                    self.settings.workflow_python,
                    str(self.settings.workflow_script),
                    "--config",
                    str(config_path),
                    "prepare",
                    "--topic",
                    task_id,
                    "--limit",
                    "1",
                ],
                logs / "prepare.log",
            )
            created = sorted(path for path in batches.iterdir() if path.is_dir())
            if len(created) != 1:
                raise RuntimeError("workflow did not create exactly one batch")
            batch = created[0]
            batch_relpath = batch.relative_to(self.settings.output_root).as_posix()
            self.ledger.update_task_progress(
                task_id,
                status="converting",
                progress_percent=30,
                current_stage="converting",
                message_zh="正在使用本机转换器处理文件",
                updated_at=_now(),
                batch_relpath=batch_relpath,
            )
            if self.process_accepted:
                self.process_accepted(task, inspection, batch)
            # Already processed V2.1 rows are skipped by the existing workflow;
            # its process stage still writes the standard candidate indexes.
            self._run_stage(
                task_id,
                "process",
                [
                    self.settings.workflow_python,
                    str(self.settings.workflow_script),
                    "--config",
                    str(config_path),
                    "process",
                    "--batch",
                    str(batch),
                ],
                logs / "process.log",
            )
            self._set_progress(task_id, "quality_checking", 80, "正在检查候选资料包完整性")
            check_code = self._run_stage(
                task_id,
                "check",
                [
                    self.settings.workflow_python,
                    str(self.settings.workflow_script),
                    "--config",
                    str(config_path),
                    "check",
                    "--batch",
                    str(batch),
                ],
                logs / "check.log",
                accepted_codes={0, 1},
            )
            self._sanitize_public_metadata(batch, inspection)
            artifacts = self._collect_artifacts(task_id, batch)
            quality = self._read_quality(batch, check_code)
            # ZIP is one article: a failed page/ordering check must not be
            # delivered as a candidate containing only generated README files.
            if inspection['route'] in {'image_zip_local_ocr', 'markdown_bundle_offline'}:
                with (batch / '_batch_manifest.csv').open(encoding='utf-8-sig', newline='') as handle:
                    zip_rows = list(csv.DictReader(handle))
                if len(zip_rows) != 1 or zip_rows[0].get('processing_status') != 'processed':
                    note = zip_rows[0].get('notes', '') if len(zip_rows) == 1 else ''
                    marker = 'MarkdownBundleError:' if inspection['route'] == 'markdown_bundle_offline' else 'ImageZipError:'
                    message = note.split(marker, 1)[1].strip() if marker in note else '资料压缩包转换未完成，请检查本机质量报告；没有交付不完整文章'
                    self.ledger.replace_artifacts(task_id, artifacts)
                    self.ledger.finish_task(task_id, status='failed', message_zh=message[:500], updated_at=_now(),
                                            quality=quality, error_code='MARKDOWN_BUNDLE_FAILED' if inspection['route'] == 'markdown_bundle_offline' else 'IMAGE_ZIP_CONVERSION_FAILED')
                    return
            # Reserved outputs are generated by kb_workflow, not uploaded
            # knowledge documents. Persist hash-bound provenance with quality.
            generated = {
                "04_知识库输入文件/README.md": "pipeline_readme",
                "04_知识库输入文件/知识库输入文件清单.csv": "input_manifest",
                "04_知识库输入文件/资料关系索引.csv": "relationship_index",
                "04_知识库输入文件/资料关系索引.md": "relationship_index",
            }
            quality["artifact_provenance"] = {
                "schema_version": "local-conversion-artifact-roles/v1",
                "items": [{**{key: item[key] for key in ("artifact_id", "relative_path", "sha256")}, "role": generated[item["relative_path"]]}
                          for item in artifacts if item["relative_path"] in generated],
            }
            quality_artifact = next(
                (item["artifact_id"] for item in artifacts if item["purpose"] == "quality_report"),
                None,
            )
            quality["report_artifact_id"] = quality_artifact
            if self.record_extraction and quality["serious_issues"] == 0:
                quality["extraction"] = self.record_extraction(task, inspection, batch)
            self.ledger.replace_artifacts(task_id, artifacts)
            if not artifacts:
                status = "failed"
                error_code = "NO_CANDIDATE_OUTPUT"
                message = "转换结束，但没有生成可用候选文件；请查看质量报告"
            elif quality["serious_issues"] > 0:
                status = "needs_review"
                error_code = None
                message = "候选资料包已生成，但质量检查发现严重问题，需要人工复核"
            elif quality["manual_review_count"] > 0:
                status = "needs_review"
                error_code = None
                message = "候选资料包已生成，存在需要人工复核的项目"
            else:
                status = "ready_candidate"
                error_code = None
                message = "本机转换和质量检查已完成，候选资料包可取回"
            self.ledger.finish_task(
                task_id,
                status=status,
                message_zh=message,
                updated_at=_now(),
                quality=quality,
                error_code=error_code,
            )
            self.ledger.record_document(
                {
                    "task_id": task_id,
                    "filename": inspection["filename"],
                    "normalized_filename": inspection["normalized_filename"],
                    "sha256": inspection["sha256"],
                    "status": status,
                    "candidate_location_label": f"本机候选资料包（{task_id}）",
                    "archive_location_label": None,
                    "updated_at": _now(),
                }
            )
        except TaskCancelled:
            self.ledger.finish_task(
                task_id,
                status="cancelled",
                message_zh="本机转换已安全停止；原文件和已生成工件均未删除",
                updated_at=_now(),
            )
        except Exception as exc:  # noqa: BLE001 - worker is a durable process boundary
            self._write_private_failure(task_id, exc)
            self.ledger.finish_task(
                task_id,
                status="failed",
                message_zh="本机转换未完成，请凭任务编号检查本机私有日志",
                updated_at=_now(),
                error_code="WORKER_FAILED",
            )
        finally:
            leases.close()

    def _resolve_source(self, inspection: dict[str, Any]) -> Path:
        root_config = self.settings.source_roots.get(inspection["source_kind"])
        if root_config is None:
            raise RuntimeError("source root missing")
        rel = PurePosixPath(inspection["source_relpath"])
        if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
            raise RuntimeError("unsafe source path")
        root = root_config.path.resolve(strict=True)
        source = root.joinpath(*rel.parts).resolve(strict=True)
        source.relative_to(root)
        if not source.is_file():
            raise RuntimeError("source is not a file")
        return source

    def _verify_hash(self, source: Path, expected: str) -> None:
        if _sha256(source) != expected:
            raise RuntimeError("source changed after inspection")

    def _run_stage(
        self,
        task_id: str,
        stage: str,
        command: list[str],
        log_path: Path,
        *,
        accepted_codes: set[int] | None = None,
    ) -> int:
        accepted = accepted_codes or {0}
        environment = dict(os.environ)
        for name in ("MINERU_API_TOKEN", "MINERU_TOKEN", "MINERU_API_KEY"):
            environment.pop(name, None)
        environment.pop("KB_WORKFLOW_PDF_TEXT_ONLY", None)
        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                command,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                env=environment,
                pass_fds=self.stage_guard_fds,
            )
            while process.poll() is None:
                if self.ledger.is_cancel_requested(task_id):
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                    raise TaskCancelled(stage)
                time.sleep(0.25)
        code = int(process.returncode or 0)
        if code not in accepted:
            raise RuntimeError(f"stage {stage} failed with exit code {code}")
        if self.ledger.is_cancel_requested(task_id):
            raise TaskCancelled(stage)
        return code

    def _set_progress(self, task_id: str, status: str, percent: int, message: str) -> None:
        self.ledger.update_task_progress(
            task_id,
            status=status,
            progress_percent=percent,
            current_stage=status,
            message_zh=message,
            updated_at=_now(),
        )

    def _read_quality(self, batch: Path, check_code: int) -> dict[str, Any]:
        report = batch / "90_日志与清单" / "quality_report.md"
        counts = {"serious_issues": 0, "warnings": 0, "manual_review_count": 0}
        if report.exists():
            text = report.read_text(encoding="utf-8", errors="replace")
            labels = {
                "严重问题": "serious_issues",
                "提醒": "warnings",
                "需人工复核": "manual_review_count",
            }
            for line in text.splitlines():
                for label, key in labels.items():
                    prefix = f"- {label}："
                    if line.startswith(prefix):
                        raw = line[len(prefix) :].strip()
                        if raw.isdigit():
                            counts[key] = int(raw)
        if check_code and counts["serious_issues"] == 0:
            counts["serious_issues"] = 1
        counts["conclusion"] = (
            "ready_candidate"
            if counts["serious_issues"] == 0 and counts["manual_review_count"] == 0
            else "needs_review"
        )
        return counts

    def _collect_artifacts(self, task_id: str, batch: Path) -> list[dict[str, Any]]:
        paths: list[tuple[Path, str]] = []
        kb = batch / "04_知识库输入文件"
        if kb.exists():
            paths.extend((path, "candidate_input") for path in kb.rglob("*") if path.is_file())
        report = batch / "90_日志与清单" / "quality_report.md"
        if report.is_file():
            paths.append((report, "quality_report"))
        artifacts: list[dict[str, Any]] = []
        for path, purpose in sorted(paths, key=lambda item: item[0].as_posix()):
            relative = path.relative_to(batch).as_posix()
            size = path.stat().st_size
            artifacts.append(
                {
                    "artifact_id": f"artifact_{uuid.uuid5(uuid.NAMESPACE_URL, task_id + ':' + relative).hex}",
                    "task_id": task_id,
                    "relative_path": relative,
                    "display_name": path.name,
                    "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    "size_bytes": size,
                    "sha256": _sha256(path),
                    "purpose": purpose,
                    "feishu_sendable": int(size <= self.settings.maximum_delivery_bytes),
                }
            )
        return artifacts

    def _sanitize_public_metadata(self, batch: Path, inspection: dict[str, Any]) -> None:
        """Remove machine paths and private subprocess output from deliverable metadata."""
        candidate_root = batch / "04_知识库输入文件"
        safe_source = f"{inspection['source_kind']}/{inspection['source_relpath']}"
        csv_names = ("知识库输入文件清单.csv", "资料关系索引.csv")
        for name in csv_names:
            path = candidate_root / name
            if not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(raw))
            rows = list(reader)
            if not reader.fieldnames:
                continue
            for row in rows:
                if row.get("source_file"):
                    row["source_file"] = safe_source
                if row.get("notes") and "MinerU 失败" in row["notes"]:
                    row["notes"] = "MinerU 失败，已保留原文件作为候选输入；详细诊断仅保存在本机私有日志"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=reader.fieldnames)
                writer.writeheader()
                writer.writerows(rows)

        relation_md = candidate_root / "资料关系索引.md"
        if relation_md.is_file():
            text = relation_md.read_text(encoding="utf-8", errors="replace")
            text = text.replace(str(self.settings.output_root), "<本机转换输出>")
            relation_md.write_text(text, encoding="utf-8")

        report = batch / "90_日志与清单" / "quality_report.md"
        if report.is_file():
            lines = []
            for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
                if "MinerU 失败" in line:
                    prefix = line.split("MinerU 失败", 1)[0]
                    line = (
                        f"{prefix}MinerU 失败，已保留原文件作为候选输入；"
                        "详细诊断仅保存在本机私有日志"
                    )
                line = line.replace(str(self.settings.output_root), "<本机转换输出>")
                lines.append(line)
            report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_private_failure(self, task_id: str, exc: Exception) -> None:
        path = self.settings.output_root / "tasks" / task_id / "private_logs" / "worker-failure.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{_now()} {type(exc).__name__}: {exc}\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="本机文档转换 MCP 单工作进程")
    parser.add_argument("--once", action="store_true", help="领取至多一个任务后退出")
    args = parser.parse_args()
    worker = ConversionWorker(ServiceSettings.from_env())
    if args.once:
        worker.run_once()
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()

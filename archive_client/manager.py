from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .config import AppConfig, app_data_dir
from .credentials import CredentialStore
from .models import (
    ArchiveDayStatus,
    CancellationToken,
    DownloadProgress,
    DownloadProgressCallback,
    DownloadResult,
    ManifestSnapshot,
    OperationCancelled,
    ProgressCallback,
    ReplicaDayStatus,
    raise_if_cancelled,
)
from .remotes import (
    ArchiveRemote,
    DriveRemote,
    R2Remote,
    UnavailableRemote,
    sha256_file,
    validate_manifest,
)
from .verifier import PureArchivePath, canonical_value, verify_local_day


REPLICA_LABELS = {
    "google_drive": "Google Drive",
    "r2": "Cloudflare R2",
    "mixed": "Google Drive / R2",
}
DASHBOARD_MANIFEST_CACHE_SECONDS = 300


class _CombinedCancellation:
    def __init__(self, *tokens: CancellationToken | None) -> None:
        self.tokens = tuple(token for token in tokens if token is not None)

    def is_set(self) -> bool:
        return any(token.is_set() for token in self.tokens)


def replica_label(value: str) -> str:
    return REPLICA_LABELS.get(value, value)


def validate_archive_date(value: str) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = date.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("归档日期必须是有效的 YYYY-MM-DD UTC 日期") from exc
    if parsed.isoformat() != candidate:
        raise ValueError("归档日期必须是有效的 YYYY-MM-DD UTC 日期")
    return candidate


def utc_yesterday() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            canonical_value(value), ensure_ascii=False, indent=2, sort_keys=True
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


class ArchiveManager:
    def __init__(
        self,
        config: AppConfig,
        credential_store: CredentialStore | None = None,
        drive: ArchiveRemote | None = None,
        r2: ArchiveRemote | None = None,
    ) -> None:
        self.config = config
        self.credential_store = credential_store or CredentialStore()
        if drive is None:
            try:
                drive = DriveRemote(config)
            except Exception as exc:
                drive = UnavailableRemote("google_drive", str(exc))
        self.drive = drive
        if r2 is None:
            try:
                access_key, secret_key = self.credential_store.get_r2(
                    config.profile_id
                )
                r2 = R2Remote(config, access_key, secret_key)
            except Exception as exc:
                r2 = UnavailableRemote("r2", str(exc))
        self.r2 = r2
        self._dashboard_manifest_cache: dict[
            tuple[str, str], tuple[float, ReplicaDayStatus]
        ] = {}
        self._dashboard_cache_lock = threading.Lock()

    @contextmanager
    def _local_operation_lock(self, operation: str):
        identity_errors = self.config.validate_identity()
        if identity_errors:
            raise RuntimeError("配置身份无效：" + "；".join(identity_errors))
        lock_dir = app_data_dir() / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        root_key = hashlib.sha256(
            str(self.config.archive_root).casefold().encode("utf-8")
        ).hexdigest()[:24]
        lock_path = lock_dir / f"archive-root-{root_key}.lock"
        handle = lock_path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                f"本地归档目录已有下载、校验或清理任务运行中，"
                f"本次{operation}未启动"
            ) from exc
        try:
            yield
        finally:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    @property
    def collector_root(self) -> Path:
        self._require_local_config()
        return self.config.archive_root / f"collector={self.config.collector_id}"

    def final_root(self, archive_date: str) -> Path:
        archive_date = validate_archive_date(archive_date)
        return self.collector_root / f"date={archive_date}"

    def staging_root(self, archive_date: str) -> Path:
        archive_date = validate_archive_date(archive_date)
        return self.collector_root / ".partial" / f"date={archive_date}"

    def report_path(self, archive_date: str) -> Path:
        archive_date = validate_archive_date(archive_date)
        self._require_local_config()
        return (
            self.config.archive_root
            / "reports"
            / f"collector={self.config.collector_id}"
            / f"verify-{archive_date}.json"
        )

    def cleanup_report_dir(self) -> Path:
        self._require_local_config()
        return (
            self.config.archive_root
            / "reports"
            / f"collector={self.config.collector_id}"
            / "cloud-cleanup"
        )

    def cleanup_marker_path(self, archive_date: str) -> Path:
        return self.final_root(archive_date) / ".smsi-cloud-cleaned.json"

    @staticmethod
    def _read_json_object(path: Path, label: str) -> dict[str, Any]:
        try:
            if path.stat().st_size > 1024 * 1024:
                raise RuntimeError(f"{label} 文件过大")
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"{label} 文件不存在") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{label} 不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"{label} 必须是 JSON 对象")
        return value

    def _local_snapshot(self, archive_date: str) -> ManifestSnapshot:
        archive_date = validate_archive_date(archive_date)
        root = self.final_root(archive_date)
        marker_path = root / ".smsi-verified.json"
        manifest_path = root / "manifest.json"
        marker = self._read_json_object(marker_path, "本地验证标记")
        if (
            marker.get("contract_version")
            != "smsi-windows-archive-verification/v1"
            or marker.get("status") != "verified"
            or marker.get("archive_date") != archive_date
        ):
            raise RuntimeError("本地验证标记不满足清理门禁")
        try:
            raw = manifest_path.read_bytes()
        except OSError as exc:
            raise RuntimeError("本地 manifest.json 无法读取") from exc
        expected_sha256 = str(marker.get("manifest_sha256") or "")
        return validate_manifest(
            "local",
            archive_date,
            raw,
            expected_sha256,
            len(raw),
        )

    def _completed_cleanup_receipt(
        self, archive_date: str
    ) -> dict[str, Any] | None:
        marker_path = self.cleanup_marker_path(archive_date)
        if not marker_path.is_file():
            return None
        try:
            marker = self._read_json_object(marker_path, "云端清理标记")
            receipt_name = str(marker.get("receipt_file") or "")
            if Path(receipt_name).name != receipt_name:
                return None
            receipt_path = self.cleanup_report_dir() / receipt_name
            receipt = self._read_json_object(receipt_path, "云端清理回执")
            local_marker = self._read_json_object(
                self.final_root(archive_date) / ".smsi-verified.json",
                "本地验证标记",
            )
        except RuntimeError:
            return None
        expected = {
            "profile_id": self.config.profile_id,
            "collector_id": self.config.collector_id,
            "archive_date": archive_date,
        }
        if any(marker.get(key) != value for key, value in expected.items()):
            return None
        if any(receipt.get(key) != value for key, value in expected.items()):
            return None
        if (
            marker.get("contract_version") != "smsi-cloud-cleanup-marker/v1"
            or marker.get("status") != "completed"
            or receipt.get("contract_version")
            != "smsi-windows-cloud-cleanup-receipt/v1"
            or receipt.get("status") != "completed"
            or sha256_file(receipt_path) != marker.get("receipt_sha256")
            or receipt.get("manifest_sha256")
            != local_marker.get("manifest_sha256")
        ):
            return None
        return receipt

    def _require_local_config(self) -> None:
        errors = [*self.config.validate_identity(), *self.config.validate_local()]
        if errors:
            raise RuntimeError("本地归档配置无效：" + "；".join(errors))

    @staticmethod
    def replicas_match(
        drive: ManifestSnapshot, r2: ManifestSnapshot
    ) -> bool:
        return drive.sha256 == r2.sha256 and drive.raw == r2.raw

    def _local_status(self, archive_date: str) -> tuple[str, str]:
        root = self.final_root(archive_date)
        marker = root / ".smsi-verified.json"
        if not root.exists():
            if self.staging_root(archive_date).exists():
                return "partial", "存在未完成下载"
            return "missing", "尚未下载"
        if not marker.is_file():
            return "unverified", "目录存在但没有验证标记"
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "unverified", "验证标记损坏"
        if value.get("status") != "verified":
            return "unverified", "本地验证未通过"
        return "verified", f"{int(value.get('object_count') or 0)} 个对象"

    def _remote_status(
        self,
        remote_name: str,
        remote: ArchiveRemote,
        archive_date: str,
        available_dates: set[str],
        force_refresh: bool = False,
    ) -> ReplicaDayStatus:
        if archive_date not in available_dates:
            return ReplicaDayStatus("missing", "云端无此日期")
        cache_key = (remote_name, archive_date)
        now = time.monotonic()
        if not force_refresh:
            with self._dashboard_cache_lock:
                cached = self._dashboard_manifest_cache.get(cache_key)
            if cached and now - cached[0] < DASHBOARD_MANIFEST_CACHE_SECONDS:
                return cached[1]
        try:
            snapshot = remote.fetch_manifest(archive_date)
        except Exception as exc:
            return ReplicaDayStatus("error", str(exc))
        status = ReplicaDayStatus(
            "verified",
            f"{snapshot.object_count} 对象 / {snapshot.row_count:,} 行",
            snapshot,
        )
        with self._dashboard_cache_lock:
            self._dashboard_manifest_cache[cache_key] = (now, status)
        return status

    def scan(
        self,
        force_refresh: bool = False,
        update: Callable[[str, Any], None] | None = None,
    ) -> tuple[list[ArchiveDayStatus], list[str]]:
        errors: list[str] = []
        local_errors = [
            *self.config.validate_identity(),
            *self.config.validate_local(),
        ]
        if local_errors:
            errors = ["本地归档配置无效：" + "；".join(local_errors)]
            if update:
                update("rows", [])
            return [], errors
        date_sources = (
            ("Google Drive", "google_drive", self.drive),
            ("Cloudflare R2", "r2", self.r2),
        )
        available_dates: dict[str, set[str]] = {}
        source_errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(remote.list_dates): (label, name)
                for label, name, remote in date_sources
            }
            for future in as_completed(futures):
                label, name = futures[future]
                try:
                    available_dates[name] = future.result()
                except Exception as exc:
                    detail = str(exc)
                    available_dates[name] = set()
                    source_errors[name] = detail
                    errors.append(f"{label}: {detail}")
                if update:
                    update(
                        "date_source",
                        {
                            "remote": name,
                            "label": label,
                            "count": len(available_dates[name]),
                            "error": source_errors.get(name),
                        },
                    )
        drive_dates = available_dates["google_drive"]
        r2_dates = available_dates["r2"]
        local_dates = {
            path.name.removeprefix("date=")
            for path in self.collector_root.glob("date=????-??-??")
            if path.is_dir()
        }
        dates: list[str] = []
        for candidate in sorted(drive_dates | r2_dates | local_dates, reverse=True):
            try:
                dates.append(validate_archive_date(candidate))
            except ValueError:
                errors.append(f"已忽略非法归档日期：{candidate}")
        dates = dates[: self.config.history_days]
        remote_statuses: dict[tuple[str, str], ReplicaDayStatus] = {}
        local_statuses: dict[str, tuple[str, str]] = {}
        cleanup_dates: set[str] = set()
        initial_rows: list[ArchiveDayStatus] = []
        for archive_date in dates:
            cleanup_receipt = self._completed_cleanup_receipt(archive_date)
            if cleanup_receipt:
                cleanup_dates.add(archive_date)
            local_state, local_detail = self._local_status(archive_date)
            local_statuses[archive_date] = (local_state, local_detail)
            for remote_name, remote_dates in (
                ("google_drive", drive_dates),
                ("r2", r2_dates),
            ):
                if remote_name in source_errors:
                    status = ReplicaDayStatus(
                        "error", f"日期目录读取失败：{source_errors[remote_name]}"
                    )
                elif archive_date not in remote_dates:
                    status = ReplicaDayStatus("missing", "云端无此日期")
                    if archive_date in cleanup_dates:
                        status = ReplicaDayStatus("cleaned", "已核准手动清理")
                else:
                    status = ReplicaDayStatus("loading", "正在读取 manifest")
                remote_statuses[(archive_date, remote_name)] = status
            initial_rows.append(
                ArchiveDayStatus(
                    archive_date,
                    remote_statuses[(archive_date, "google_drive")],
                    remote_statuses[(archive_date, "r2")],
                    local_state,
                    local_detail,
                )
            )
        if update:
            update("rows", initial_rows)

        jobs = [
            (archive_date, remote_name, remote, remote_dates)
            for archive_date in dates
            for remote_name, remote, remote_dates in (
                ("google_drive", self.drive, drive_dates),
                ("r2", self.r2, r2_dates),
            )
            if remote_statuses[(archive_date, remote_name)].state == "loading"
        ]
        if jobs:
            with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as executor:
                futures = {
                    executor.submit(
                        self._remote_status,
                        remote_name,
                        remote,
                        archive_date,
                        remote_dates,
                        force_refresh,
                    ): (archive_date, remote_name)
                    for archive_date, remote_name, remote, remote_dates in jobs
                }
                for future in as_completed(futures):
                    archive_date, remote_name = futures[future]
                    try:
                        status = future.result()
                    except Exception as exc:
                        status = ReplicaDayStatus("error", str(exc))
                    if status.state == "missing" and archive_date in cleanup_dates:
                        status = ReplicaDayStatus("cleaned", "已核准手动清理")
                    remote_statuses[(archive_date, remote_name)] = status
                    drive = remote_statuses[(archive_date, "google_drive")]
                    r2 = remote_statuses[(archive_date, "r2")]
                    match = (
                        self.replicas_match(drive.snapshot, r2.snapshot)
                        if drive.snapshot and r2.snapshot
                        else None
                    )
                    if update:
                        update(
                            "replica",
                            {
                                "archive_date": archive_date,
                                "remote": remote_name,
                                "status": status,
                                "replicas_match": match,
                            },
                        )

        rows: list[ArchiveDayStatus] = []
        for archive_date in dates:
            drive = remote_statuses[(archive_date, "google_drive")]
            r2 = remote_statuses[(archive_date, "r2")]
            match = (
                self.replicas_match(drive.snapshot, r2.snapshot)
                if drive.snapshot and r2.snapshot
                else None
            )
            local_state, local_detail = local_statuses[archive_date]
            rows.append(
                ArchiveDayStatus(
                    archive_date, drive, r2, local_state, local_detail, match
                )
            )
        return rows, errors

    def _local_storage_usage(self) -> dict[str, Any]:
        probe = self.config.archive_root
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        disk = shutil.disk_usage(probe)
        return {
            "scope": "volume",
            "total_bytes": int(disk.total),
            "used_bytes": int(disk.used),
            "free_bytes": int(disk.free),
            "volume": probe.drive or probe.anchor,
        }

    def scan_dashboard(
        self,
        force_refresh: bool = False,
        update: Callable[[str, Any], None] | None = None,
    ) -> tuple[list[ArchiveDayStatus], list[str], dict[str, dict[str, Any]]]:
        usage: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(self.scan, force_refresh, update): ("scan", "scan"),
                executor.submit(self.drive.storage_usage): ("usage", "google_drive"),
                executor.submit(self.r2.storage_usage): ("usage", "r2"),
                executor.submit(self._local_storage_usage): ("usage", "local"),
            }
            rows: list[ArchiveDayStatus] = []
            errors: list[str] = []
            for future in as_completed(futures):
                kind, name = futures[future]
                if kind == "scan":
                    rows, errors = future.result()
                    continue
                try:
                    value = future.result()
                except Exception as exc:
                    value = {"error": str(exc)}
                usage[name] = value
                if update:
                    update("usage", {"name": name, "value": value})
        for name in ("google_drive", "r2"):
            usage.setdefault(name, {"error": "容量统计没有返回结果"})
        usage.setdefault("local", {"error": "磁盘容量统计没有返回结果"})
        return rows, errors, usage

    def latest_cleanup_plan(self, archive_date: str) -> Path | None:
        archive_date = validate_archive_date(archive_date)
        directory = self.cleanup_report_dir()
        candidates = sorted(directory.glob(f"plan-{archive_date}-*.json"), reverse=True)
        for candidate in candidates:
            try:
                plan = self._read_json_object(candidate, "云端清理计划")
            except RuntimeError:
                continue
            if (
                plan.get("contract_version")
                == "smsi-windows-cloud-cleanup-plan/v1"
                and plan.get("status") == "ready"
                and plan.get("profile_id") == self.config.profile_id
                and plan.get("collector_id") == self.config.collector_id
                and plan.get("archive_date") == archive_date
            ):
                return candidate
        return None

    def latest_r2_cleanup_checkpoint(
        self, archive_date: str, plan_path: Path
    ) -> Path | None:
        archive_date = validate_archive_date(archive_date)
        plan_path = Path(plan_path).expanduser().resolve()
        plan_sha256 = sha256_file(plan_path)
        candidates = sorted(
            self.cleanup_report_dir().glob(
                f"checkpoint-r2-{archive_date}-*.json"
            ),
            reverse=True,
        )
        for candidate in candidates:
            try:
                checkpoint = self._read_json_object(
                    candidate, "Cloudflare R2 清理检查点"
                )
            except RuntimeError:
                continue
            if (
                checkpoint.get("contract_version")
                == "smsi-windows-cloud-cleanup-checkpoint/v1"
                and checkpoint.get("status") == "r2_absence_verified"
                and checkpoint.get("profile_id") == self.config.profile_id
                and checkpoint.get("collector_id") == self.config.collector_id
                and checkpoint.get("archive_date") == archive_date
                and checkpoint.get("plan_sha256") == plan_sha256
            ):
                return candidate
        return None

    def prepare_manual_cleanup(
        self,
        archive_date: str,
        retention_path: Path,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        with self._local_operation_lock("清理准备"):
            return self._prepare_manual_cleanup(
                archive_date, retention_path, progress
            )

    def _prepare_manual_cleanup(
        self,
        archive_date: str,
        retention_path: Path,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        archive_date = validate_archive_date(archive_date)
        if self._completed_cleanup_receipt(archive_date):
            raise RuntimeError("该日期已经有完整的云端清理回执")
        retention_path = Path(retention_path).expanduser().resolve()
        retention = self._read_json_object(retention_path, "服务器 retention.json")
        if (
            retention.get("contract_version") != "smsi-v3-hot-retention/v1"
            or retention.get("status") != "dropped"
            or retention.get("archive_date") != archive_date
            or not retention.get("dropped_at")
            or not isinstance(retention.get("partitions"), list)
        ):
            raise RuntimeError(
                "服务器 retention.json 尚未证明该日期热库分区已安全删除"
            )

        snapshots, _ = self._load_snapshots(archive_date)
        if len(snapshots) != 2 or not self.replicas_match(
            snapshots["google_drive"], snapshots["r2"]
        ):
            raise RuntimeError("清理要求 Google Drive 与 R2 manifest 同时存在且完全一致")
        snapshot = snapshots["google_drive"]
        if retention.get("verified_manifest_sha256") != snapshot.sha256:
            raise RuntimeError("retention.json 与当前云端 manifest SHA-256 不一致")

        local_snapshot = self._local_snapshot(archive_date)
        if not self.replicas_match(snapshot, local_snapshot):
            raise RuntimeError("本地 manifest 与双云端 manifest 不一致")
        verification = verify_local_day(
            self.final_root(archive_date), local_snapshot, progress
        )
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
        object_bytes = sum(
            int(item.get("size_bytes") or 0)
            for item in snapshot.manifest.get("objects") or []
        )
        plan = {
            "contract_version": "smsi-windows-cloud-cleanup-plan/v1",
            "status": "ready",
            "profile_id": self.config.profile_id,
            "collector_id": self.config.collector_id,
            "archive_date": archive_date,
            "manifest_sha256": snapshot.sha256,
            "object_count": snapshot.object_count,
            "row_count": snapshot.row_count,
            "parquet_bytes": object_bytes,
            "local_tree_sha256": verification["tree_sha256"],
            "local_verified_at": verification["verified_at"],
            "retention_evidence": {
                "source_file": str(retention_path),
                "file_sha256": sha256_file(retention_path),
                "status": retention["status"],
                "dropped_at": retention["dropped_at"],
                "partitions": retention["partitions"],
                "verified_manifest_sha256": retention[
                    "verified_manifest_sha256"
                ],
            },
            "targets": {
                "google_drive": (
                    f"{self.config.drive_root}/date={archive_date}"
                ),
                "cloudflare_r2": (
                    f"s3://{self.config.r2_bucket}/"
                    f"{self.config.r2_root}/date={archive_date}/"
                ),
            },
            "expected_remote_entries": {
                "google_drive": 2 * (snapshot.object_count + 1),
                "cloudflare_r2": snapshot.object_count + 1,
                "note": "Drive 数量包含每个对象及 manifest 的元数据 sidecar",
            },
            "generated_at": now,
            "deletion_mode": "manual_provider_console",
            "client_credentials_remain_read_only": True,
        }
        plan_path = self.cleanup_report_dir() / f"plan-{archive_date}-{stamp}.json"
        if plan_path.exists():
            raise RuntimeError("云端清理计划文件已存在，请稍后重试")
        write_json_atomic(plan_path, plan)
        plan["report_path"] = str(plan_path)
        return plan

    def confirm_manual_cleanup(
        self,
        archive_date: str,
        plan_path: Path,
        operator_confirmation: str,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        with self._local_operation_lock("清理确认"):
            return self._confirm_manual_cleanup(
                archive_date,
                plan_path,
                operator_confirmation,
                progress,
            )

    def _confirm_manual_cleanup(
        self,
        archive_date: str,
        plan_path: Path,
        operator_confirmation: str,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        archive_date = validate_archive_date(archive_date)
        if self._completed_cleanup_receipt(archive_date):
            raise RuntimeError("该日期已经有完整的云端清理回执")
        plan_path = Path(plan_path).expanduser().resolve()
        cleanup_root = self.cleanup_report_dir().resolve()
        if not plan_path.is_relative_to(cleanup_root):
            raise RuntimeError("云端清理计划不属于当前采集配置")
        plan = self._read_json_object(plan_path, "云端清理计划")
        expected = {
            "profile_id": self.config.profile_id,
            "collector_id": self.config.collector_id,
            "archive_date": archive_date,
        }
        if (
            plan.get("contract_version")
            != "smsi-windows-cloud-cleanup-plan/v1"
            or plan.get("status") != "ready"
            or any(plan.get(key) != value for key, value in expected.items())
        ):
            raise RuntimeError("云端清理计划与当前采集配置或日期不匹配")
        checkpoint_path = self.latest_r2_cleanup_checkpoint(
            archive_date, plan_path
        )
        expected_confirmation = (
            f"CLEAN DRIVE {archive_date}"
            if checkpoint_path
            else f"CLEAN R2 {archive_date}"
        )
        if operator_confirmation.strip() != expected_confirmation:
            raise RuntimeError(f"确认文字必须完全等于 {expected_confirmation}")

        try:
            drive_dates = self.drive.list_dates()
        except Exception as exc:
            raise RuntimeError(f"无法证明 Google Drive 删除完成：{exc}") from exc
        try:
            r2_dates = self.r2.list_dates()
        except Exception as exc:
            raise RuntimeError(f"无法证明 Cloudflare R2 删除完成：{exc}") from exc
        local_snapshot = self._local_snapshot(archive_date)
        if local_snapshot.sha256 != plan.get("manifest_sha256"):
            raise RuntimeError("本地 manifest 与清理计划不一致")
        verification = verify_local_day(
            self.final_root(archive_date), local_snapshot, progress
        )
        if verification["tree_sha256"] != plan.get("local_tree_sha256"):
            raise RuntimeError("本地归档树摘要在清理期间发生变化")

        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
        drive_exists = archive_date in drive_dates
        r2_exists = archive_date in r2_dates
        if checkpoint_path is None:
            if r2_exists:
                raise RuntimeError("Cloudflare R2 仍存在该日期前缀，尚未完成第一阶段删除")
            if not drive_exists:
                raise RuntimeError(
                    "Google Drive 也已不存在，违反逐副本清理顺序；已拒绝生成检查点"
                )
            checkpoint = {
                "contract_version": "smsi-windows-cloud-cleanup-checkpoint/v1",
                "status": "r2_absence_verified",
                **expected,
                "manifest_sha256": local_snapshot.sha256,
                "local_tree_sha256": verification["tree_sha256"],
                "plan_file": plan_path.name,
                "plan_sha256": sha256_file(plan_path),
                "cloudflare_r2_absent": True,
                "google_drive_still_present": True,
                "operator_confirmation": operator_confirmation.strip(),
                "verified_at": now,
            }
            checkpoint_path = (
                self.cleanup_report_dir()
                / f"checkpoint-r2-{archive_date}-{stamp}.json"
            )
            write_json_atomic(checkpoint_path, checkpoint)
            self.r2.invalidate_usage_cache()
            checkpoint["report_path"] = str(checkpoint_path)
            return checkpoint

        if r2_exists:
            raise RuntimeError("Cloudflare R2 日期前缀重新出现，已拒绝继续清理")
        if drive_exists:
            raise RuntimeError("Google Drive 仍存在该日期目录，尚未完成第二阶段删除")
        checkpoint = self._read_json_object(
            checkpoint_path, "Cloudflare R2 清理检查点"
        )
        receipt = {
            "contract_version": "smsi-windows-cloud-cleanup-receipt/v1",
            "status": "completed",
            **expected,
            "manifest_sha256": local_snapshot.sha256,
            "local_tree_sha256": verification["tree_sha256"],
            "local_verified_at": verification["verified_at"],
            "plan_file": plan_path.name,
            "plan_sha256": sha256_file(plan_path),
            "r2_checkpoint_file": checkpoint_path.name,
            "r2_checkpoint_sha256": sha256_file(checkpoint_path),
            "remote_absence": {
                "google_drive": True,
                "cloudflare_r2": True,
                "verified_at": now,
            },
            "operator_confirmations": {
                "cloudflare_r2": checkpoint["operator_confirmation"],
                "google_drive": operator_confirmation.strip(),
            },
            "google_drive_trash_emptied_attested": True,
            "completed_at": now,
        }
        receipt_path = (
            self.cleanup_report_dir() / f"receipt-{archive_date}-{stamp}.json"
        )
        write_json_atomic(receipt_path, receipt)
        marker = {
            "contract_version": "smsi-cloud-cleanup-marker/v1",
            "status": "completed",
            **expected,
            "manifest_sha256": local_snapshot.sha256,
            "receipt_file": receipt_path.name,
            "receipt_sha256": sha256_file(receipt_path),
            "completed_at": now,
        }
        write_json_atomic(self.cleanup_marker_path(archive_date), marker)
        self.drive.invalidate_usage_cache()
        self.r2.invalidate_usage_cache()
        receipt["report_path"] = str(receipt_path)
        return receipt

    def _load_snapshots(
        self, archive_date: str
    ) -> tuple[dict[str, ManifestSnapshot], list[str]]:
        archive_date = validate_archive_date(archive_date)
        snapshots: dict[str, ManifestSnapshot] = {}
        errors: list[str] = []
        remotes = (("google_drive", self.drive), ("r2", self.r2))
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(remote.fetch_manifest, archive_date): name
                for name, remote in remotes
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    snapshots[name] = future.result()
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
        if self.config.require_both_replicas:
            if len(snapshots) != 2:
                raise RuntimeError("双副本门禁未通过：" + "；".join(errors))
            if not self.replicas_match(
                snapshots["google_drive"], snapshots["r2"]
            ):
                raise RuntimeError("双副本 manifest 不一致，已拒绝下载")
        if not snapshots:
            raise RuntimeError("两个云端均无法读取：" + "；".join(errors))
        return snapshots, errors

    def download_day(
        self,
        archive_date: str,
        progress: ProgressCallback | None = None,
        detail_progress: DownloadProgressCallback | None = None,
        cancel: CancellationToken | None = None,
    ) -> DownloadResult:
        with self._local_operation_lock("下载"):
            return self._download_day(
                archive_date,
                progress,
                detail_progress,
                cancel,
            )

    def _download_day(
        self,
        archive_date: str,
        progress: ProgressCallback | None = None,
        detail_progress: DownloadProgressCallback | None = None,
        cancel: CancellationToken | None = None,
    ) -> DownloadResult:
        archive_date = validate_archive_date(archive_date)
        policy_errors = self.config.validate_policy()
        if policy_errors:
            raise RuntimeError("同步策略无效：" + "；".join(policy_errors))
        raise_if_cancelled(cancel)
        snapshots, warnings = self._load_snapshots(archive_date)
        raise_if_cancelled(cancel)
        selected = self.config.preferred_replica
        if selected not in snapshots:
            selected = next(iter(snapshots))
            warnings.append(f"首选副本不可用，已改用 {replica_label(selected)}")
        snapshot = snapshots[selected]
        objects = snapshot.manifest["objects"]
        object_count = len(objects)
        download_total = sum(int(item["size_bytes"]) for item in objects)

        compatible_sources = [selected]
        for name, candidate in snapshots.items():
            if name != selected and self.replicas_match(snapshot, candidate):
                compatible_sources.append(name)
        if len(snapshots) > 1 and len(compatible_sources) == 1:
            warnings.append("另一云端副本 manifest 不一致，不能作为下载回退来源")
        download_workers = max(
            1, min(int(self.config.download_workers), max(object_count, 1))
        )
        if progress:
            progress(f"下载来源：{replica_label(selected)}", 0, 10_000)
        if detail_progress:
            detail_progress(
                DownloadProgress(
                    archive_date=archive_date,
                    stage="preparing",
                    overall_current=0,
                    source=selected,
                    object_count=object_count,
                    bytes_total=download_total,
                    download_workers=download_workers,
                )
            )

        final = self.final_root(archive_date)
        if final.exists():
            marker_state, _ = self._local_status(archive_date)
            if marker_state == "verified":
                marker = json.loads(
                    (final / ".smsi-verified.json").read_text(encoding="utf-8")
                )
                if marker.get("manifest_sha256") == snapshot.sha256:
                    report_path = self.report_path(archive_date)
                    if not report_path.is_file():
                        write_json_atomic(report_path, marker)
                    if progress:
                        progress("本地归档已完整验证", 10_000, 10_000)
                    if detail_progress:
                        detail_progress(
                            DownloadProgress(
                                archive_date=archive_date,
                                stage="complete",
                                overall_current=10_000,
                                source=selected,
                                object_index=object_count,
                                object_count=object_count,
                                bytes_completed=download_total,
                                bytes_total=download_total,
                                stage_current=object_count,
                                stage_total=object_count,
                                completed_objects=object_count,
                                download_workers=download_workers,
                            )
                        )
                    return DownloadResult(
                        "already_verified",
                        archive_date,
                        selected,
                        final,
                        report_path,
                        snapshot.object_count,
                        snapshot.row_count,
                        0,
                        warnings,
                    )
            raise RuntimeError("本地正式目录已存在但无法匹配云端，请人工检查")

        staging = self.staging_root(archive_date)
        staging.mkdir(parents=True, exist_ok=True)
        additional_bytes = 0
        for item in objects:
            relative_key = str(item["relative_key"])
            inside = PureArchivePath.strip_date(relative_key, archive_date)
            destination = staging / Path(*inside.parts)
            temporary = destination.with_suffix(destination.suffix + ".partial")
            existing_bytes = sum(
                path.stat().st_size for path in (destination, temporary) if path.is_file()
            )
            additional_bytes += max(
                int(item["size_bytes"]) - existing_bytes,
                0,
            )
        free_bytes = shutil.disk_usage(staging).free
        reserve_bytes = 256 * 1024 * 1024 if additional_bytes else 0
        if free_bytes < additional_bytes + reserve_bytes:
            raise RuntimeError(
                "本地磁盘空间不足："
                f"还需约 {additional_bytes / (1024 ** 3):.2f} GiB，"
                f"当前可用 {free_bytes / (1024 ** 3):.2f} GiB，"
                "并要求保留 0.25 GiB 安全空间"
            )
        abort = threading.Event()
        transfer_cancel = _CombinedCancellation(cancel, abort)
        progress_lock = threading.Lock()
        publish_lock = threading.Lock()
        object_progress_bytes: dict[int, int] = {}
        object_network_bytes: dict[int, int] = {}
        active_objects: set[int] = set()
        completed_objects: set[int] = set()
        last_overall = 0

        def publish_download_progress(
            *,
            stage: str,
            object_index: int,
            relative_key: str,
            expected_size: int,
            object_current: int,
            network_current: int,
            source: str,
            completed: bool = False,
        ) -> None:
            nonlocal last_overall
            with publish_lock:
                with progress_lock:
                    object_progress_bytes[object_index] = max(
                        object_progress_bytes.get(object_index, 0),
                        min(max(int(object_current), 0), expected_size),
                    )
                    object_network_bytes[object_index] = max(
                        object_network_bytes.get(object_index, 0),
                        max(int(network_current), 0),
                    )
                    if completed:
                        completed_objects.add(object_index)
                        active_objects.discard(object_index)
                        object_progress_bytes[object_index] = expected_size
                    bytes_completed = min(
                        sum(object_progress_bytes.values()), download_total
                    )
                    network_completed = sum(object_network_bytes.values())
                    fraction = (
                        bytes_completed / download_total if download_total else 1.0
                    )
                    last_overall = max(last_overall, int(fraction * 7_000))
                    update = DownloadProgress(
                        archive_date=archive_date,
                        stage=stage,
                        overall_current=last_overall,
                        source=source,
                        object_name=PurePosixPath(relative_key).name,
                        object_index=object_index,
                        object_count=object_count,
                        object_bytes_completed=object_progress_bytes[object_index],
                        object_bytes_total=expected_size,
                        bytes_completed=bytes_completed,
                        bytes_total=download_total,
                        network_bytes_completed=network_completed,
                        stage_current=len(completed_objects),
                        stage_total=object_count,
                        completed_objects=len(completed_objects),
                        active_transfers=len(active_objects),
                        download_workers=download_workers,
                    )
                if progress:
                    progress(
                        f"下载 · {replica_label(source)} · "
                        f"{PurePosixPath(relative_key).name}",
                        update.overall_current,
                        10_000,
                    )
                if detail_progress:
                    detail_progress(update)

        def download_one(
            object_index: int, item: dict[str, Any]
        ) -> dict[str, Any]:
            raise_if_cancelled(transfer_cancel)
            relative_key = str(item["relative_key"])
            inside = PureArchivePath.strip_date(relative_key, archive_date)
            destination = staging / Path(*inside.parts)
            expected_size = int(item["size_bytes"])
            expected_sha256 = str(item["sha256"])
            object_high_water = 0
            network_before_attempt = 0
            last_published_at = 0.0
            attempt_order = [selected] + [
                name for name in compatible_sources if name != selected
            ]
            attempted: list[str] = []
            object_warnings: list[str] = []
            with progress_lock:
                active_objects.add(object_index)
            for source_name in attempt_order:
                raise_if_cancelled(transfer_cancel)
                remote = self.drive if source_name == "google_drive" else self.r2
                attempted.append(source_name)
                attempt_start: int | None = None
                attempt_high_water = 0

                def object_progress(
                    name: str,
                    current: int,
                    total: int,
                    *,
                    source: str = source_name,
                ) -> None:
                    nonlocal attempt_high_water, attempt_start, object_high_water
                    nonlocal last_published_at
                    normalized_current = min(max(int(current), 0), expected_size)
                    if attempt_start is None:
                        attempt_start = normalized_current
                    attempt_high_water = max(
                        attempt_high_water,
                        max(normalized_current - attempt_start, 0),
                    )
                    object_high_water = max(object_high_water, normalized_current)
                    now = time.monotonic()
                    if (
                        normalized_current < expected_size
                        and now - last_published_at < 0.2
                    ):
                        return
                    last_published_at = now
                    publish_download_progress(
                        stage="downloading",
                        object_index=object_index,
                        relative_key=name,
                        expected_size=expected_size,
                        object_current=object_high_water,
                        network_current=network_before_attempt + attempt_high_water,
                        source=source,
                    )

                try:
                    transferred = remote.download_object(
                        relative_key,
                        destination,
                        expected_size,
                        expected_sha256,
                        object_progress,
                        transfer_cancel,
                    )
                except OperationCancelled:
                    raise
                except Exception as exc:
                    if source_name == attempt_order[-1]:
                        raise RuntimeError(
                            f"{PurePosixPath(relative_key).name} · "
                            f"{replica_label(source_name)} 下载失败：{exc}"
                        ) from exc
                    network_before_attempt += attempt_high_water
                    fallback = attempt_order[attempt_order.index(source_name) + 1]
                    warning = (
                        f"{PurePosixPath(relative_key).name}："
                        f"{replica_label(source_name)} 下载失败，已切换到 "
                        f"{replica_label(fallback)}：{exc}"
                    )
                    object_warnings.append(warning)
                    temporary = destination.with_suffix(destination.suffix + ".partial")
                    temporary.unlink(missing_ok=True)
                    publish_download_progress(
                        stage="switching",
                        object_index=object_index,
                        relative_key=relative_key,
                        expected_size=expected_size,
                        object_current=object_high_water,
                        network_current=network_before_attempt,
                        source=fallback,
                    )
                    continue
                network_total = network_before_attempt + max(
                    int(transferred), attempt_high_water
                )
                publish_download_progress(
                    stage="downloading",
                    object_index=object_index,
                    relative_key=relative_key,
                    expected_size=expected_size,
                    object_current=expected_size,
                    network_current=network_total,
                    source=source_name,
                    completed=True,
                )
                return {
                    "index": object_index,
                    "source": source_name,
                    "attempted": attempted,
                    "warnings": object_warnings,
                    "network_bytes": network_total,
                }
            raise RuntimeError(f"归档对象下载失败：{relative_key}")

        results: dict[int, dict[str, Any]] = {}
        first_error: Exception | None = None
        if objects:
            with ThreadPoolExecutor(max_workers=download_workers) as executor:
                futures = {
                    executor.submit(download_one, index, item): index
                    for index, item in enumerate(objects, start=1)
                }
                for future in as_completed(futures):
                    if future.cancelled():
                        continue
                    try:
                        result = future.result()
                    except OperationCancelled as exc:
                        abort.set()
                        if cancel is not None and cancel.is_set():
                            first_error = first_error or exc
                    except Exception as exc:
                        if first_error is None:
                            first_error = exc
                        abort.set()
                    else:
                        results[result["index"]] = result
                    if abort.is_set():
                        for pending in futures:
                            pending.cancel()
        if cancel is not None and cancel.is_set():
            raise OperationCancelled(
                "操作已取消，未完成文件保留在 .partial 目录"
            )
        if first_error is not None:
            raise first_error
        if len(results) != object_count:
            raise RuntimeError("下载任务未完成全部归档对象")

        ordered_results = [results[index] for index in sorted(results)]
        for result in ordered_results:
            warnings.extend(result["warnings"])
        sources_attempted = list(
            dict.fromkeys(
                source
                for result in ordered_results
                for source in result["attempted"]
            )
        )
        sources_completed = list(
            dict.fromkeys(result["source"] for result in ordered_results)
        )
        active_source = (
            sources_completed[0] if len(sources_completed) == 1 else "mixed"
        )
        downloaded = sum(int(result["network_bytes"]) for result in ordered_results)
        raise_if_cancelled(cancel)

        (staging / "manifest.json").write_bytes(snapshot.raw)
        if progress:
            progress("正在执行完整恢复校验", 7_000, 10_000)
        if detail_progress:
            detail_progress(
                DownloadProgress(
                    archive_date=archive_date,
                    stage="verifying",
                    overall_current=7_000,
                    source=active_source,
                    object_count=object_count,
                    bytes_completed=download_total,
                    bytes_total=download_total,
                    network_bytes_completed=downloaded,
                    stage_total=object_count,
                    completed_objects=object_count,
                    download_workers=download_workers,
                )
            )

        def verification_progress(name: str, current: int, total: int) -> None:
            fraction = (current / total) if total else 0
            overall_current = 7_000 + int(min(max(fraction, 0), 1) * 3_000)
            if progress:
                progress(
                    f"校验 · {PurePosixPath(name).name}",
                    overall_current,
                    10_000,
                )
            if detail_progress:
                detail_progress(
                    DownloadProgress(
                        archive_date=archive_date,
                        stage="verifying",
                        overall_current=overall_current,
                        source=active_source,
                        object_name=PurePosixPath(name).name,
                        object_index=current,
                        object_count=object_count,
                        bytes_completed=download_total,
                        bytes_total=download_total,
                        network_bytes_completed=downloaded,
                        stage_current=current,
                        stage_total=total,
                        completed_objects=object_count,
                        download_workers=download_workers,
                    )
                )

        report = verify_local_day(
            staging, snapshot, verification_progress, cancel
        )
        raise_if_cancelled(cancel)
        report.update(
            {
                "download_replica": active_source,
                "download_sources_attempted": sources_attempted,
                "download_sources_completed": sources_completed,
                "drive_manifest_sha256": snapshots.get("google_drive").sha256
                if snapshots.get("google_drive")
                else None,
                "r2_manifest_sha256": snapshots.get("r2").sha256
                if snapshots.get("r2")
                else None,
                "replicas_match": len(snapshots) == 2
                and self.replicas_match(
                    snapshots["google_drive"], snapshots["r2"]
                ),
                "downloaded_bytes_this_run": downloaded,
            }
        )
        report_path = self.report_path(archive_date)
        write_json_atomic(staging / ".smsi-verified.json", report)
        raise_if_cancelled(cancel)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, final)
        # The verified directory is the source of truth. Publish the report only
        # after that atomic commit, so cancellation cannot leave a false report.
        write_json_atomic(report_path, report)
        partial_parent = staging.parent
        if partial_parent.exists() and not any(partial_parent.iterdir()):
            partial_parent.rmdir()
        if detail_progress:
            detail_progress(
                DownloadProgress(
                    archive_date=archive_date,
                    stage="complete",
                    overall_current=10_000,
                    source=active_source,
                    object_index=object_count,
                    object_count=object_count,
                    bytes_completed=download_total,
                    bytes_total=download_total,
                    network_bytes_completed=downloaded,
                    stage_current=object_count,
                    stage_total=object_count,
                    completed_objects=object_count,
                    download_workers=download_workers,
                )
            )
        return DownloadResult(
            "verified",
            archive_date,
            active_source,
            final,
            report_path,
            snapshot.object_count,
            snapshot.row_count,
            downloaded,
            warnings,
        )

    def verify_existing_day(
        self,
        archive_date: str,
        progress: ProgressCallback | None = None,
        cancel: CancellationToken | None = None,
    ) -> dict[str, Any]:
        with self._local_operation_lock("校验"):
            return self._verify_existing_day(archive_date, progress, cancel)

    def _verify_existing_day(
        self,
        archive_date: str,
        progress: ProgressCallback | None = None,
        cancel: CancellationToken | None = None,
    ) -> dict[str, Any]:
        archive_date = validate_archive_date(archive_date)
        raise_if_cancelled(cancel)
        root = self.final_root(archive_date)
        if not root.is_dir():
            raise RuntimeError("本地日期目录不存在")
        cleanup_receipt = self._completed_cleanup_receipt(archive_date)
        if cleanup_receipt:
            snapshot = self._local_snapshot(archive_date)
            report = verify_local_day(root, snapshot, progress, cancel)
            raise_if_cancelled(cancel)
            report.update(
                {
                    "replicas_match": None,
                    "cloud_cleanup_status": "completed",
                    "cloud_cleanup_completed_at": cleanup_receipt.get(
                        "completed_at"
                    ),
                }
            )
            write_json_atomic(self.report_path(archive_date), report)
            write_json_atomic(root / ".smsi-verified.json", report)
            return report
        snapshots, _ = self._load_snapshots(archive_date)
        raise_if_cancelled(cancel)
        snapshot = snapshots.get(self.config.preferred_replica) or next(
            iter(snapshots.values())
        )
        report = verify_local_day(root, snapshot, progress, cancel)
        raise_if_cancelled(cancel)
        report["replicas_match"] = len(snapshots) == 2 and self.replicas_match(
            snapshots["google_drive"], snapshots["r2"]
        )
        write_json_atomic(self.report_path(archive_date), report)
        write_json_atomic(root / ".smsi-verified.json", report)
        return report

    def run_once(self, archive_date: str | None = None) -> DownloadResult:
        return self.download_day(archive_date or utc_yesterday())

from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .config import AppConfig
from .credentials import CredentialStore
from .models import (
    ArchiveDayStatus,
    DownloadProgress,
    DownloadProgressCallback,
    DownloadResult,
    ManifestSnapshot,
    ProgressCallback,
    ReplicaDayStatus,
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
}


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

    @staticmethod
    def _remote_status(
        remote: ArchiveRemote, archive_date: str, available_dates: set[str]
    ) -> ReplicaDayStatus:
        if archive_date not in available_dates:
            return ReplicaDayStatus("missing", "云端无此日期")
        try:
            snapshot = remote.fetch_manifest(archive_date)
        except Exception as exc:
            return ReplicaDayStatus("error", str(exc))
        return ReplicaDayStatus(
            "verified",
            f"{snapshot.object_count} 对象 / {snapshot.row_count:,} 行",
            snapshot,
        )

    def scan(self) -> tuple[list[ArchiveDayStatus], list[str]]:
        errors: list[str] = []
        local_errors = [
            *self.config.validate_identity(),
            *self.config.validate_local(),
        ]
        if local_errors:
            return [], ["本地归档配置无效：" + "；".join(local_errors)]
        try:
            drive_dates = self.drive.list_dates()
        except Exception as exc:
            drive_dates = set()
            errors.append(f"Google Drive: {exc}")
        try:
            r2_dates = self.r2.list_dates()
        except Exception as exc:
            r2_dates = set()
            errors.append(f"Cloudflare R2: {exc}")
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
        rows: list[ArchiveDayStatus] = []
        for archive_date in dates:
            drive = self._remote_status(self.drive, archive_date, drive_dates)
            r2 = self._remote_status(self.r2, archive_date, r2_dates)
            cleanup_receipt = self._completed_cleanup_receipt(archive_date)
            if cleanup_receipt:
                if drive.state == "missing":
                    drive = ReplicaDayStatus("cleaned", "已核准手动清理")
                if r2.state == "missing":
                    r2 = ReplicaDayStatus("cleaned", "已核准手动清理")
            match: bool | None = None
            if drive.snapshot and r2.snapshot:
                match = self.replicas_match(drive.snapshot, r2.snapshot)
            local_state, local_detail = self._local_status(archive_date)
            rows.append(
                ArchiveDayStatus(
                    archive_date, drive, r2, local_state, local_detail, match
                )
            )
        return rows, errors

    def scan_dashboard(
        self,
    ) -> tuple[list[ArchiveDayStatus], list[str], dict[str, dict[str, Any]]]:
        rows, errors = self.scan()
        usage: dict[str, dict[str, Any]] = {}
        for name, remote in (("google_drive", self.drive), ("r2", self.r2)):
            try:
                usage[name] = remote.storage_usage()
            except Exception as exc:
                usage[name] = {"error": str(exc)}
        try:
            probe = self.config.archive_root
            while not probe.exists() and probe.parent != probe:
                probe = probe.parent
            disk = shutil.disk_usage(probe)
            usage["local"] = {
                "scope": "volume",
                "total_bytes": int(disk.total),
                "used_bytes": int(disk.used),
                "free_bytes": int(disk.free),
                "volume": probe.drive or probe.anchor,
            }
        except Exception as exc:
            usage["local"] = {"error": str(exc)}
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
        for name, remote in (("google_drive", self.drive), ("r2", self.r2)):
            try:
                snapshots[name] = remote.fetch_manifest(archive_date)
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
    ) -> DownloadResult:
        archive_date = validate_archive_date(archive_date)
        snapshots, warnings = self._load_snapshots(archive_date)
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
        active_source = selected
        sources_attempted: list[str] = []
        sources_completed: list[str] = []
        if progress:
            progress(f"下载来源：{replica_label(active_source)}", 0, 10_000)
        if detail_progress:
            detail_progress(
                DownloadProgress(
                    archive_date=archive_date,
                    stage="preparing",
                    overall_current=0,
                    source=active_source,
                    object_count=object_count,
                    bytes_total=download_total,
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
                            )
                        )
                    return DownloadResult(
                        "already_verified",
                        archive_date,
                        selected,
                        final,
                        self.report_path(archive_date),
                        snapshot.object_count,
                        snapshot.row_count,
                        0,
                        warnings,
                    )
            raise RuntimeError("本地正式目录已存在但无法匹配云端，请人工检查")

        staging = self.staging_root(archive_date)
        staging.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        completed_bytes = 0
        last_overall = 0
        for object_index, item in enumerate(objects, start=1):
            relative_key = str(item["relative_key"])
            inside = PureArchivePath.strip_date(relative_key, archive_date)
            destination = staging / Path(*inside.parts)
            expected_size = int(item["size_bytes"])
            expected_sha256 = str(item["sha256"])
            object_high_water = 0
            completed = False
            attempt_order = [active_source] + [
                name for name in compatible_sources if name != active_source
            ]
            for source_name in attempt_order:
                remote = self.drive if source_name == "google_drive" else self.r2
                if source_name not in sources_attempted:
                    sources_attempted.append(source_name)
                attempt_start: int | None = None
                attempt_high_water = 0

                def object_progress(
                    name: str,
                    current: int,
                    total: int,
                    *,
                    source: str = source_name,
                ) -> None:
                    nonlocal attempt_high_water, attempt_start, object_high_water, last_overall
                    normalized_current = min(max(int(current), 0), expected_size)
                    if attempt_start is None:
                        attempt_start = normalized_current
                    attempt_high_water = max(
                        attempt_high_water,
                        max(normalized_current - attempt_start, 0),
                    )
                    object_high_water = max(object_high_water, normalized_current)
                    if download_total:
                        fraction = (
                            completed_bytes + object_high_water
                        ) / download_total
                    else:
                        fraction = 1.0
                    last_overall = max(last_overall, int(fraction * 7_000))
                    if progress:
                        progress(
                            f"下载 · {replica_label(source)} · {PurePosixPath(name).name}",
                            last_overall,
                            10_000,
                        )
                    if detail_progress:
                        detail_progress(
                            DownloadProgress(
                                archive_date=archive_date,
                                stage="downloading",
                                overall_current=last_overall,
                                source=source,
                                object_name=PurePosixPath(name).name,
                                object_index=object_index,
                                object_count=object_count,
                                object_bytes_completed=normalized_current,
                                object_bytes_total=expected_size,
                                bytes_completed=min(
                                    completed_bytes + object_high_water,
                                    download_total,
                                ),
                                bytes_total=download_total,
                                network_bytes_completed=downloaded + attempt_high_water,
                                stage_current=object_index,
                                stage_total=object_count,
                            )
                        )

                try:
                    transferred = remote.download_object(
                        relative_key,
                        destination,
                        expected_size,
                        expected_sha256,
                        object_progress,
                    )
                    downloaded += transferred
                except Exception as exc:
                    if source_name == attempt_order[-1]:
                        raise RuntimeError(
                            f"{replica_label(source_name)} 下载失败：{exc}"
                        ) from exc
                    fallback = attempt_order[attempt_order.index(source_name) + 1]
                    warning = (
                        f"{replica_label(source_name)} 下载失败，已切换到 "
                        f"{replica_label(fallback)}：{exc}"
                    )
                    warnings.append(warning)
                    if progress:
                        progress(
                            f"来源切换：{replica_label(fallback)}",
                            last_overall,
                            10_000,
                        )
                    if detail_progress:
                        detail_progress(
                            DownloadProgress(
                                archive_date=archive_date,
                                stage="switching",
                                overall_current=last_overall,
                                source=fallback,
                                object_name=PurePosixPath(relative_key).name,
                                object_index=object_index,
                                object_count=object_count,
                                object_bytes_completed=object_high_water,
                                object_bytes_total=expected_size,
                                bytes_completed=min(
                                    completed_bytes + object_high_water,
                                    download_total,
                                ),
                                bytes_total=download_total,
                                network_bytes_completed=downloaded,
                                stage_current=object_index,
                                stage_total=object_count,
                            )
                        )
                    continue
                active_source = source_name
                if source_name not in sources_completed:
                    sources_completed.append(source_name)
                completed = True
                break
            if not completed:
                raise RuntimeError(f"归档对象下载失败：{relative_key}")
            completed_bytes += expected_size
            last_overall = max(
                last_overall,
                int((completed_bytes / download_total) * 7_000)
                if download_total
                else 7_000,
            )
            if progress:
                progress(
                    f"下载 · {replica_label(active_source)} · {PurePosixPath(relative_key).name}",
                    last_overall,
                    10_000,
                )
            if detail_progress:
                detail_progress(
                    DownloadProgress(
                        archive_date=archive_date,
                        stage="downloading",
                        overall_current=last_overall,
                        source=active_source,
                        object_name=PurePosixPath(relative_key).name,
                        object_index=object_index,
                        object_count=object_count,
                        object_bytes_completed=expected_size,
                        object_bytes_total=expected_size,
                        bytes_completed=completed_bytes,
                        bytes_total=download_total,
                        network_bytes_completed=downloaded,
                        stage_current=object_index,
                        stage_total=object_count,
                    )
                )

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
                    )
                )

        report = verify_local_day(staging, snapshot, verification_progress)
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
        write_json_atomic(report_path, report)
        write_json_atomic(staging / ".smsi-verified.json", report)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, final)
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
        self, archive_date: str, progress: ProgressCallback | None = None
    ) -> dict[str, Any]:
        archive_date = validate_archive_date(archive_date)
        root = self.final_root(archive_date)
        if not root.is_dir():
            raise RuntimeError("本地日期目录不存在")
        cleanup_receipt = self._completed_cleanup_receipt(archive_date)
        if cleanup_receipt:
            snapshot = self._local_snapshot(archive_date)
            report = verify_local_day(root, snapshot, progress)
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
        snapshot = snapshots.get(self.config.preferred_replica) or next(
            iter(snapshots.values())
        )
        report = verify_local_day(root, snapshot, progress)
        report["replicas_match"] = len(snapshots) == 2 and self.replicas_match(
            snapshots["google_drive"], snapshots["r2"]
        )
        write_json_atomic(self.report_path(archive_date), report)
        write_json_atomic(root / ".smsi-verified.json", report)
        return report

    def run_once(self, archive_date: str | None = None) -> DownloadResult:
        return self.download_day(archive_date or utc_yesterday())

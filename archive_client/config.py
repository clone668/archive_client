from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import urlparse


APP_NAME = "SMSIArchiveClient"
CONFIG_SCHEMA_VERSION = 2
REMOTE_RE = re.compile(r"^[A-Za-z0-9._-]+:$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def _valid_prefix(value: str) -> bool:
    if not value or "\\" in value:
        return False
    path = PurePosixPath(value.strip("/"))
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def app_data_dir() -> Path:
    base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class AppConfig:
    profile_id: str = "tencent-paper"
    display_name: str = "tencent-paper"
    enabled: bool = True
    collector_id: str = "tencent-paper"
    drive_remote: str = "gdrive:"
    drive_prefix: str = "smsi/v3"
    rclone_binary: str = "rclone"
    r2_endpoint: str = ""
    r2_bucket: str = "smsi-archive-tencent-paper"
    r2_prefix: str = "smsi/v3"
    r2_region: str = "auto"
    local_root: str = r"D:\SMSI-Archive"
    preferred_replica: str = "google_drive"
    require_both_replicas: bool = True
    history_days: int = 45
    download_workers: int = 4

    @property
    def drive_root(self) -> str:
        prefix = self.drive_prefix.strip("/")
        return f"{self.drive_remote}{prefix}/collector={self.collector_id}"

    @property
    def r2_root(self) -> str:
        prefix = self.r2_prefix.strip("/")
        return f"{prefix}/collector={self.collector_id}"

    @property
    def archive_root(self) -> Path:
        return Path(self.local_root).expanduser().resolve()

    def validate_identity(self) -> list[str]:
        errors: list[str] = []
        safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        if not self.profile_id or any(char not in safe for char in self.profile_id):
            errors.append("配置 ID 无效")
        if not self.display_name.strip():
            errors.append("配置名称不能为空")
        if not self.collector_id or any(char not in safe for char in self.collector_id):
            errors.append("采集流 ID 无效")
        return errors

    @property
    def label(self) -> str:
        name = self.display_name.strip() or self.profile_id
        return f"{name} · collector={self.collector_id}"

    def validate_drive(self) -> list[str]:
        errors: list[str] = []
        if not REMOTE_RE.fullmatch(self.drive_remote):
            errors.append("Google Drive remote 应为类似 gdrive: 的名称")
        if not _valid_prefix(self.drive_prefix):
            errors.append("Google Drive 对象前缀无效")
        if not self.rclone_binary.strip():
            errors.append("rclone 路径不能为空")
        return errors

    def validate_r2(self) -> list[str]:
        errors: list[str] = []
        endpoint = urlparse(self.r2_endpoint)
        if endpoint.scheme != "https" or not endpoint.netloc:
            errors.append("R2 Endpoint 必须是有效的 https:// 地址")
        if not BUCKET_RE.fullmatch(self.r2_bucket):
            errors.append("R2 Bucket 名称无效")
        if not _valid_prefix(self.r2_prefix):
            errors.append("R2 对象前缀无效")
        if not self.r2_region.strip():
            errors.append("R2 Region 不能为空")
        return errors

    def validate_local(self) -> list[str]:
        errors: list[str] = []
        if not self.local_root.strip():
            errors.append("本地目录不能为空")
        elif Path(self.local_root).expanduser().exists() and not Path(
            self.local_root
        ).expanduser().is_dir():
            errors.append("本地目录指向了文件")
        return errors

    def validate_policy(self) -> list[str]:
        errors: list[str] = []
        if self.preferred_replica not in {"google_drive", "r2"}:
            errors.append("首选副本无效")
        try:
            history_days = int(self.history_days)
        except (TypeError, ValueError):
            history_days = 0
        if not 7 <= history_days <= 3650:
            errors.append("显示历史天数必须在 7 到 3650 之间")
        try:
            download_workers = int(self.download_workers)
        except (TypeError, ValueError):
            download_workers = 0
        if not 1 <= download_workers <= 8:
            errors.append("并发下载数必须在 1 到 8 之间")
        return errors

    def validate(self) -> list[str]:
        return [
            *self.validate_identity(),
            *self.validate_drive(),
            *self.validate_r2(),
            *self.validate_local(),
            *self.validate_policy(),
        ]


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_data_dir() / "config.json"

    @staticmethod
    def _from_payload(payload: dict, fallback_id: str = "") -> AppConfig:
        allowed = set(AppConfig.__dataclass_fields__)
        values = {key: value for key, value in payload.items() if key in allowed}
        profile_id = str(values.get("profile_id") or fallback_id or "default")
        values["profile_id"] = profile_id
        values.setdefault("display_name", profile_id)
        return AppConfig(**values)

    @staticmethod
    def _validate_profiles(profiles: list[AppConfig]) -> None:
        profile_ids: set[str] = set()
        collector_ids: set[str] = set()
        for profile in profiles:
            identity_errors = profile.validate_identity()
            if identity_errors:
                raise RuntimeError(
                    f"配置档案 {profile.profile_id!r} 身份无效："
                    + "；".join(identity_errors)
                )
            if profile.profile_id in profile_ids:
                raise RuntimeError(f"配置 ID 重复: {profile.profile_id}")
            if profile.collector_id in collector_ids:
                raise RuntimeError(f"采集流 ID 重复: {profile.collector_id}")
            profile_ids.add(profile.profile_id)
            collector_ids.add(profile.collector_id)

    def load_profiles(self) -> tuple[list[AppConfig], str]:
        if not self.path.exists():
            default = AppConfig()
            return [default], default.profile_id
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"客户端配置损坏: {self.path}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"客户端配置损坏: {self.path}")

        if raw.get("schema_version") == CONFIG_SCHEMA_VERSION:
            stored = raw.get("profiles")
            if not isinstance(stored, dict) or not stored:
                raise RuntimeError("客户端配置没有可用的采集服务器档案")
            profiles: list[AppConfig] = []
            for stored_id, payload in stored.items():
                if not isinstance(payload, dict):
                    raise RuntimeError(f"配置档案 {stored_id!r} 内容无效")
                stored_id = str(stored_id)
                payload_id = str(payload.get("profile_id") or stored_id)
                if payload_id != stored_id:
                    raise RuntimeError(
                        f"配置档案键 {stored_id!r} 与配置 ID {payload_id!r} 不一致"
                    )
                profiles.append(self._from_payload(payload, stored_id))
            if not profiles:
                raise RuntimeError("客户端配置没有可用的采集服务器档案")
            self._validate_profiles(profiles)
            ids = {profile.profile_id for profile in profiles}
            active = str(raw.get("active_profile_id") or "")
            if active not in ids:
                active = profiles[0].profile_id
            return profiles, active

        legacy_id = str(raw.get("profile_id") or raw.get("collector_id") or "default")
        profile = self._from_payload(raw, legacy_id)
        self._validate_profiles([profile])
        return [profile], profile.profile_id

    def load(self) -> AppConfig:
        profiles, active = self.load_profiles()
        return next(
            profile for profile in profiles if profile.profile_id == active
        )

    def load_profile(self, profile_id: str) -> AppConfig:
        profiles, _ = self.load_profiles()
        for profile in profiles:
            if profile.profile_id == profile_id:
                return profile
        raise KeyError(f"配置档案不存在: {profile_id}")

    def _write(self, profiles: list[AppConfig], active_profile_id: str) -> None:
        if not profiles:
            raise ValueError("至少需要保留一个采集服务器档案")
        self._validate_profiles(profiles)
        ids = {profile.profile_id for profile in profiles}
        if active_profile_id not in ids:
            raise ValueError("活动配置档案不存在")
        payload = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "active_profile_id": active_profile_id,
            "profiles": {
                profile.profile_id: asdict(profile) for profile in profiles
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def save(self, config: AppConfig) -> None:
        errors = config.validate()
        if errors:
            raise ValueError("；".join(errors))
        profiles, _ = self.load_profiles()
        for profile in profiles:
            if (
                profile.profile_id != config.profile_id
                and profile.collector_id == config.collector_id
            ):
                raise ValueError(
                    f"采集流 ID 已被配置 {profile.display_name} 使用"
                )
        updated = [
            config if profile.profile_id == config.profile_id else profile
            for profile in profiles
        ]
        if not any(profile.profile_id == config.profile_id for profile in profiles):
            updated.append(config)
        self._write(updated, config.profile_id)

    def set_active(self, profile_id: str) -> None:
        profiles, _ = self.load_profiles()
        if not any(profile.profile_id == profile_id for profile in profiles):
            raise KeyError(f"配置档案不存在: {profile_id}")
        self._write(profiles, profile_id)

    def delete_profile(self, profile_id: str) -> str:
        profiles, active = self.load_profiles()
        remaining = [
            profile for profile in profiles if profile.profile_id != profile_id
        ]
        if len(remaining) == len(profiles):
            raise KeyError(f"配置档案不存在: {profile_id}")
        if not remaining:
            raise ValueError("至少需要保留一个采集服务器档案")
        next_active = active if active != profile_id else remaining[0].profile_id
        self._write(remaining, next_active)
        return next_active

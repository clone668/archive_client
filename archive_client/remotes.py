from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Any

from .config import AppConfig
from .models import ManifestSnapshot, ProgressCallback


DATE_DIR_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})/$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
METADATA_SUFFIX = ".smsi-metadata.json"
USAGE_CACHE_SECONDS = 300


def resolve_rclone_binary(configured: str) -> str | None:
    value = configured.strip() or "rclone"
    resolved = shutil.which(value)
    if resolved:
        return resolved
    path = Path(value).expanduser()
    if path.is_file():
        return str(path.resolve())
    if path.name.lower() not in {"rclone", "rclone.exe"}:
        return None

    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        return None
    winget_root = Path(local_app_data) / "Microsoft" / "WinGet"
    candidates = [winget_root / "Links" / "rclone.exe"]
    packages = winget_root / "Packages"
    candidates.extend(
        sorted(
            packages.glob("Rclone.Rclone_*/*/rclone.exe"),
            reverse=True,
        )
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_relative_key(value: Any, archive_date: str | None = None) -> str:
    key = str(value or "").strip("/")
    path = PurePosixPath(key)
    if not key or path.is_absolute() or ".." in path.parts or "\\" in key:
        raise RuntimeError("归档对象路径不安全")
    if archive_date and not key.startswith(f"date={archive_date}/"):
        raise RuntimeError("归档对象日期前缀不匹配")
    return key


def validate_manifest(
    replica: str,
    archive_date: str,
    raw: bytes,
    expected_sha256: str,
    expected_size: int,
) -> ManifestSnapshot:
    if len(raw) != expected_size:
        raise RuntimeError(f"{replica} manifest 大小校验失败")
    observed_sha256 = sha256_bytes(raw)
    if observed_sha256 != expected_sha256:
        raise RuntimeError(f"{replica} manifest SHA-256 校验失败")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{replica} manifest 不是有效 JSON") from exc
    if (
        manifest.get("contract_version") != "smsi-long-term-archive-manifest/v3"
        or manifest.get("status") != "verified"
        or manifest.get("retention_delete_allowed") is not True
        or manifest.get("archive_date") != archive_date
        or not isinstance(manifest.get("objects"), list)
    ):
        raise RuntimeError(f"{replica} manifest 尚未达到可恢复状态")
    objects = manifest["objects"]
    if len(objects) != int(manifest.get("object_count", -1)):
        raise RuntimeError(f"{replica} manifest 对象数不一致")
    seen: set[str] = set()
    total_rows = 0
    for item in objects:
        key = validate_relative_key(item.get("relative_key"), archive_date)
        if key in seen:
            raise RuntimeError(f"{replica} manifest 包含重复对象")
        seen.add(key)
        if int(item.get("size_bytes", -1)) < 0:
            raise RuntimeError(f"{replica} manifest 对象大小无效")
        if not SHA256_RE.fullmatch(str(item.get("sha256") or "")):
            raise RuntimeError(f"{replica} manifest 对象 SHA-256 无效")
        total_rows += int(item.get("row_count") or 0)
    if total_rows != int(manifest.get("row_count", -1)):
        raise RuntimeError(f"{replica} manifest 总行数不一致")
    return ManifestSnapshot(
        replica=replica,
        archive_date=archive_date,
        manifest=manifest,
        raw=raw,
        sha256=observed_sha256,
    )


class ArchiveRemote(ABC):
    name: str

    @abstractmethod
    def list_dates(self) -> set[str]:
        raise NotImplementedError

    @abstractmethod
    def fetch_manifest(self, archive_date: str) -> ManifestSnapshot:
        raise NotImplementedError

    @abstractmethod
    def verify_object_metadata(
        self, relative_key: str, expected_size: int, expected_sha256: str
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def download_object(
        self,
        relative_key: str,
        destination: Path,
        expected_size: int,
        expected_sha256: str,
        progress: ProgressCallback | None = None,
    ) -> int:
        raise NotImplementedError

    def storage_usage(self) -> dict[str, Any]:
        raise RuntimeError(f"{self.name} 不支持容量统计")

    def invalidate_usage_cache(self) -> None:
        return None


class UnavailableRemote(ArchiveRemote):
    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    def _raise(self) -> None:
        raise RuntimeError(self.reason)

    def list_dates(self) -> set[str]:
        self._raise()
        return set()

    def fetch_manifest(self, archive_date: str) -> ManifestSnapshot:
        self._raise()
        raise AssertionError(archive_date)

    def verify_object_metadata(
        self, relative_key: str, expected_size: int, expected_sha256: str
    ) -> None:
        self._raise()

    def download_object(
        self,
        relative_key: str,
        destination: Path,
        expected_size: int,
        expected_sha256: str,
        progress: ProgressCallback | None = None,
    ) -> int:
        self._raise()
        return 0

    def storage_usage(self) -> dict[str, Any]:
        self._raise()
        return {}


class DriveRemote(ArchiveRemote):
    name = "google_drive"

    def __init__(self, config: AppConfig) -> None:
        errors = [*config.validate_identity(), *config.validate_drive()]
        if errors:
            raise RuntimeError("Google Drive 配置无效：" + "；".join(errors))
        self.config = config
        self._usage_cache: tuple[float, dict[str, Any]] | None = None

    def _binary(self) -> str:
        configured = self.config.rclone_binary.strip() or "rclone"
        resolved = resolve_rclone_binary(configured)
        if not resolved:
            raise RuntimeError("未找到 rclone，请先安装或在设置中指定路径")
        return resolved

    def _remote_path(self, relative_key: str = "") -> str:
        suffix = validate_relative_key(relative_key) if relative_key else ""
        return "/".join(
            part for part in (self.config.drive_root.rstrip("/"), suffix) if part
        )

    @staticmethod
    def _creation_flags() -> int:
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def _run(
        self, arguments: list[str], timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self._binary(), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=True,
                creationflags=self._creation_flags(),
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "rclone 执行失败").strip()
            raise RuntimeError(detail) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("rclone 请求超时") from exc

    def list_dates(self) -> set[str]:
        result = self._run(["lsf", self._remote_path(), "--dirs-only"], timeout=60)
        dates = set()
        for line in result.stdout.splitlines():
            match = DATE_DIR_RE.fullmatch(line.strip())
            if match:
                dates.add(match.group(1))
        return dates

    def storage_usage(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._usage_cache and now - self._usage_cache[0] < USAGE_CACHE_SECONDS:
            return dict(self._usage_cache[1])
        result = self._run(
            ["about", self.config.drive_remote, "--json"], timeout=60
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Google Drive 容量响应不是有效 JSON") from exc
        usage = {
            "scope": "account",
            "total_bytes": int(payload.get("total") or 0),
            "used_bytes": int(payload.get("used") or 0),
            "free_bytes": int(payload.get("free") or 0),
            "trashed_bytes": int(payload.get("trashed") or 0),
        }
        if any(
            int(usage[key]) < 0
            for key in ("total_bytes", "used_bytes", "free_bytes", "trashed_bytes")
        ):
            raise RuntimeError("Google Drive 容量响应包含负数")
        self._usage_cache = (now, usage)
        return dict(usage)

    def invalidate_usage_cache(self) -> None:
        self._usage_cache = None

    def _cat(self, relative_key: str) -> bytes:
        try:
            result = subprocess.run(
                [self._binary(), "cat", self._remote_path(relative_key)],
                capture_output=True,
                timeout=120,
                check=True,
                creationflags=self._creation_flags(),
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or "Google Drive 读取失败") from exc
        return result.stdout

    def _metadata(self, relative_key: str) -> dict[str, Any]:
        raw = self._cat(relative_key + METADATA_SUFFIX)
        try:
            metadata = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Google Drive 元数据损坏") from exc
        if metadata.get("relative_key") != relative_key:
            raise RuntimeError("Google Drive 元数据路径不匹配")
        return metadata

    def fetch_manifest(self, archive_date: str) -> ManifestSnapshot:
        relative = f"date={archive_date}/manifest.json"
        metadata = self._metadata(relative)
        raw = self._cat(relative)
        return validate_manifest(
            self.name,
            archive_date,
            raw,
            str(metadata.get("sha256") or ""),
            int(metadata.get("size_bytes", -1)),
        )

    def verify_object_metadata(
        self, relative_key: str, expected_size: int, expected_sha256: str
    ) -> None:
        metadata = self._metadata(validate_relative_key(relative_key))
        if (
            int(metadata.get("size_bytes", -1)) != int(expected_size)
            or metadata.get("sha256") != expected_sha256
        ):
            raise RuntimeError(f"Google Drive 对象元数据不匹配: {relative_key}")

    def download_object(
        self,
        relative_key: str,
        destination: Path,
        expected_size: int,
        expected_sha256: str,
        progress: ProgressCallback | None = None,
    ) -> int:
        relative_key = validate_relative_key(relative_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if (
                destination.stat().st_size == expected_size
                and sha256_file(destination) == expected_sha256
            ):
                return 0
            destination.unlink()
        temporary = destination.with_suffix(destination.suffix + ".partial")
        if temporary.exists():
            temporary.unlink()
        if progress:
            progress(relative_key, 0, expected_size)
        timeout = max(600, expected_size // (256 * 1024))
        process = subprocess.Popen(
            [
                self._binary(),
                "copyto",
                self._remote_path(relative_key),
                str(temporary),
                "--inplace",
                "--retries",
                "5",
                "--low-level-retries",
                "20",
                "--timeout",
                "2m",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=self._creation_flags(),
        )
        stderr_lines: deque[str] = deque(maxlen=80)

        def read_stderr() -> None:
            if process.stderr is None:
                return
            for line in process.stderr:
                stderr_lines.append(line.rstrip())

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if progress and temporary.exists():
                    progress(
                        relative_key,
                        min(temporary.stat().st_size, expected_size),
                        expected_size,
                    )
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait()
                    raise RuntimeError("rclone 请求超时")
                time.sleep(0.25)
        finally:
            stderr_thread.join(timeout=2)
        if process.returncode:
            detail = "\n".join(stderr_lines).strip()
            raise RuntimeError(detail or "rclone 执行失败")
        if temporary.stat().st_size != expected_size:
            raise RuntimeError(f"Google Drive 下载大小不一致: {relative_key}")
        if sha256_file(temporary) != expected_sha256:
            raise RuntimeError(f"Google Drive 下载哈希不一致: {relative_key}")
        temporary.replace(destination)
        if progress:
            progress(relative_key, expected_size, expected_size)
        return expected_size


class R2Remote(ArchiveRemote):
    name = "r2"

    def __init__(
        self,
        config: AppConfig,
        access_key_id: str,
        secret_access_key: str,
        client: Any | None = None,
    ) -> None:
        errors = [*config.validate_identity(), *config.validate_r2()]
        if errors:
            raise RuntimeError("Cloudflare R2 配置无效：" + "；".join(errors))
        self.config = config
        if client is None and (not access_key_id or not secret_access_key):
            raise RuntimeError("尚未保存 R2 只读凭据")
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise RuntimeError("缺少 boto3，请先安装客户端依赖") from exc
            client = boto3.client(
                "s3",
                endpoint_url=config.r2_endpoint,
                region_name=config.r2_region or "auto",
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 8, "mode": "adaptive"},
                    connect_timeout=15,
                    read_timeout=120,
                ),
            )
        self.client = client
        self._usage_cache: tuple[float, dict[str, Any]] | None = None

    def _key(self, relative_key: str = "") -> str:
        suffix = validate_relative_key(relative_key) if relative_key else ""
        return "/".join(part for part in (self.config.r2_root, suffix) if part)

    def list_dates(self) -> set[str]:
        prefix = self._key().rstrip("/") + "/"
        dates: set[str] = set()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.config.r2_bucket, Prefix=prefix, Delimiter="/"
        ):
            for item in page.get("CommonPrefixes") or []:
                name = str(item.get("Prefix") or "").removeprefix(prefix)
                match = DATE_DIR_RE.fullmatch(name)
                if match:
                    dates.add(match.group(1))
        return dates

    def storage_usage(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._usage_cache and now - self._usage_cache[0] < USAGE_CACHE_SECONDS:
            return dict(self._usage_cache[1])
        archive_prefix = self._key().rstrip("/") + "/"
        bucket_objects = 0
        bucket_bytes = 0
        archive_objects = 0
        archive_bytes = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.r2_bucket):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                size = int(item.get("Size") or 0)
                if size < 0:
                    raise RuntimeError("Cloudflare R2 返回了负对象大小")
                bucket_objects += 1
                bucket_bytes += size
                if key.startswith(archive_prefix):
                    archive_objects += 1
                    archive_bytes += size
        usage = {
            "scope": "bucket",
            "bucket": self.config.r2_bucket,
            "bucket_objects": bucket_objects,
            "bucket_bytes": bucket_bytes,
            "archive_objects": archive_objects,
            "archive_bytes": archive_bytes,
            "total_bytes": None,
        }
        self._usage_cache = (now, usage)
        return dict(usage)

    def invalidate_usage_cache(self) -> None:
        self._usage_cache = None

    @staticmethod
    def _read_body(body: Any) -> bytes:
        try:
            return body.read()
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def _head(self, relative_key: str) -> dict[str, Any]:
        return self.client.head_object(
            Bucket=self.config.r2_bucket, Key=self._key(relative_key)
        )

    @staticmethod
    def _head_sha256(head: dict[str, Any]) -> str:
        metadata = {
            str(key).lower(): str(value)
            for key, value in (head.get("Metadata") or {}).items()
        }
        return metadata.get("sha256") or ""

    def fetch_manifest(self, archive_date: str) -> ManifestSnapshot:
        relative = f"date={archive_date}/manifest.json"
        head = self._head(relative)
        response = self.client.get_object(
            Bucket=self.config.r2_bucket, Key=self._key(relative)
        )
        raw = self._read_body(response["Body"])
        return validate_manifest(
            self.name,
            archive_date,
            raw,
            self._head_sha256(head),
            int(head.get("ContentLength", -1)),
        )

    def verify_object_metadata(
        self, relative_key: str, expected_size: int, expected_sha256: str
    ) -> None:
        head = self._head(validate_relative_key(relative_key))
        if (
            int(head.get("ContentLength", -1)) != int(expected_size)
            or self._head_sha256(head) != expected_sha256
        ):
            raise RuntimeError(f"R2 对象元数据不匹配: {relative_key}")

    def download_object(
        self,
        relative_key: str,
        destination: Path,
        expected_size: int,
        expected_sha256: str,
        progress: ProgressCallback | None = None,
    ) -> int:
        relative_key = validate_relative_key(relative_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if (
                destination.stat().st_size == expected_size
                and sha256_file(destination) == expected_sha256
            ):
                return 0
            destination.unlink()
        temporary = destination.with_suffix(destination.suffix + ".partial")
        offset = temporary.stat().st_size if temporary.exists() else 0
        if offset > expected_size:
            temporary.unlink()
            offset = 0
        if offset == expected_size:
            if sha256_file(temporary) == expected_sha256:
                temporary.replace(destination)
                return 0
            temporary.unlink()
            offset = 0
        arguments: dict[str, Any] = {
            "Bucket": self.config.r2_bucket,
            "Key": self._key(relative_key),
        }
        if offset:
            arguments["Range"] = f"bytes={offset}-"
        if progress:
            progress(relative_key, offset, expected_size)
        response = self.client.get_object(**arguments)
        body = response["Body"]
        downloaded = 0
        try:
            with temporary.open("ab" if offset else "wb") as handle:
                for chunk in iter(lambda: body.read(8 * 1024 * 1024), b""):
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(relative_key, offset + downloaded, expected_size)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if temporary.stat().st_size != expected_size:
            raise RuntimeError(f"R2 下载大小不一致: {relative_key}")
        if sha256_file(temporary) != expected_sha256:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"R2 下载哈希不一致: {relative_key}")
        temporary.replace(destination)
        return downloaded

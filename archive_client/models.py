from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


ProgressCallback = Callable[[str, int, int], None]


class CancellationToken(Protocol):
    def is_set(self) -> bool: ...


class OperationCancelled(RuntimeError):
    """Raised after an in-flight operation has stopped without publishing output."""


def raise_if_cancelled(cancel: CancellationToken | None) -> None:
    if cancel is not None and cancel.is_set():
        raise OperationCancelled("操作已取消，未完成结果不会发布")


@dataclass(frozen=True)
class DownloadProgress:
    archive_date: str
    stage: str
    overall_current: int
    overall_total: int = 10_000
    source: str | None = None
    object_name: str | None = None
    object_index: int = 0
    object_count: int = 0
    object_bytes_completed: int = 0
    object_bytes_total: int = 0
    bytes_completed: int = 0
    bytes_total: int = 0
    network_bytes_completed: int = 0
    stage_current: int = 0
    stage_total: int = 0
    completed_objects: int = 0
    active_transfers: int = 0
    download_workers: int = 1


DownloadProgressCallback = Callable[[DownloadProgress], None]


@dataclass(frozen=True)
class ManifestSnapshot:
    replica: str
    archive_date: str
    manifest: dict[str, Any]
    raw: bytes
    sha256: str

    @property
    def object_count(self) -> int:
        return int(self.manifest.get("object_count") or 0)

    @property
    def row_count(self) -> int:
        return int(self.manifest.get("row_count") or 0)


@dataclass
class ReplicaDayStatus:
    state: str
    detail: str = ""
    snapshot: ManifestSnapshot | None = None


@dataclass
class ArchiveDayStatus:
    archive_date: str
    drive: ReplicaDayStatus
    r2: ReplicaDayStatus
    local_state: str
    local_detail: str = ""
    replicas_match: bool | None = None


@dataclass
class DownloadResult:
    status: str
    archive_date: str
    replica: str
    destination: Path
    report_path: Path
    object_count: int
    row_count: int
    bytes_downloaded: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class BatchDownloadResult:
    requested_dates: list[str]
    results: list[DownloadResult] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)

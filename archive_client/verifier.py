from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .models import ManifestSnapshot, ProgressCallback
from .remotes import sha256_file, validate_relative_key


def canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {
            str(key): canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    if isinstance(value, float) and (
        value != value or value in (float("inf"), float("-inf"))
    ):
        raise ValueError("归档包含非有限浮点数")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def update_content_digest(digest: Any, row: Mapping[str, Any]) -> None:
    payload = json.dumps(
        canonical_value(dict(row)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def verify_parquet(path: Path, item: Mapping[str, Any]) -> dict[str, Any]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("缺少 pyarrow，无法执行完整 Parquet 校验") from exc

    parquet = pq.ParquetFile(path)
    try:
        row_count = int(parquet.metadata.num_rows)
        expected_rows = int(item.get("row_count") or 0)
        if row_count != expected_rows:
            raise RuntimeError(f"Parquet 行数不一致: {path}")
        schema_sha256 = hashlib.sha256(
            str(parquet.schema_arrow).encode("utf-8")
        ).hexdigest()
        if schema_sha256 != str(item.get("schema_sha256") or ""):
            raise RuntimeError(f"Parquet schema 不一致: {path}")

        content_sha256: str | None = None
        if item.get("kind") == "business":
            expected_content = str(item.get("content_sha256") or "")
            if len(expected_content) != 64:
                raise RuntimeError(f"业务内容摘要缺失: {path}")
            digest = hashlib.sha256()
            for batch in parquet.iter_batches(batch_size=10_000):
                for row in batch.to_pylist():
                    update_content_digest(digest, row)
            content_sha256 = digest.hexdigest()
            if content_sha256 != expected_content:
                raise RuntimeError(f"业务内容摘要不一致: {path}")
    finally:
        parquet.close()
    return {
        "row_count": row_count,
        "schema_sha256": schema_sha256,
        "content_sha256": content_sha256,
    }


def verify_local_day(
    root: Path,
    snapshot: ManifestSnapshot,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    objects = snapshot.manifest["objects"]
    verified = []
    total_rows = 0
    tree_material = []
    for index, item in enumerate(objects, start=1):
        relative_key = validate_relative_key(
            item.get("relative_key"), snapshot.archive_date
        )
        relative_inside_day = PureArchivePath.strip_date(relative_key, snapshot.archive_date)
        path = root / Path(*relative_inside_day.parts)
        expected_size = int(item["size_bytes"])
        expected_sha256 = str(item["sha256"])
        if not path.is_file():
            raise RuntimeError(f"本地归档对象缺失: {relative_key}")
        if path.stat().st_size != expected_size:
            raise RuntimeError(f"本地归档对象大小不一致: {relative_key}")
        if sha256_file(path) != expected_sha256:
            raise RuntimeError(f"本地归档对象 SHA-256 不一致: {relative_key}")
        parquet = verify_parquet(path, item)
        total_rows += parquet["row_count"]
        entry = {
            "relative_key": relative_key,
            "kind": item.get("kind"),
            "table_name": item.get("table_name"),
            "size_bytes": expected_size,
            "sha256": expected_sha256,
            **parquet,
        }
        verified.append(entry)
        tree_material.append(
            {
                "relative_key": relative_key,
                "size_bytes": expected_size,
                "sha256": expected_sha256,
                "row_count": parquet["row_count"],
            }
        )
        if progress:
            progress(relative_key, index, len(objects))

    if len(verified) != snapshot.object_count:
        raise RuntimeError("本地归档对象总数不一致")
    if total_rows != snapshot.row_count:
        raise RuntimeError("本地归档总行数不一致")
    tree_sha256 = hashlib.sha256(
        json.dumps(
            canonical_value(tree_material),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "contract_version": "smsi-windows-archive-verification/v1",
        "status": "verified",
        "archive_date": snapshot.archive_date,
        "manifest_sha256": snapshot.sha256,
        "object_count": len(verified),
        "row_count": total_rows,
        "tree_sha256": tree_sha256,
        "verified_at": datetime.now(timezone.utc),
        "retention_delete_allowed": False,
        "objects": verified,
    }


class PureArchivePath:
    @staticmethod
    def strip_date(relative_key: str, archive_date: str):
        from pathlib import PurePosixPath

        path = PurePosixPath(relative_key)
        prefix = f"date={archive_date}"
        if not path.parts or path.parts[0] != prefix:
            raise RuntimeError("归档对象日期目录不匹配")
        return PurePosixPath(*path.parts[1:])

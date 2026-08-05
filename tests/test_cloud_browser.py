from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from archive_client.config import AppConfig
from archive_client.manager import ArchiveManager
from archive_client.remotes import DriveRemote, R2Remote


def make_config(local_root: str = ".") -> AppConfig:
    return AppConfig(
        profile_id="profile-a",
        display_name="Profile A",
        collector_id="collector-a",
        drive_remote="drive-a:",
        drive_prefix="smsi/v3",
        r2_endpoint="https://example.r2.cloudflarestorage.com",
        r2_bucket="smsi-archive-test",
        r2_prefix="smsi/v3",
        r2_region="auto",
        local_root=local_root,
    )


class FakePaginator:
    def __init__(self, client: "FakeR2Client") -> None:
        self.client = client

    def paginate(self, *, Prefix: str = "", Delimiter: str | None = None, **_kwargs):
        contents = []
        prefixes: set[str] = set()
        for key, size in sorted(self.client.objects.items()):
            if not key.startswith(Prefix):
                continue
            remainder = key.removeprefix(Prefix)
            if Delimiter and Delimiter in remainder:
                prefixes.add(Prefix + remainder.split(Delimiter, 1)[0] + Delimiter)
                continue
            contents.append({"Key": key, "Size": size, "LastModified": "2026-08-04T00:00:00Z"})
        yield {
            "CommonPrefixes": [{"Prefix": value} for value in sorted(prefixes)],
            "Contents": contents,
        }


class FakeR2Client:
    def __init__(self, objects: dict[str, int]) -> None:
        self.objects = dict(objects)
        self.deleted: list[str] = []

    def get_paginator(self, _name: str) -> FakePaginator:
        return FakePaginator(self)

    def delete_objects(self, *, Delete, **_kwargs):
        for item in Delete["Objects"]:
            key = item["Key"]
            self.deleted.append(key)
            self.objects.pop(key, None)
        return {}


class CloudBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = "smsi/v3/collector=collector-a/"

    def test_r2_browser_lists_only_direct_children(self) -> None:
        client = FakeR2Client(
            {
                self.root + "date=2026-08-02/manifest.json": 100,
                self.root + "date=2026-08-03/manifest.json": 110,
                self.root + "root-note.txt": 12,
            }
        )
        remote = R2Remote(make_config(), "a", "b", client=client)
        entries = remote.list_entries("")
        self.assertEqual(
            [(item["name"], item["is_dir"]) for item in entries],
            [
                ("date=2026-08-02", True),
                ("date=2026-08-03", True),
                ("root-note.txt", False),
            ],
        )

    def test_file_delete_does_not_delete_same_prefix_sibling(self) -> None:
        target = self.root + "date=2026-08-02/manifest.json"
        sibling = target + ".smsi-metadata.json"
        client = FakeR2Client({target: 100, sibling: 40})
        remote = R2Remote(make_config(), "a", "b", client=client)
        plan = remote.prepare_delete(
            "date=2026-08-02/manifest.json", is_dir=False
        )
        result = remote.execute_delete(plan)
        self.assertEqual(result["deleted_objects"], 1)
        self.assertEqual(client.deleted, [target])
        self.assertIn(sibling, client.objects)

    def test_directory_change_after_confirmation_blocks_delete(self) -> None:
        prefix = self.root + "date=2026-08-02/"
        client = FakeR2Client({prefix + "manifest.json": 100})
        remote = R2Remote(make_config(), "a", "b", client=client)
        plan = remote.prepare_delete("date=2026-08-02", is_dir=True)
        client.objects[prefix + "new-object.parquet"] = 200
        with self.assertRaisesRegex(RuntimeError, "发生变化"):
            remote.execute_delete(plan)
        self.assertEqual(client.deleted, [])

    def test_collector_root_cannot_be_deleted(self) -> None:
        client = FakeR2Client({self.root + "manifest.json": 100})
        remote = R2Remote(make_config(), "a", "b", client=client)
        with self.assertRaisesRegex(RuntimeError, "不能删除网盘根目录"):
            remote.prepare_delete("", is_dir=True)
        self.assertEqual(client.deleted, [])

    def test_directory_delete_is_limited_to_exact_prefix(self) -> None:
        prefix = self.root + "date=2026-08-02/"
        other = self.root + "date=2026-08-020/manifest.json"
        client = FakeR2Client(
            {
                prefix + "manifest.json": 100,
                prefix + "business/data.parquet": 200,
                other: 300,
            }
        )
        remote = R2Remote(make_config(), "a", "b", client=client)
        plan = remote.prepare_delete("date=2026-08-02", is_dir=True)
        result = remote.execute_delete(plan)
        self.assertEqual(result["deleted_objects"], 2)
        self.assertIn(other, client.objects)

    def test_google_drive_browser_maps_direct_entries(self) -> None:
        remote = DriveRemote(make_config())
        listing = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                [
                    {"Name": "date=2026-08-02", "IsDir": True, "Size": -1},
                    {
                        "Name": "note.txt",
                        "IsDir": False,
                        "Size": 20,
                        "ModTime": "2026-08-04T00:00:00Z",
                    },
                ]
            ),
            stderr="",
        )
        remote._run = lambda *_args, **_kwargs: listing  # type: ignore[method-assign]
        entries = remote.list_entries("")
        self.assertEqual(entries[0]["path"], "date=2026-08-02")
        self.assertTrue(entries[0]["is_dir"])
        self.assertEqual(entries[1]["size_bytes"], 20)

    def test_manager_writes_delete_plan_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = self.root + "date=2026-08-02/"
            client = FakeR2Client({prefix + "manifest.json": 100})
            r2 = R2Remote(make_config(temporary), "a", "b", client=client)
            manager = ArchiveManager(
                make_config(temporary), drive=None, r2=r2
            )
            plan = manager.prepare_r2_file_delete(
                "date=2026-08-02", is_dir=True
            )
            receipt = manager.execute_r2_file_delete(Path(plan["report_path"]))
            self.assertEqual(receipt["status"], "completed")
            self.assertTrue(Path(receipt["report_path"]).is_file())


if __name__ == "__main__":
    unittest.main()

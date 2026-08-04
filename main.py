from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone

from archive_client.config import ConfigStore, app_data_dir
from archive_client.credentials import CredentialStore
from archive_client.manager import ArchiveManager
from archive_client.verifier import canonical_value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SMSI Windows archive client")
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--date")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--profile")
    selection.add_argument("--all-profiles", action="store_true")
    return parser.parse_args()


def run_once(
    archive_date: str | None,
    profile_id: str | None = None,
    all_profiles: bool = False,
) -> int:
    log_dir = app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_path = log_dir / f"sync-{stamp}.json"
    store = ConfigStore()
    try:
        profiles, active = store.load_profiles()
    except Exception as exc:
        payload = {
            "success": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "finished_at": datetime.now(timezone.utc),
        }
        log_path.write_text(
            json.dumps(
                canonical_value(payload),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return 1
    if profile_id:
        selected = [
            profile for profile in profiles if profile.profile_id == profile_id
        ]
        if not selected:
            selected = []
            selection_error = f"配置档案不存在: {profile_id}"
        else:
            selection_error = ""
    else:
        selected = [profile for profile in profiles if profile.enabled]
        selection_error = "" if selected else "没有启用的采集服务器档案"

    results = []
    if selection_error:
        results.append({"success": False, "error": selection_error})
    for profile in selected:
        try:
            result = ArchiveManager(profile, CredentialStore()).run_once(
                archive_date
            )
            entry = {
                **canonical_value(result.__dict__),
                "profile_id": profile.profile_id,
                "display_name": profile.display_name,
                "success": True,
            }
        except Exception as exc:
            entry = {
                "profile_id": profile.profile_id,
                "display_name": profile.display_name,
                "success": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        results.append(entry)
    success = bool(results) and all(entry["success"] for entry in results)
    payload = {
        "success": success,
        "active_profile_id": active,
        "all_profiles": all_profiles or not bool(profile_id),
        "profiles": results,
        "finished_at": datetime.now(timezone.utc),
    }
    exit_code = 0 if success else 1
    log_path.write_text(
        json.dumps(canonical_value(payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return exit_code


def main() -> int:
    args = parse_args()
    if args.run_once:
        return run_once(args.date, args.profile, args.all_profiles)
    from archive_client.app import ArchiveClientApp

    app = ArchiveClientApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

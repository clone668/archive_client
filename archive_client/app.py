from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import Any, Callable

from .config import AppConfig, ConfigStore, app_data_dir
from .credentials import CredentialStore
from .manager import ArchiveManager, replica_label, utc_yesterday
from .models import (
    ArchiveDayStatus,
    BatchDownloadResult,
    DownloadProgress,
    DownloadResult,
    OperationCancelled,
)
from .remotes import DriveRemote, R2Remote, resolve_rclone_binary
from .scheduler import install_task, remove_task, run_task, task_installed


BG = "#f3f5f7"
SURFACE = "#ffffff"
INK = "#18212b"
MUTED = "#61707f"
BORDER = "#d7dee5"
ACCENT = "#176b87"
ACCENT_DARK = "#11566e"
GREEN = "#18794e"
AMBER = "#9a6700"
RED = "#b42318"
BLUE = "#176b87"
PALE_BLUE = "#eaf4f7"
PALE_GREEN = "#eaf6ef"
PALE_AMBER = "#fff7df"
PALE_RED = "#fff0ee"
R2_FREE_ALLOWANCE_BYTES = 10 * 1024**3
LOGGER = logging.getLogger(__name__)


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_duration(seconds: float) -> str:
    total = max(int(seconds), 0)
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        minutes, remaining = divmod(total, 60)
        return f"{minutes} 分 {remaining} 秒"
    hours, remaining = divmod(total, 3600)
    minutes = remaining // 60
    return f"{hours} 小时 {minutes} 分"


def compact_name(value: str, limit: int = 40) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


STATE_TEXT = {
    "loading": "... 读取中",
    "verified": "✓ 已验证",
    "cleaned": "✓ 已清理",
    "missing": "○ 缺失",
    "error": "! 异常",
    "partial": "○ 下载未完成",
    "unverified": "? 未验证",
}


class ThinProgressbar(tk.Canvas):
    def __init__(
        self,
        parent: tk.Widget,
        *,
        maximum: float = 100,
        value: float = 0,
        mode: str = "determinate",
    ) -> None:
        super().__init__(
            parent,
            background="#d5dee4",
            borderwidth=0,
            highlightthickness=0,
        )
        self._maximum = maximum
        self._value = value
        self._mode = mode
        self._phase = 0.0
        self._animation_interval = 24
        self._animation_job: str | None = None
        self.bind("<Configure>", lambda _event: self._render())

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if cnf is None and not kwargs:
            return super().configure()
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
        elif cnf is not None:
            return super().configure(cnf, **kwargs)

        if "maximum" in kwargs:
            self._maximum = float(kwargs.pop("maximum"))
        if "value" in kwargs:
            self._value = float(kwargs.pop("value"))
        if "mode" in kwargs:
            self._mode = str(kwargs.pop("mode"))
        result = super().configure(**kwargs) if kwargs else None
        self._render()
        return result

    config = configure

    def start(self, interval: int = 24) -> None:
        self.stop()
        self._mode = "indeterminate"
        self._animation_interval = max(int(interval), 16)
        self._phase = min(max(self._value / max(self._maximum, 1), 0), 1)
        self._tick()

    def stop(self) -> None:
        if self._animation_job is None:
            return
        try:
            self.after_cancel(self._animation_job)
        except tk.TclError:
            pass
        self._animation_job = None

    def destroy(self) -> None:
        self.stop()
        super().destroy()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.025) % 1
        self._render()
        self._animation_job = self.after(self._animation_interval, self._tick)

    def _render(self) -> None:
        width = max(self.winfo_width(), 1)
        height = max(self.winfo_height(), 1)
        self.delete("progress")
        if self._mode == "indeterminate":
            chunk_width = max(int(width * 0.14), 48)
            left = int(self._phase * (width + chunk_width)) - chunk_width
            right = left + chunk_width
        else:
            left = 0
            ratio = min(max(self._value / max(self._maximum, 1), 0), 1)
            right = int(width * ratio)
        if right > 0 and left < width:
            self.create_rectangle(
                max(left, 0),
                0,
                min(right, width),
                height,
                fill=ACCENT,
                outline="",
                tags="progress",
            )


class SettingsPanel(ttk.Frame):
    def __init__(
        self,
        parent: tk.Widget,
        app: "ArchiveClientApp",
        config_store: ConfigStore,
        credentials: CredentialStore,
        config: AppConfig,
        *,
        is_new: bool = False,
    ) -> None:
        super().__init__(parent)
        self.parent = app
        self.config_store = config_store
        self.credentials = credentials
        self.config = config
        self.is_new = is_new
        self._disposed = False

        self.vars: dict[str, tk.Variable] = {
            "profile_id": tk.StringVar(value=config.profile_id),
            "display_name": tk.StringVar(value=config.display_name),
            "enabled": tk.BooleanVar(value=config.enabled),
            "collector_id": tk.StringVar(value=config.collector_id),
            "drive_remote": tk.StringVar(value=config.drive_remote),
            "drive_prefix": tk.StringVar(value=config.drive_prefix),
            "rclone_binary": tk.StringVar(value=config.rclone_binary),
            "r2_endpoint": tk.StringVar(value=config.r2_endpoint),
            "r2_bucket": tk.StringVar(value=config.r2_bucket),
            "r2_prefix": tk.StringVar(value=config.r2_prefix),
            "r2_region": tk.StringVar(value=config.r2_region),
            "local_root": tk.StringVar(value=config.local_root),
            "preferred_replica": tk.StringVar(value=config.preferred_replica),
            "require_both_replicas": tk.BooleanVar(
                value=config.require_both_replicas
            ),
            "history_days": tk.IntVar(value=config.history_days),
            "download_workers": tk.IntVar(value=config.download_workers),
            "access_key": tk.StringVar(),
            "secret_key": tk.StringVar(),
        }
        self.drive_status_var = tk.StringVar(value="连接：尚未测试")
        self.r2_status_var = tk.StringVar()
        self.r2_connection_text = "尚未测试"
        self.r2_credential_text = self._credential_status()
        self.test_events: queue.Queue[tuple[str, bool, str]] = queue.Queue()
        self.test_buttons: dict[str, ttk.Button] = {}
        self.tests_running: set[str] = set()
        self.status_var = tk.StringVar(
            value="填写后保存设置" if is_new else "设置已加载"
        )
        self._build()
        self._update_r2_status()
        self._poll_id = self.after(100, self._drain_test_events)

    def _section(self, parent: tk.Widget, title: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=(14, 10))
        frame.columnconfigure(1, weight=1)
        parent.columnconfigure(0, weight=4)
        parent.columnconfigure(1, weight=1)
        frame.grid(row=0, column=0, sticky="new", padx=(0, 24), pady=(0, 10))
        return frame

    @staticmethod
    def _field(
        frame: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.Variable,
        *,
        show: str | None = None,
    ) -> ttk.Entry:
        ttk.Label(frame, text=label, style="Form.TLabel").grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 12),
            pady=6,
        )
        entry = ttk.Entry(frame, textvariable=variable, show=show or "")
        entry.grid(row=row, column=1, sticky="ew", pady=6)
        return entry

    def _build(self) -> None:
        container = ttk.Frame(self, padding=(18, 14, 18, 16))
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)

        ttk.Label(
            container,
            text="连接与存储",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            container,
            text=(
                "新建采集服务器配置"
                if self.is_new
                else f"当前配置：{self.config.display_name}"
            ),
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))

        self.notebook = ttk.Notebook(container, style="Settings.TNotebook")
        self.notebook.grid(row=2, column=0, sticky="nsew")
        general_tab = ttk.Frame(self.notebook, padding=(16, 14))
        drive_tab = ttk.Frame(self.notebook, padding=(16, 14))
        r2_tab = ttk.Frame(self.notebook, padding=(16, 14))
        policy_tab = ttk.Frame(self.notebook, padding=(16, 14))
        self.notebook.add(general_tab, text="常规")
        self.notebook.add(drive_tab, text="Google Drive")
        self.notebook.add(r2_tab, text="Cloudflare R2")
        self.notebook.add(policy_tab, text="同步策略")

        status_band = ttk.Frame(container, style="Surface.TFrame", padding=(10, 8))
        status_band.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.status_label = ttk.Label(
            status_band,
            textvariable=self.status_var,
            style="MetricName.TLabel",
            anchor="w",
        )
        self.status_label.pack(fill="x")

        actions = ttk.Frame(container)
        actions.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        if not self.is_new and self.parent.profile_count > 1:
            ttk.Button(
                actions,
                text="× 删除此配置",
                style="Danger.TButton",
                command=self._delete_profile,
            ).pack(side="left")
        ttk.Button(actions, text="← 返回归档", command=self._close).pack(side="right")
        ttk.Button(
            actions, text="✓ 保存设置", style="Primary.TButton", command=self._save
        ).pack(side="right", padx=(0, 8))

        identity = self._section(general_tab, "归档流")
        profile_id_entry = self._field(
            identity, 0, "配置 ID", self.vars["profile_id"]
        )
        if not self.is_new:
            profile_id_entry.configure(state="readonly")
        self._field(identity, 1, "配置名称", self.vars["display_name"])
        self._field(identity, 2, "采集流 ID", self.vars["collector_id"])
        self._field(identity, 3, "本地目录", self.vars["local_root"])
        ttk.Button(identity, text="▣ 选择目录", command=self._choose_root).grid(
            row=3, column=2, padx=(8, 0)
        )
        ttk.Checkbutton(
            identity,
            text="参与同步全部与 Windows 自动任务",
            variable=self.vars["enabled"],
        ).grid(row=4, column=1, sticky="w", pady=5)

        drive = self._section(drive_tab, "Google Drive")
        self._field(drive, 0, "rclone Remote", self.vars["drive_remote"])
        self._field(drive, 1, "对象前缀", self.vars["drive_prefix"])
        self._field(drive, 2, "rclone 路径", self.vars["rclone_binary"])
        ttk.Button(drive, text="⚙ 配置 OAuth", command=self._configure_drive).grid(
            row=2, column=2, padx=(8, 0)
        )
        ttk.Label(
            drive,
            textvariable=self.drive_status_var,
            style="Form.TLabel",
        ).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(5, 0)
        )
        self.test_buttons["google_drive"] = ttk.Button(
            drive,
            text="↗ 测试连接",
            command=lambda: self._test_connection("google_drive"),
        )
        self.test_buttons["google_drive"].grid(
            row=3, column=2, padx=(8, 0), pady=(5, 0)
        )

        r2 = self._section(r2_tab, "Cloudflare R2")
        self._field(r2, 0, "S3 Endpoint", self.vars["r2_endpoint"])
        self._field(r2, 1, "Bucket", self.vars["r2_bucket"])
        self._field(r2, 2, "对象前缀", self.vars["r2_prefix"])
        self._field(r2, 3, "Region", self.vars["r2_region"])
        self._field(r2, 4, "Access Key ID", self.vars["access_key"])
        self._field(r2, 5, "Secret Access Key", self.vars["secret_key"], show="*")
        ttk.Label(r2, textvariable=self.r2_status_var, style="Form.TLabel").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(5, 0)
        )
        self.test_buttons["r2"] = ttk.Button(
            r2,
            text="↗ 测试连接",
            command=lambda: self._test_connection("r2"),
        )
        self.test_buttons["r2"].grid(
            row=6, column=2, padx=(8, 0), pady=(5, 0)
        )

        policy = self._section(policy_tab, "同步策略")
        ttk.Label(policy, text="首选下载副本", style="Form.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Combobox(
            policy,
            textvariable=self.vars["preferred_replica"],
            state="readonly",
            values=("google_drive", "r2"),
        ).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Checkbutton(
            policy,
            text="下载前要求两个云端 manifest 一致",
            variable=self.vars["require_both_replicas"],
        ).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(policy, text="显示历史天数", style="Form.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Spinbox(
            policy, from_=7, to=3650, textvariable=self.vars["history_days"]
        ).grid(row=2, column=1, sticky="w", pady=5)
        ttk.Label(policy, text="并发下载数", style="Form.TLabel").grid(
            row=3, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Spinbox(
            policy,
            from_=1,
            to=8,
            textvariable=self.vars["download_workers"],
        ).grid(row=3, column=1, sticky="w", pady=5)

    def set_status(self, message: str, severity: str = "info") -> None:
        color = {
            "success": GREEN,
            "warning": AMBER,
            "error": RED,
        }.get(severity, MUTED)
        self.status_var.set(message)
        self.status_label.configure(foreground=color)

    def _choose_root(self) -> None:
        selected = filedialog.askdirectory(
            parent=self, initialdir=str(self.vars["local_root"].get())
        )
        if selected:
            self.vars["local_root"].set(selected)

    def _configure_drive(self) -> None:
        if self.parent.busy:
            self.set_status("归档任务运行中，请等待任务结束后再配置 OAuth。", "warning")
            return
        binary = str(self.vars["rclone_binary"].get()).strip() or "rclone"
        resolved = resolve_rclone_binary(binary)
        if not resolved:
            self.set_status("rclone 不可用：未找到 rclone。", "error")
            return
        subprocess.Popen(
            [resolved, "config"],
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
        )
        self.drive_status_var.set("连接：OAuth 配置窗口已打开，完成后请测试")
        self.set_status("OAuth 配置窗口已打开，完成后返回此页测试连接")

    def _credential_status(self) -> str:
        if self.is_new or not self.config.profile_id:
            return "保存配置时写入凭据"
        try:
            access, secret = self.credentials.get_r2(self.config.profile_id)
        except Exception as exc:
            return f"凭据状态不可用（{exc}）"
        return "凭据已保存" if access and secret else "凭据未保存"

    def _update_r2_status(self) -> None:
        self.r2_status_var.set(
            f"连接：{self.r2_connection_text} · {self.r2_credential_text}"
        )

    def _form_config(self, *, for_save: bool) -> AppConfig:
        history_days = (
            int(self.vars["history_days"].get())
            if for_save
            else self.config.history_days
        )
        download_workers = (
            int(self.vars["download_workers"].get())
            if for_save
            else self.config.download_workers
        )
        return AppConfig(
            profile_id=str(self.vars["profile_id"].get()).strip(),
            display_name=str(self.vars["display_name"].get()).strip(),
            enabled=bool(self.vars["enabled"].get()),
            collector_id=str(self.vars["collector_id"].get()).strip(),
            drive_remote=str(self.vars["drive_remote"].get()).strip(),
            drive_prefix=str(self.vars["drive_prefix"].get()).strip(),
            rclone_binary=str(self.vars["rclone_binary"].get()).strip(),
            r2_endpoint=str(self.vars["r2_endpoint"].get()).strip(),
            r2_bucket=str(self.vars["r2_bucket"].get()).strip(),
            r2_prefix=str(self.vars["r2_prefix"].get()).strip(),
            r2_region=str(self.vars["r2_region"].get()).strip(),
            local_root=str(self.vars["local_root"].get()).strip(),
            preferred_replica=str(self.vars["preferred_replica"].get()).strip(),
            require_both_replicas=bool(self.vars["require_both_replicas"].get()),
            history_days=history_days,
            download_workers=download_workers,
        )

    def _test_connection(self, replica: str) -> None:
        if self.parent.busy:
            self.set_status("归档任务运行中，请等待任务结束后再测试连接。", "warning")
            return
        try:
            config = self._form_config(for_save=False)
            if replica == "google_drive":
                errors = [*config.validate_identity(), *config.validate_drive()]
                if errors:
                    raise ValueError("；".join(errors))
                build_remote: Callable[[], Any] = lambda: DriveRemote(config)
                self.drive_status_var.set("连接：正在测试")
            else:
                errors = [*config.validate_identity(), *config.validate_r2()]
                if errors:
                    raise ValueError("；".join(errors))
                access = str(self.vars["access_key"].get()).strip()
                secret = str(self.vars["secret_key"].get()).strip()
                if bool(access) != bool(secret):
                    raise ValueError("R2 Access Key 和 Secret 必须同时填写")
                if not access:
                    access, secret = self.credentials.get_r2(config.profile_id)
                build_remote = lambda: R2Remote(config, access, secret)
                self.r2_connection_text = "正在测试"
                self._update_r2_status()
        except Exception as exc:
            self.set_status(f"无法测试连接：{exc}", "error")
            return

        button = self.test_buttons[replica]
        button.configure(state="disabled")
        self.tests_running.add(replica)
        self.set_status(f"正在测试 {replica_label(replica)} 连接")

        def run() -> None:
            try:
                dates = build_remote().list_dates()
                message = f"连接成功，可见 {len(dates)} 个归档日期"
            except Exception as exc:
                self.test_events.put((replica, False, str(exc)))
            else:
                self.test_events.put((replica, True, message))

        threading.Thread(target=run, daemon=True).start()

    def _drain_test_events(self) -> None:
        try:
            while True:
                replica, success, message = self.test_events.get_nowait()
                prefix = "成功" if success else "失败"
                if replica == "google_drive":
                    self.drive_status_var.set(f"连接{prefix}：{message}")
                else:
                    self.r2_connection_text = f"{prefix}：{message}"
                    self._update_r2_status()
                self.tests_running.discard(replica)
                self.test_buttons[replica].configure(state="normal")
                self.set_status(
                    f"{replica_label(replica)}：{message}",
                    "success" if success else "error",
                )
        except queue.Empty:
            pass
        if not self._disposed:
            self._poll_id = self.after(100, self._drain_test_events)

    def dispose(self) -> None:
        self._disposed = True
        try:
            self.after_cancel(self._poll_id)
        except (AttributeError, tk.TclError):
            pass

    def _close(self) -> None:
        if self.tests_running:
            self.set_status(
                "连接测试尚未完成，请等待测试结束后再返回归档页。",
                "warning",
            )
            return
        self.parent.show_archive_tab()

    def _save(self) -> None:
        if self.parent.busy:
            self.set_status("归档任务运行中，请等待任务结束后再保存设置。", "warning")
            return
        if self.tests_running:
            self.set_status(
                "连接测试尚未完成，请等待测试结束后再保存设置。",
                "warning",
            )
            return
        try:
            config = self._form_config(for_save=True)
            errors = config.validate()
            if errors:
                raise ValueError("；".join(errors))
            access = str(self.vars["access_key"].get()).strip()
            secret = str(self.vars["secret_key"].get()).strip()
            if bool(access) != bool(secret):
                raise ValueError("R2 Access Key 和 Secret 必须同时填写")
            self.config_store.save(config)
            if access:
                self.credentials.set_r2(config.profile_id, access, secret)
        except Exception as exc:
            self.set_status(f"无法保存：{exc}", "error")
            return
        self.parent.reload_profiles(
            config.profile_id,
            settings_status=f"{config.display_name} 设置已保存",
            select_settings=True,
        )

    def _delete_profile(self) -> None:
        if self.parent.busy:
            self.set_status("归档任务运行中，请等待任务结束后再删除配置。", "warning")
            return
        if self.tests_running:
            self.set_status(
                "连接测试尚未完成，请等待测试结束后再删除配置。",
                "warning",
            )
            return
        if not messagebox.askyesno(
            "删除采集服务器配置",
            f"确认删除“{self.config.display_name}”及其本机 R2 凭据？\n"
            "云端对象和本地归档不会删除。",
            parent=self,
        ):
            return
        try:
            self.parent.delete_profile(self.config.profile_id)
        except Exception as exc:
            self.set_status(f"无法删除配置：{exc}", "error")
            return


class ArchiveClientApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.withdraw()
        self.title("SMSI 归档客户端")
        self.geometry("1080x700")
        self.minsize(920, 600)
        self.configure(bg=BG)
        self.config_store = ConfigStore()
        self.credentials = CredentialStore()
        self.profiles, self.active_profile_id = self.config_store.load_profiles()
        self.config_value = next(
            profile
            for profile in self.profiles
            if profile.profile_id == self.active_profile_id
        )
        self.manager = ArchiveManager(self.config_value, self.credentials)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.rows: dict[str, ArchiveDayStatus] = {}
        self.usage: dict[str, dict[str, Any]] = {}
        self._usage_refreshing: set[str] = set()
        self._scan_progress_keys: set[tuple[str, ...]] = set()
        self._scan_progress_total = 0
        self._operation_kind: str | None = None
        self._active_cancel: threading.Event | None = None
        self._active_thread: threading.Thread | None = None
        self._closing = False
        self._speed_sample_at: float | None = None
        self._speed_sample_bytes = 0
        self._transfer_speed: float | None = None
        self._scheduled_run_started_at: float | None = None
        self.schedule_installed = task_installed()
        self._startup_sync_pending = self.schedule_installed
        self._configure_styles()
        self._load_app_icon()
        self._build()
        self._center_window()
        self.deiconify()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)
        self.after(250, self.refresh)

    def report_callback_exception(self, exc_type, exc_value, traceback) -> None:
        LOGGER.error(
            "Unhandled Tk callback error",
            exc_info=(exc_type, exc_value, traceback),
        )
        if not self._closing:
            self._show_result("界面操作失败", str(exc_value), "error")
            self.show_archive_tab()

    @property
    def profile_count(self) -> int:
        return len(self.profiles)

    def _center_window(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = max((self.winfo_screenwidth() - width) // 2, 0)
        y = max((self.winfo_screenheight() - height) // 2, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _load_app_icon(self) -> None:
        self._app_icon: tk.PhotoImage | None = None
        png_path = resource_path("assets/smsi_archive_64.png")
        ico_path = resource_path("assets/smsi_archive.ico")
        try:
            if png_path.is_file():
                self._app_icon = tk.PhotoImage(file=str(png_path))
                self.iconphoto(True, self._app_icon)
            if ico_path.is_file():
                self.iconbitmap(default=str(ico_path))
        except tk.TclError:
            LOGGER.exception("Failed to load application icon")

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure(
            "TLabel",
            background=BG,
            foreground=INK,
            font=("Segoe UI", 9),
        )
        style.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Form.TLabel", background=SURFACE, foreground=INK, font=("Segoe UI", 9))
        style.configure("SectionTitle.TLabel", foreground=INK, font=("Segoe UI Semibold", 11))
        style.configure(
            "Metric.TLabel",
            background=SURFACE,
            foreground=INK,
            font=("Segoe UI", 9),
        )
        style.configure(
            "MetricNote.TLabel",
            background=SURFACE,
            foreground=MUTED,
            font=("Segoe UI", 8),
        )
        style.configure("MetricName.TLabel", background=SURFACE, foreground=MUTED, font=("Segoe UI", 8))
        style.configure(
            "DashboardTitle.TLabel",
            background=SURFACE,
            foreground=MUTED,
            font=("Segoe UI", 8),
        )
        style.configure(
            "DashboardValue.TLabel",
            background=SURFACE,
            foreground=INK,
            font=("Segoe UI", 9),
        )
        style.configure(
            "DashboardMeta.TLabel",
            background=SURFACE,
            foreground=MUTED,
            font=("Segoe UI", 8),
        )
        style.configure(
            "DashboardStatus.TLabel",
            background=SURFACE,
            foreground=ACCENT,
            font=("Segoe UI", 8),
        )
        style.configure("Status.TLabel", font=("Segoe UI", 9), foreground=INK)
        style.configure(
            "Percent.TLabel",
            background=SURFACE,
            font=("Segoe UI Semibold", 9),
            foreground=ACCENT,
        )
        style.configure(
            "ResultTitle.TLabel",
            background=SURFACE,
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "ResultDetail.TLabel",
            background=SURFACE,
            foreground=MUTED,
            font=("Segoe UI", 9),
        )
        style.configure(
            "TButton",
            font=("Segoe UI", 9),
            padding=(11, 6),
            background="#edf1f4",
            foreground=INK,
            bordercolor=BORDER,
            focusthickness=1,
            focuscolor=ACCENT,
        )
        style.map(
            "TButton",
            background=[("active", "#e3e9ed"), ("pressed", "#dbe3e8")],
            foreground=[("disabled", "#9aa6b2")],
        )
        style.configure("Dashboard.TButton", font=("Segoe UI", 8), padding=(6, 2))
        style.configure("TabHeader.TButton", font=("Segoe UI", 8), padding=(9, 4))
        style.configure(
            "Primary.TButton",
            font=("Segoe UI Semibold", 9),
            padding=(13, 6),
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
        )
        style.map(
            "Primary.TButton",
            background=[
                ("active", ACCENT_DARK),
                ("pressed", ACCENT_DARK),
                ("disabled", "#9eb6c0"),
            ],
            foreground=[("disabled", "#eef3f5")],
        )
        style.configure(
            "Danger.TButton",
            foreground=RED,
            background=PALE_RED,
            bordercolor="#efc6c0",
        )
        style.map("Danger.TButton", background=[("active", "#fde3df")])
        style.configure(
            "TNotebook",
            background=BG,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background=BG,
            foreground=MUTED,
            font=("Segoe UI Semibold", 9),
            padding=(14, 6),
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", SURFACE), ("active", "#e9eef1")],
            foreground=[("selected", ACCENT), ("active", INK)],
            padding=[("selected", (14, 6))],
            expand=[("selected", (0, 0, 0, 0))],
        )
        style.configure("Settings.TNotebook", background=SURFACE, borderwidth=0)
        style.configure(
            "Settings.TNotebook.Tab",
            background=SURFACE,
            foreground=MUTED,
            font=("Segoe UI Semibold", 9),
            padding=(12, 7),
            borderwidth=0,
        )
        style.map(
            "Settings.TNotebook.Tab",
            background=[("selected", PALE_BLUE), ("active", "#f0f4f6")],
            foreground=[("selected", ACCENT)],
            padding=[("selected", (12, 7))],
            expand=[("selected", (0, 0, 0, 0))],
        )
        style.configure(
            "TLabelframe",
            background=SURFACE,
            bordercolor=BORDER,
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background=SURFACE,
            foreground=INK,
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "TEntry",
            padding=(7, 5),
            fieldbackground=SURFACE,
            foreground=INK,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
        )
        style.configure(
            "TCombobox",
            padding=(7, 5),
            fieldbackground=SURFACE,
            foreground=INK,
            bordercolor=BORDER,
            arrowsize=14,
        )
        style.configure(
            "TabHeader.TCombobox",
            padding=(7, 4),
            fieldbackground="#f8fafb",
            background="#f8fafb",
            foreground=INK,
            bordercolor="#cbd4dc",
            lightcolor="#cbd4dc",
            darkcolor="#cbd4dc",
            arrowsize=12,
        )
        style.map(
            "TabHeader.TCombobox",
            fieldbackground=[("readonly", "#f8fafb")],
            background=[("readonly", "#f8fafb"), ("active", "#eef3f6")],
            foreground=[("readonly", INK)],
            selectbackground=[("readonly", "#f8fafb")],
            selectforeground=[("readonly", INK)],
        )
        style.configure(
            "TSpinbox",
            padding=(7, 5),
            fieldbackground=SURFACE,
            foreground=INK,
            bordercolor=BORDER,
            arrowsize=14,
        )
        style.configure(
            "TCheckbutton",
            background=SURFACE,
            foreground=INK,
            font=("Segoe UI", 9),
        )
        style.layout("Sync.Toolbutton", style.layout("Toolbutton"))
        style.configure(
            "Sync.Toolbutton",
            background=SURFACE,
            foreground=INK,
            font=("Segoe UI", 8),
            padding=(6, 2),
            bordercolor=BORDER,
            relief="flat",
        )
        style.map(
            "Sync.Toolbutton",
            background=[
                ("selected", PALE_GREEN),
                ("active", "#e9eef1"),
                ("pressed", "#dfe7ea"),
            ],
            foreground=[("selected", GREEN), ("disabled", "#9aa6b2")],
            bordercolor=[("selected", "#bcdcc9"), ("active", BORDER)],
        )
        style.configure(
            "Treeview",
            rowheight=29,
            font=("Segoe UI", 9),
            background=SURFACE,
            fieldbackground=SURFACE,
            foreground=INK,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
        )
        style.configure(
            "Treeview.Item",
            padding=(0, 0, 0, 1),
            relief="solid",
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 9),
            background="#e9eef2",
            foreground=INK,
            padding=(8, 7),
            bordercolor=BORDER,
            relief="flat",
        )
        style.map("Treeview.Heading", background=[("active", "#dde5ea")])
        for name, color in (
            ("Drive", "#4285f4"),
            ("R2", "#f48120"),
            ("Local", GREEN),
        ):
            style.configure(
                f"{name}.Horizontal.TProgressbar",
                background=color,
                troughcolor="#e2e7ea",
                bordercolor="#e2e7ea",
                lightcolor=color,
                darkcolor=color,
                thickness=2,
            )
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        tab_shell = ttk.Frame(self)
        tab_shell.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=8,
            pady=(4, 6),
        )
        tab_shell.columnconfigure(0, weight=1)
        tab_shell.rowconfigure(0, weight=1)

        self.main_notebook = ttk.Notebook(tab_shell)
        self.main_notebook.grid(row=0, column=0, sticky="nsew")
        self.archive_tab = ttk.Frame(self.main_notebook)
        self.report_tab = ttk.Frame(self.main_notebook)
        self.settings_tab = ttk.Frame(self.main_notebook)
        self.main_notebook.add(self.archive_tab, text="归档")
        self.main_notebook.add(self.report_tab, text="报告")
        self.main_notebook.add(self.settings_tab, text="设置")
        self.archive_tab.columnconfigure(0, weight=1)
        self.archive_tab.rowconfigure(3, weight=1)

        tools = ttk.Frame(tab_shell)
        tools.place(relx=1.0, x=0, y=1, anchor="ne")
        self.profile_var = tk.StringVar()
        self.profile_choice_to_id: dict[str, str] = {}
        self.profile_id_to_choice: dict[str, str] = {}
        self.profile_combo = ttk.Combobox(
            tools,
            textvariable=self.profile_var,
            state="readonly",
            style="TabHeader.TCombobox",
            width=20,
        )
        self.profile_combo.grid(row=0, column=0, padx=(0, 6))
        self.profile_combo.bind("<<ComboboxSelected>>", self._select_profile)
        self.add_profile_button = ttk.Button(
            tools,
            text="+ 新增配置",
            command=self.new_profile,
            style="TabHeader.TButton",
        )
        self.add_profile_button.grid(row=0, column=1, padx=(0, 6))
        self.refresh_button = ttk.Button(
            tools,
            text="↻ 刷新",
            command=self._refresh_current_tab,
            style="TabHeader.TButton",
        )
        self.refresh_button.grid(row=0, column=2)
        self._sync_profile_selector()

        metrics = tk.Frame(
            self.archive_tab,
            bg=SURFACE,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=16,
            pady=8,
        )
        metrics.grid(row=0, column=0, sticky="ew", pady=(12, 0))
        self.metric_vars = {
            "drive": tk.StringVar(value="--"),
            "drive_detail": tk.StringVar(value="正在读取账号容量"),
            "r2": tk.StringVar(value="--"),
            "r2_detail": tk.StringVar(value="正在读取 Bucket 用量"),
            "local": tk.StringVar(value="--"),
            "local_detail": tk.StringVar(value="正在读取磁盘容量"),
            "schedule": tk.StringVar(value=self._schedule_summary()),
            "schedule_meta": tk.StringVar(value=self._schedule_meta()),
        }
        self.schedule_detail_var = tk.StringVar()
        self.schedule_toggle_var = tk.BooleanVar(value=self.schedule_installed)
        self.capacity_bars: dict[str, ttk.Progressbar] = {}
        for index, (name, label) in enumerate(
            (
                ("drive", "Google Drive"),
                ("r2", "Cloudflare R2"),
                ("local", "本地归档磁盘"),
                ("schedule", "自动同步"),
            )
        ):
            cell = ttk.Frame(metrics, style="Surface.TFrame")
            cell.grid(
                row=0,
                column=index,
                sticky="nsew",
                padx=14,
            )
            cell.columnconfigure(0, weight=1)
            cell_header = ttk.Frame(
                cell,
                style="Surface.TFrame",
                height=24,
            )
            cell_header.grid(row=0, column=0, sticky="ew")
            cell_header.pack_propagate(False)
            ttk.Label(
                cell_header, text=label, style="DashboardTitle.TLabel"
            ).pack(side="left", anchor="w")
            if name == "schedule":
                self.schedule_button = ttk.Checkbutton(
                    cell_header,
                    text="↻ 自动同步",
                    variable=self.schedule_toggle_var,
                    command=self.toggle_schedule,
                    style="Sync.Toolbutton",
                )
                self.schedule_button.pack(side="right")
            elif name == "local":
                self.local_button = ttk.Button(
                    cell_header,
                    text="▣ 打开目录",
                    command=self.open_local_root,
                    style="Dashboard.TButton",
                )
                self.local_button.pack(side="right")
            ttk.Label(
                cell,
                textvariable=self.metric_vars[name],
                style="DashboardValue.TLabel",
            ).grid(row=1, column=0, sticky="w", pady=(4, 0))
            if name == "schedule":
                ttk.Label(
                    cell,
                    textvariable=self.metric_vars["schedule_meta"],
                    style="DashboardMeta.TLabel",
                ).grid(row=2, column=0, sticky="w", pady=(1, 0))
                ttk.Label(
                    cell,
                    textvariable=self.schedule_detail_var,
                    style="DashboardStatus.TLabel",
                ).grid(row=3, column=0, sticky="w", pady=(4, 0))
            else:
                ttk.Label(
                    cell,
                    textvariable=self.metric_vars[f"{name}_detail"],
                    style="DashboardMeta.TLabel",
                ).grid(row=2, column=0, sticky="w", pady=(1, 0))
                bar_style = {
                    "drive": "Drive.Horizontal.TProgressbar",
                    "r2": "R2.Horizontal.TProgressbar",
                    "local": "Local.Horizontal.TProgressbar",
                }[name]
                bar_slot = ttk.Frame(
                    cell,
                    style="Surface.TFrame",
                    height=9,
                )
                self.capacity_bars[name] = ttk.Progressbar(
                    bar_slot,
                    style=bar_style,
                    mode="determinate",
                    maximum=100,
                )
                bar_slot.grid(
                    row=3, column=0, sticky="ew", pady=(5, 0)
                )
                bar_slot.pack_propagate(False)
                self.capacity_bars[name].pack(fill="both", expand=True)
            metrics.columnconfigure(index, weight=1, uniform="metrics")

        action_bar = ttk.Frame(self.archive_tab, padding=(0, 10, 0, 10))
        action_bar.grid(row=1, column=0, sticky="ew")
        self.download_button = ttk.Button(
            action_bar,
            text="↓ 下载选中",
            command=self.download_selected,
            state="disabled" if self.schedule_installed else "normal",
        )
        self.download_button.grid(row=0, column=0)
        self.verify_button = ttk.Button(
            action_bar, text="✓ 重新校验", command=self.verify_selected
        )
        self.verify_button.grid(row=0, column=1, padx=(8, 0))
        self.cancel_button = ttk.Button(
            action_bar,
            text="× 取消当前任务",
            command=self.cancel_current_operation,
            state="disabled",
        )
        self.cancel_button.grid(row=0, column=2, padx=(8, 0))

        self.result_frame = tk.Frame(
            self.archive_tab,
            bg=PALE_BLUE,
            highlightbackground="#c9dfe7",
            highlightthickness=1,
            padx=14,
            pady=7,
        )
        self.result_frame.columnconfigure(1, weight=1)
        self.result_title_var = tk.StringVar()
        self.result_detail_var = tk.StringVar()
        self.result_title_label = tk.Label(
            self.result_frame,
            textvariable=self.result_title_var,
            bg=PALE_BLUE,
            fg=ACCENT,
            font=("Segoe UI Semibold", 9),
        )
        self.result_title_label.grid(row=0, column=0, sticky="nw")
        self.result_detail_label = tk.Label(
            self.result_frame,
            textvariable=self.result_detail_var,
            bg=PALE_BLUE,
            fg=MUTED,
            font=("Segoe UI", 9),
            wraplength=820,
            justify="left",
            anchor="w",
        )
        self.result_detail_label.grid(
            row=0,
            column=1,
            sticky="new",
            padx=(8, 0),
        )
        self.result_frame.bind(
            "<Configure>",
            lambda event: self.result_detail_label.configure(
                wraplength=max(
                    event.width - self.result_title_label.winfo_reqwidth() - 50,
                    320,
                )
            ),
        )

        self.table_frame = ttk.Frame(self.archive_tab, style="Surface.TFrame")
        self.table_frame.grid(row=3, column=0, sticky="nsew")
        self.table_frame.columnconfigure(0, weight=1)
        self.table_frame.rowconfigure(0, weight=1)
        columns = ("date", "drive", "r2", "match", "local", "detail")
        self.table = ttk.Treeview(
            self.table_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
            height=5,
        )
        headings = {
            "date": "UTC 日期",
            "drive": "Google Drive",
            "r2": "Cloudflare R2",
            "match": "双副本",
            "local": "本地",
            "detail": "归档规模",
        }
        widths = {
            "date": 116,
            "drive": 132,
            "r2": 132,
            "match": 100,
            "local": 118,
            "detail": 320,
        }
        for column in columns:
            self.table.heading(column, text=headings[column], anchor="w")
            self.table.column(
                column,
                width=widths[column],
                minwidth=85 if column != "detail" else 180,
                anchor="w",
                stretch=column == "detail",
            )
        self.table.tag_configure("stripe_even", background=SURFACE)
        self.table.tag_configure("stripe_odd", background="#f5f7f9")
        self.table.tag_configure("ok", foreground=INK)
        self.table.tag_configure("loading", foreground=BLUE, background=PALE_BLUE)
        self.table.tag_configure("warn", foreground=AMBER, background=PALE_AMBER)
        self.table.tag_configure("error", foreground=RED, background=PALE_RED)
        scrollbar = ttk.Scrollbar(
            self.table_frame, orient="vertical", command=self.table.yview
        )
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind("<Double-1>", self._on_table_double_click)
        self.table.bind("<<TreeviewSelect>>", self._on_table_selection)
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        status_bar = ttk.Frame(self.archive_tab, style="Surface.TFrame")
        status_bar.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        status_bar.columnconfigure(0, weight=1)
        status_line = ttk.Frame(
            status_bar,
            style="Surface.TFrame",
            padding=(12, 6, 12, 5),
        )
        status_line.grid(row=0, column=0, sticky="ew")
        status_line.columnconfigure(0, weight=1)
        status_text = ttk.Frame(status_line, style="Surface.TFrame")
        status_text.grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="正在连接")
        ttk.Label(status_text, textvariable=self.status_var, style="Status.TLabel").pack(
            side="left"
        )
        self.progress_detail_var = tk.StringVar()
        ttk.Label(
            status_text,
            textvariable=self.progress_detail_var,
            style="MetricName.TLabel",
            anchor="w",
        ).pack(side="left", padx=(10, 0))
        self.progress_percent_var = tk.StringVar()
        ttk.Label(
            status_line,
            textvariable=self.progress_percent_var,
            style="Percent.TLabel",
            anchor="e",
        ).grid(row=0, column=1, sticky="e", padx=(12, 0))
        progress_slot = ttk.Frame(
            status_bar,
            style="Surface.TFrame",
            height=7,
        )
        progress_slot.grid(row=1, column=0, sticky="ew")
        progress_slot.pack_propagate(False)
        self.progress = ThinProgressbar(progress_slot)
        self.progress.pack(fill="both", expand=True)

        self._build_report_tab()
        self.settings_panel: SettingsPanel | None = None
        self._mount_settings_panel(self.config_value)
        self.main_notebook.bind("<<NotebookTabChanged>>", self._on_main_tab_changed)

    def _build_report_tab(self) -> None:
        self.report_tab.columnconfigure(0, weight=1)
        self.report_tab.rowconfigure(3, weight=1)
        self.report_records: dict[str, dict[str, Any]] = {}

        toolbar = ttk.Frame(self.report_tab, padding=(8, 10, 8, 8))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(0, weight=1)
        self.report_status_var = tk.StringVar(value="尚未读取报告")
        ttk.Label(
            toolbar,
            textvariable=self.report_status_var,
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.open_report_file_button = ttk.Button(
            toolbar,
            text="↗ 打开文件",
            command=self.open_selected_report_file,
            state="disabled",
        )
        self.open_report_file_button.grid(row=0, column=1, padx=(8, 0))
        self.open_report_directory_button = ttk.Button(
            toolbar,
            text="▣ 打开目录",
            command=self.open_report_directory,
        )
        self.open_report_directory_button.grid(row=0, column=2, padx=(8, 0))

        report_list_frame = ttk.Frame(self.report_tab, style="Surface.TFrame")
        report_list_frame.grid(row=1, column=0, sticky="ew")
        report_list_frame.columnconfigure(0, weight=1)
        self.report_table = ttk.Treeview(
            report_list_frame,
            columns=("date", "type", "status", "objects", "rows", "verified_at"),
            show="headings",
            selectmode="browse",
            height=5,
        )
        report_headings = {
            "date": "UTC 日期",
            "type": "报告类型",
            "status": "状态",
            "objects": "对象数",
            "rows": "总行数",
            "verified_at": "生成时间",
        }
        report_widths = {
            "date": 120,
            "type": 120,
            "status": 110,
            "objects": 90,
            "rows": 130,
            "verified_at": 220,
        }
        for column in report_headings:
            self.report_table.heading(
                column,
                text=report_headings[column],
                anchor="w",
            )
            self.report_table.column(
                column,
                width=report_widths[column],
                minwidth=80,
                anchor="w",
                stretch=column == "verified_at",
            )
        self.report_table.tag_configure("ok", foreground=INK)
        self.report_table.tag_configure("warn", foreground=AMBER)
        self.report_table.tag_configure("error", foreground=RED)
        report_scrollbar = ttk.Scrollbar(
            report_list_frame,
            orient="vertical",
            command=self.report_table.yview,
        )
        self.report_table.configure(yscrollcommand=report_scrollbar.set)
        self.report_table.grid(row=0, column=0, sticky="ew")
        report_scrollbar.grid(row=0, column=1, sticky="ns")
        self.report_table.bind("<<TreeviewSelect>>", self._on_report_selection)
        self.report_table.bind(
            "<Double-1>",
            lambda _event: self.open_selected_report_file(),
        )

        summary = ttk.Frame(
            self.report_tab,
            style="Surface.TFrame",
            padding=(12, 8),
        )
        summary.grid(row=2, column=0, sticky="ew", pady=(6, 6))
        summary.columnconfigure(0, weight=1)
        self.report_summary_var = tk.StringVar(value="请选择一份报告")
        ttk.Label(
            summary,
            textvariable=self.report_summary_var,
            style="Status.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.report_meta_var = tk.StringVar()
        ttk.Label(
            summary,
            textvariable=self.report_meta_var,
            style="MetricName.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        detail_frame = ttk.Frame(self.report_tab, style="Surface.TFrame")
        detail_frame.grid(row=3, column=0, sticky="nsew")
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(0, weight=1)
        detail_columns = ("kind", "name", "path", "rows", "size", "digest")
        self.report_detail_table = ttk.Treeview(
            detail_frame,
            columns=detail_columns,
            show="headings",
            selectmode="browse",
        )
        detail_scrollbar = ttk.Scrollbar(
            detail_frame,
            orient="vertical",
            command=self.report_detail_table.yview,
        )
        self.report_detail_table.configure(yscrollcommand=detail_scrollbar.set)
        self.report_detail_table.grid(row=0, column=0, sticky="nsew")
        detail_scrollbar.grid(row=0, column=1, sticky="ns")
        self.report_detail_table.tag_configure("stripe_even", background=SURFACE)
        self.report_detail_table.tag_configure("stripe_odd", background="#f5f7f9")
        self._configure_report_detail_columns(objects=True)

        self.report_path_var = tk.StringVar()
        ttk.Label(
            self.report_tab,
            textvariable=self.report_path_var,
            style="Muted.TLabel",
            anchor="w",
        ).grid(row=4, column=0, sticky="ew", padx=10, pady=(5, 6))

        self.refresh_reports()

    def _refresh_current_tab(self) -> None:
        if self.main_notebook.select() == str(self.report_tab):
            self.refresh_reports()
            return
        self.refresh(force=True)

    def _on_main_tab_changed(self, _event: tk.Event | None = None) -> None:
        if self.main_notebook.select() == str(self.report_tab):
            self.refresh_reports()

    def _report_root(self) -> Path:
        return (
            self.config_value.archive_root
            / "reports"
            / f"collector={self.config_value.collector_id}"
        )

    @staticmethod
    def _report_type(path: Path) -> str:
        name = path.name
        if name.startswith("verify-"):
            return "完整校验"
        if name.startswith("plan-"):
            return "清理计划"
        if name.startswith("checkpoint-r2-"):
            return "R2 检查点"
        if name.startswith("receipt-"):
            return "清理回执"
        return "JSON 报告"

    @staticmethod
    def _report_timestamp(value: Any, fallback: float) -> str:
        text = str(value or "").strip()
        match = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", text)
        if match:
            return f"{match.group(1)} {match.group(2)} UTC"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(fallback)) + " UTC"

    @staticmethod
    def _report_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _report_state(report_type: str, payload: dict[str, Any]) -> tuple[str, str]:
        status = str(payload.get("status") or "").strip().lower()
        if status == "verified":
            return "✓ 已验证", "ok"
        if report_type == "清理回执":
            return "✓ 已完成", "ok"
        if report_type == "R2 检查点":
            return "✓ 已确认", "ok"
        if report_type == "清理计划":
            return "○ 待处理", "warn"
        return (status or "已读取"), "ok"

    def refresh_reports(self) -> None:
        selected_path = ""
        selected = self.report_table.selection()
        if selected:
            selected_path = str(
                self.report_records.get(selected[0], {}).get("path") or ""
            )

        self.report_table.delete(*self.report_table.get_children())
        self.report_records.clear()
        self.report_detail_table.delete(*self.report_detail_table.get_children())
        self.open_report_file_button.configure(state="disabled")
        self.report_summary_var.set("请选择一份报告")
        self.report_meta_var.set("")
        self.report_path_var.set("")

        root = self._report_root()
        try:
            paths = sorted(
                root.rglob("*.json") if root.is_dir() else (),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError as exc:
            self.report_status_var.set(f"报告目录读取失败：{exc}")
            return

        selected_iid = ""
        invalid_count = 0
        for index, path in enumerate(paths):
            payload: dict[str, Any] = {}
            error = ""
            try:
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise RuntimeError("报告文件超过 8 MB")
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(parsed, dict):
                    raise RuntimeError("报告根节点不是 JSON 对象")
                payload = parsed
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
                error = str(exc)
                invalid_count += 1

            report_type = self._report_type(path)
            match = re.search(r"\d{4}-\d{2}-\d{2}", path.name)
            archive_date = str(payload.get("archive_date") or "")
            if not archive_date and match:
                archive_date = match.group(0)
            state, tag = (
                ("! 异常", "error")
                if error
                else self._report_state(report_type, payload)
            )
            objects = payload.get("objects")
            object_count = self._report_int(
                payload.get("object_count"),
                len(objects) if isinstance(objects, list) else 0,
            )
            row_count = self._report_int(payload.get("row_count"))
            generated_at = next(
                (
                    payload.get(key)
                    for key in (
                        "verified_at",
                        "completed_at",
                        "generated_at",
                        "created_at",
                    )
                    if payload.get(key)
                ),
                None,
            )
            iid = f"report-{index}"
            record = {
                "path": path,
                "payload": payload,
                "error": error,
                "type": report_type,
                "date": archive_date or "--",
                "state": state,
                "tag": tag,
            }
            self.report_records[iid] = record
            self.report_table.insert(
                "",
                "end",
                iid=iid,
                values=(
                    record["date"],
                    report_type,
                    state,
                    f"{object_count:,}" if object_count else "--",
                    f"{row_count:,}" if row_count else "--",
                    self._report_timestamp(generated_at, path.stat().st_mtime),
                ),
                tags=(tag,),
            )
            if str(path) == selected_path:
                selected_iid = iid

        valid_count = len(paths) - invalid_count
        self.report_status_var.set(
            f"{len(paths)} 份报告 · {valid_count} 可读取"
            + (f" · {invalid_count} 异常" if invalid_count else "")
        )
        if not paths:
            self.report_summary_var.set("当前配置没有本地报告")
            self.report_meta_var.set(str(root))
            return
        target = selected_iid or self.report_table.get_children()[0]
        self.report_table.selection_set(target)
        self.report_table.focus(target)
        self.report_table.see(target)
        self._on_report_selection()

    def _configure_report_detail_columns(self, *, objects: bool) -> None:
        if objects:
            headings = {
                "kind": "类型",
                "name": "表名",
                "path": "归档对象",
                "rows": "行数",
                "size": "大小",
                "digest": "SHA-256",
            }
            widths = {
                "kind": 80,
                "name": 160,
                "path": 360,
                "rows": 100,
                "size": 100,
                "digest": 150,
            }
            self.report_detail_table.configure(displaycolumns=tuple(headings))
            for column, heading in headings.items():
                self.report_detail_table.heading(column, text=heading, anchor="w")
                self.report_detail_table.column(
                    column,
                    width=widths[column],
                    minwidth=70,
                    anchor="w",
                    stretch=column == "path",
                )
            return

        self.report_detail_table.configure(displaycolumns=("name", "path"))
        self.report_detail_table.heading("name", text="字段", anchor="w")
        self.report_detail_table.heading("path", text="值", anchor="w")
        self.report_detail_table.column(
            "name", width=220, minwidth=140, anchor="w", stretch=False
        )
        self.report_detail_table.column(
            "path", width=700, minwidth=300, anchor="w", stretch=True
        )

    def _on_report_selection(self, _event: tk.Event | None = None) -> None:
        selected = self.report_table.selection()
        if not selected:
            return
        record = self.report_records.get(selected[0])
        if not record:
            return
        path = Path(record["path"])
        payload = record["payload"]
        error = str(record["error"] or "")
        self.open_report_file_button.configure(
            state="normal" if path.is_file() else "disabled"
        )
        self.report_path_var.set(str(path))
        self.report_detail_table.delete(*self.report_detail_table.get_children())
        if error:
            self.report_summary_var.set(f"{record['date']} · 报告读取异常")
            self.report_meta_var.set(error)
            self._configure_report_detail_columns(objects=False)
            return

        objects = payload.get("objects")
        object_rows = (
            [item for item in objects if isinstance(item, dict)]
            if isinstance(objects, list)
            else []
        )
        if object_rows:
            self._configure_report_detail_columns(objects=True)
            total_bytes = sum(
                self._report_int(item.get("size_bytes")) for item in object_rows
            )
            source = str(payload.get("download_replica") or "")
            source_text = replica_label(source) if source else "本地校验"
            match = payload.get("replicas_match")
            match_text = (
                "双副本一致"
                if match is True
                else "双副本不一致"
                if match is False
                else "云端已清理"
            )
            warnings = payload.get("warnings")
            warning_count = len(warnings) if isinstance(warnings, list) else 0
            self.report_summary_var.set(
                f"{record['date']} · {record['state']} · "
                f"{len(object_rows):,} 个对象 · "
                f"{self._report_int(payload.get('row_count')):,} 行"
            )
            meta = (
                f"{human_bytes(total_bytes)} · 来源 {source_text} · {match_text}"
            )
            if warning_count:
                meta += f" · {warning_count} 条警告"
            self.report_meta_var.set(meta)
            kind_labels = {
                "business": "业务",
                "control": "控制",
                "evidence": "证据",
                "raw": "原始",
            }
            for index, item in enumerate(object_rows):
                if not isinstance(item, dict):
                    continue
                digest = str(item.get("sha256") or "")
                self.report_detail_table.insert(
                    "",
                    "end",
                    values=(
                        kind_labels.get(str(item.get("kind") or ""), item.get("kind") or "--"),
                        item.get("table_name") or "--",
                        item.get("relative_key") or "--",
                        f"{self._report_int(item.get('row_count')):,}",
                        human_bytes(self._report_int(item.get("size_bytes"))),
                        digest[:16] + "…" if len(digest) > 16 else digest,
                    ),
                    tags=("stripe_odd" if index % 2 else "stripe_even",),
                )
            return

        self._configure_report_detail_columns(objects=False)
        self.report_summary_var.set(
            f"{record['date']} · {record['type']} · {record['state']}"
        )
        self.report_meta_var.set("结构化报告字段")
        for index, (name, value) in enumerate(sorted(payload.items())):
            display = (
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if isinstance(value, (dict, list))
                else str(value)
            )
            self.report_detail_table.insert(
                "",
                "end",
                values=("", name, compact_name(display, 180), "", "", ""),
                tags=("stripe_odd" if index % 2 else "stripe_even",),
            )

    def open_report_directory(self) -> None:
        try:
            root = self._report_root()
            root.mkdir(parents=True, exist_ok=True)
            os.startfile(root)
        except Exception as exc:
            self.report_summary_var.set("无法打开报告目录")
            self.report_meta_var.set(str(exc))

    def open_selected_report_file(self) -> None:
        selected = self.report_table.selection()
        if not selected:
            return
        record = self.report_records.get(selected[0])
        if not record:
            return
        path = Path(record["path"])
        try:
            if not path.is_file():
                raise RuntimeError("报告文件不存在")
            os.startfile(path)
        except Exception as exc:
            self.report_summary_var.set("无法打开报告文件")
            self.report_meta_var.set(str(exc))

    def _show_result(
        self, title: str, detail: str = "", severity: str = "info"
    ) -> None:
        color, background, border = {
            "success": (GREEN, PALE_GREEN, "#bcdcc9"),
            "warning": (AMBER, PALE_AMBER, "#ead59a"),
            "error": (RED, PALE_RED, "#efc6c0"),
        }.get(severity, (BLUE, PALE_BLUE, "#c9dfe7"))
        self.result_title_var.set(title)
        self.result_detail_var.set(detail)
        self.result_frame.configure(bg=background, highlightbackground=border)
        self.result_title_label.configure(bg=background, fg=color)
        self.result_detail_label.configure(bg=background)
        self.result_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))

    def _clear_result(self) -> None:
        self.result_frame.grid_remove()
        self.result_title_var.set("")
        self.result_detail_var.set("")

    def _mount_settings_panel(
        self,
        config: AppConfig,
        *,
        is_new: bool = False,
        status: str | None = None,
        severity: str = "info",
    ) -> None:
        if self.settings_panel is not None:
            self.settings_panel.dispose()
            self.settings_panel.destroy()
        self.settings_panel = SettingsPanel(
            self.settings_tab,
            self,
            self.config_store,
            self.credentials,
            config,
            is_new=is_new,
        )
        self.settings_panel.pack(fill="both", expand=True)
        if status:
            self.settings_panel.set_status(status, severity)

    def show_archive_tab(self) -> None:
        self.main_notebook.select(self.archive_tab)

    def show_settings_tab(self) -> None:
        self.main_notebook.select(self.settings_tab)

    def _reset_transfer_metrics(self) -> None:
        self._speed_sample_at = None
        self._speed_sample_bytes = 0
        self._transfer_speed = None
        self.progress_detail_var.set("")

    def _handle_download_progress(
        self,
        update: DownloadProgress,
        batch_index: int = 1,
        batch_total: int = 1,
    ) -> None:
        total = update.overall_total
        date_percent = (update.overall_current / total * 100) if total else 0
        batch_percent = (
            ((batch_index - 1) + date_percent / 100) / batch_total * 100
            if batch_total
            else 0
        )
        self.progress.configure(value=min(max(batch_percent, 0), 100))
        if batch_total > 1:
            self.progress_percent_var.set(
                f"总 {batch_percent:.0f}% · 本日 {date_percent:.0f}%"
            )
            title_prefix = f"批量下载 {batch_index}/{batch_total} · "
        else:
            self.progress_percent_var.set(f"{date_percent:.0f}%")
            title_prefix = ""

        source = replica_label(update.source) if update.source else "云端"
        if update.stage == "preparing":
            self.status_var.set(
                f"{title_prefix}{update.archive_date} · 正在准备下载"
            )
            if update.object_count:
                detail = (
                    f"来源 {source} · {update.object_count} 个对象 · "
                    f"{update.download_workers} 并发 · "
                    f"共 {human_bytes(update.bytes_total)}"
                )
            else:
                detail = "空归档，无对象需要下载"
            self.progress_detail_var.set(detail)
            return

        if update.stage == "switching":
            self._speed_sample_at = None
            self._transfer_speed = None
            self.status_var.set(
                f"{title_prefix}{update.archive_date} · 正在切换到 {source}"
            )
            self.progress_detail_var.set(
                f"已完成 {update.completed_objects}/{update.object_count} · "
                f"{compact_name(update.object_name or '')}"
            )
            return

        if update.stage == "downloading":
            now = time.monotonic()
            network_bytes = update.network_bytes_completed
            if self._speed_sample_at is None or network_bytes < self._speed_sample_bytes:
                self._speed_sample_at = now
                self._speed_sample_bytes = network_bytes
            else:
                elapsed = now - self._speed_sample_at
                if elapsed >= 0.4:
                    delta = network_bytes - self._speed_sample_bytes
                    instant_speed = delta / elapsed if delta > 0 else 0
                    if instant_speed > 0:
                        self._transfer_speed = (
                            instant_speed
                            if self._transfer_speed is None
                            else self._transfer_speed * 0.7 + instant_speed * 0.3
                        )
                    self._speed_sample_at = now
                    self._speed_sample_bytes = network_bytes

            parts = [
                f"已完成 {update.completed_objects}/{update.object_count}",
                f"并发 {update.active_transfers}",
                compact_name(update.object_name or ""),
                f"{human_bytes(update.bytes_completed)} / {human_bytes(update.bytes_total)}",
            ]
            if self._transfer_speed:
                remaining = max(update.bytes_total - update.bytes_completed, 0)
                if batch_total > 1:
                    self.progress_percent_var.set(
                        f"总 {batch_percent:.0f}% · "
                        f"{human_bytes(int(self._transfer_speed))}/s · "
                        f"约剩 {human_duration(remaining / self._transfer_speed)}"
                    )
                    parts.insert(0, f"本日 {date_percent:.0f}%")
                else:
                    self.progress_percent_var.set(
                        f"{date_percent:.0f}% · "
                        f"{human_bytes(int(self._transfer_speed))}/s · "
                        f"约剩 {human_duration(remaining / self._transfer_speed)}"
                    )
            self.status_var.set(
                f"{title_prefix}{update.archive_date} · 正在从 {source} 下载"
            )
            self.progress_detail_var.set(" · ".join(part for part in parts if part))
            return

        if update.stage == "verifying":
            self.status_var.set(
                f"{title_prefix}{update.archive_date} · 正在执行完整恢复校验"
            )
            if update.stage_current:
                detail = (
                    f"对象 {update.stage_current}/{update.stage_total} · "
                    f"{compact_name(update.object_name or '')}"
                )
            else:
                detail = f"下载完成 {human_bytes(update.bytes_total)} · 正在准备校验"
            self.progress_detail_var.set(detail)
            return

        if update.stage == "complete":
            self.status_var.set(
                f"{title_prefix}{update.archive_date} · 下载与完整校验完成"
            )
            self.progress_detail_var.set(
                f"{update.object_count} 个对象 · {human_bytes(update.bytes_total)}"
            )

    def _set_busy(self, value: bool, status: str | None = None) -> None:
        self.busy = value
        state = "disabled" if value else "normal"
        for button in (
            self.refresh_button,
            self.verify_button,
            self.add_profile_button,
            self.schedule_button,
        ):
            button.configure(state=state)
        self._update_download_button_state()
        self.cancel_button.configure(
            state="normal"
            if value
            and self._active_cancel is not None
            and not self._active_cancel.is_set()
            else "disabled"
        )
        self.profile_combo.configure(state="disabled" if value else "readonly")
        if status:
            self.status_var.set(status)
        if not value:
            self.progress.configure(value=0)
            self._operation_kind = None
            self._active_cancel = None
            self._active_thread = None

    def _update_download_button_state(self) -> None:
        self.download_button.configure(
            state="disabled" if self.busy or self.schedule_installed else "normal"
        )

    def _begin_operation(self, kind: str, *, cancelable: bool) -> threading.Event | None:
        self._operation_kind = kind
        self._active_cancel = threading.Event() if cancelable else None
        return self._active_cancel

    def cancel_current_operation(self) -> None:
        cancel = self._active_cancel
        if cancel is None or cancel.is_set():
            return
        if not messagebox.askyesno(
            "取消当前任务",
            "确认停止当前下载或校验？\n"
            "下载断点会保留，未完成校验报告不会发布。",
            parent=self,
        ):
            return
        cancel.set()
        self.cancel_button.configure(state="disabled")
        self.status_var.set("正在安全停止当前任务")
        self.progress_detail_var.set("正在关闭网络连接并保留断点数据")

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.settings_panel and self.settings_panel.tests_running:
            self.show_settings_tab()
            self.settings_panel.set_status(
                "连接测试尚未完成，请等待测试结束后再关闭客户端。",
                "warning",
            )
            return
        thread = self._active_thread
        if not self.busy or thread is None or not thread.is_alive():
            self.destroy()
            return
        if not messagebox.askyesno(
            "关闭客户端",
            "当前任务尚未完成。确认安全停止并关闭客户端？",
            parent=self,
        ):
            return
        self._closing = True
        if self._active_cancel is not None:
            self._active_cancel.set()
            self.status_var.set("正在安全停止并关闭客户端")
            self.progress_detail_var.set("正在终止传输并保留 .partial 断点数据")
        else:
            self.status_var.set("正在等待当前只读请求结束后关闭")
            self.progress_detail_var.set("不会强制中断正在运行的 rclone 请求")
        self.cancel_button.configure(state="disabled")
        self.after(100, self._wait_for_safe_close)

    def _wait_for_safe_close(self) -> None:
        thread = self._active_thread
        if thread is None or not thread.is_alive():
            self.destroy()
            return
        self.after(100, self._wait_for_safe_close)

    def _schedule_summary(self) -> str:
        return "已启用" if self.schedule_installed else "未启用"

    def _schedule_meta(self) -> str:
        if not self.schedule_installed:
            return "没有计划任务"
        enabled = sum(profile.enabled for profile in self.profiles)
        return f"{enabled} 个配置 · 每 30 分钟"

    def _start_scheduled_sync(self) -> None:
        started_at = time.time()
        run_task()
        self._scheduled_run_started_at = started_at
        self.schedule_detail_var.set(f"检查中 · UTC {utc_yesterday()}")
        self.after(2_000, self._poll_scheduled_run)

    def _run_startup_sync(self) -> None:
        if self._closing or not self.schedule_installed:
            return
        try:
            self._start_scheduled_sync()
        except Exception as exc:
            self._show_result(
                "启动自动同步检查失败",
                str(exc),
                "warning",
            )
            return

    def _poll_scheduled_run(self) -> None:
        started_at = self._scheduled_run_started_at
        if self._closing or started_at is None:
            return
        log_dir = app_data_dir() / "logs"
        candidates = [
            path
            for path in log_dir.glob("sync-*.json")
            if path.stat().st_mtime >= started_at - 1
        ]
        if not candidates:
            if time.time() - started_at < 6 * 60 * 60:
                self.after(2_000, self._poll_scheduled_run)
            else:
                self._scheduled_run_started_at = None
                self._show_result(
                    "自动同步仍未返回结果",
                    "请检查 Windows 计划任务状态和同步审计日志。",
                    "warning",
                )
            return
        log_path = max(candidates, key=lambda path: path.stat().st_mtime)
        try:
            payload = json.loads(log_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.after(1_000, self._poll_scheduled_run)
            return
        self._scheduled_run_started_at = None
        profiles = payload.get("profiles") or []
        failures = [
            f"{item.get('display_name') or item.get('profile_id') or '未知配置'}："
            f"{item.get('error') or '未知错误'}"
            for item in profiles
            if not item.get("success")
        ]
        if payload.get("success"):
            self.schedule_detail_var.set(
                f"刚完成 · {len(profiles)} 配置 · 列表刷新中"
            )
        else:
            self._show_result(
                "自动同步失败",
                "\n".join(failures) or "审计结果未包含具体失败原因。",
                "error",
            )
        self._refresh_after_scheduled_run()

    def _refresh_after_scheduled_run(self) -> None:
        if self._closing:
            return
        if self.busy:
            self.after(1_000, self._refresh_after_scheduled_run)
            return
        self.refresh(force=True)

    def _worker(self, task: Callable[[], Any], success_event: str) -> None:
        def run() -> None:
            try:
                result = task()
            except OperationCancelled as exc:
                self.events.put(("operation_cancelled", str(exc)))
            except Exception as exc:
                LOGGER.exception("Background operation failed")
                self.events.put(("error", exc))
            else:
                self.events.put((success_event, result))

        thread = threading.Thread(target=run, daemon=True)
        self._active_thread = thread
        thread.start()

    def refresh(self, *, force: bool = False) -> None:
        if self.busy:
            return
        self._begin_operation("scan", cancelable=False)
        self._usage_refreshing = {"google_drive", "r2", "local"}
        self._scan_progress_keys.clear()
        self._scan_progress_total = 0
        self._set_busy(True, "正在读取双云端日期目录")
        self._reset_transfer_metrics()
        self.progress_detail_var.set(
            "同时读取 Google Drive、Cloudflare R2 与容量信息"
        )
        self.progress_percent_var.set("")
        self.progress.stop()
        self.progress.configure(mode="indeterminate", maximum=100, value=8)
        self.progress.start(24)
        self._render_metrics()
        self.update_idletasks()

        def report(event: str, payload: Any) -> None:
            self.events.put(("scan_update", (event, payload)))

        def run() -> None:
            try:
                result = self.manager.scan_dashboard(
                    force_refresh=force,
                    update=report,
                )
            except Exception as exc:
                LOGGER.exception("Dashboard scan failed")
                self.events.put(("scan_error", exc))
            else:
                self.events.put(("scan_complete", result))

        thread = threading.Thread(target=run, daemon=True)
        self._active_thread = thread
        thread.start()

    def _selected_dates(self) -> tuple[str, ...]:
        selected = set(self.table.selection())
        return tuple(
            item for item in self.table.get_children() if item in selected
        )

    def _on_table_selection(self, _event: tk.Event | None = None) -> None:
        count = len(self.table.selection())
        text = "↓ 下载选中" if count <= 1 else f"↓ 下载选中（{count}）"
        self.download_button.configure(text=text)

    def _progress_callback(self, name: str, current: int, total: int) -> None:
        self.events.put(("progress", (name, current, total)))

    def _download_dates(self, archive_dates: tuple[str, ...]) -> None:
        if self.busy:
            return
        self.show_archive_tab()
        self._clear_result()
        cancel = self._begin_operation("download", cancelable=True)
        assert cancel is not None
        batch_total = len(archive_dates)
        self._set_busy(
            True,
            f"正在下载 {archive_dates[0]}"
            if batch_total == 1
            else f"正在批量下载 {batch_total} 个日期",
        )
        self._reset_transfer_metrics()
        self.progress_detail_var.set("正在读取并比较双云端 manifest")
        self.progress_percent_var.set("0%" if batch_total == 1 else "总 0%")
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0, maximum=100)

        def run_batch() -> BatchDownloadResult:
            batch = BatchDownloadResult(requested_dates=list(archive_dates))
            for batch_index, archive_date in enumerate(archive_dates, start=1):
                if cancel.is_set():
                    raise OperationCancelled(
                        "操作已取消，未完成文件保留在 .partial 目录"
                    )
                self.events.put(
                    (
                        "download_date_start",
                        (archive_date, batch_index, batch_total),
                    )
                )
                try:
                    result = self.manager.download_day(
                        archive_date,
                        detail_progress=lambda update, index=batch_index: self.events.put(
                            (
                                "download_progress",
                                (update, index, batch_total),
                            )
                        ),
                        cancel=cancel,
                    )
                except OperationCancelled:
                    raise
                except Exception as exc:
                    batch.failures[archive_date] = str(exc)
                    self.events.put(
                        (
                            "download_date_failed",
                            (archive_date, batch_index, batch_total, str(exc)),
                        )
                    )
                else:
                    batch.results.append(result)
            return batch

        self._worker(
            run_batch,
            "download_batch",
        )

    def download_selected(self) -> None:
        archive_dates = self._selected_dates()
        if not archive_dates:
            self._show_result(
                "请选择日期",
                "请先在列表中选择一个或多个归档日期。",
                "warning",
            )
            return
        self._download_dates(archive_dates)

    def verify_selected(self) -> None:
        archive_dates = self._selected_dates()
        if not archive_dates:
            self._show_result(
                "请选择日期", "请先在列表中选择一个归档日期。", "warning"
            )
            return
        if len(archive_dates) != 1:
            self._show_result(
                "只能校验一个日期",
                "重新校验一次只能处理一个日期，请只保留一行选中。",
                "warning",
            )
            return
        archive_date = archive_dates[0]
        if self.busy:
            return
        self.show_archive_tab()
        self._clear_result()
        cancel = self._begin_operation("verify", cancelable=True)
        assert cancel is not None
        self._set_busy(True, f"正在校验 {archive_date}")
        self._reset_transfer_metrics()
        self.progress_detail_var.set("正在读取并校验本地归档")
        self.progress_percent_var.set("0%")
        self.progress.configure(mode="determinate", value=0, maximum=100)
        self._worker(
            lambda: self.manager.verify_existing_day(
                archive_date, self._progress_callback, cancel
            ),
            "verify",
        )

    def open_settings(self) -> None:
        self.show_settings_tab()

    def _sync_profile_selector(self) -> None:
        names = [
            profile.display_name.strip() or profile.profile_id
            for profile in self.profiles
        ]
        name_counts = {name: names.count(name) for name in names}
        self.profile_choice_to_id = {}
        self.profile_id_to_choice = {}
        for profile, name in zip(self.profiles, names):
            choice = (
                name
                if name_counts[name] == 1
                else f"{name} ({profile.profile_id})"
            )
            self.profile_choice_to_id[choice] = profile.profile_id
            self.profile_id_to_choice[profile.profile_id] = choice
        self.profile_combo.configure(values=list(self.profile_choice_to_id))
        self.profile_var.set(
            self.profile_id_to_choice.get(
                self.config_value.profile_id,
                self.config_value.display_name.strip() or self.config_value.profile_id,
            )
        )

    def _select_profile(self, _event: tk.Event | None = None) -> None:
        profile_id = self.profile_choice_to_id.get(self.profile_var.get())
        if not profile_id or profile_id == self.config_value.profile_id:
            return
        if self.settings_panel and self.settings_panel.tests_running:
            self.profile_var.set(
                self.profile_id_to_choice[self.config_value.profile_id]
            )
            self.show_settings_tab()
            self.settings_panel.set_status(
                "连接测试尚未完成，暂时不能切换配置。",
                "warning",
            )
            return
        try:
            self.config_store.set_active(profile_id)
        except Exception as exc:
            self.profile_var.set(
                self.profile_id_to_choice[self.config_value.profile_id]
            )
            self._show_result("无法切换配置", str(exc), "error")
            self.show_archive_tab()
            return
        self.reload_profiles(profile_id)

    def reload_profiles(
        self,
        profile_id: str | None = None,
        *,
        settings_status: str | None = None,
        select_settings: bool = False,
        settings_severity: str = "success",
    ) -> None:
        self.profiles, stored_active = self.config_store.load_profiles()
        selected = profile_id or stored_active
        self.config_value = next(
            (
                profile
                for profile in self.profiles
                if profile.profile_id == selected
            ),
            self.profiles[0],
        )
        self.active_profile_id = self.config_value.profile_id
        self.manager = ArchiveManager(self.config_value, self.credentials)
        self.rows.clear()
        self.usage.clear()
        self.table.delete(*self.table.get_children())
        self.refresh_reports()
        for name in ("drive", "r2", "local"):
            self.metric_vars[name].set("--")
            self.metric_vars[f"{name}_detail"].set("正在读取")
        self._sync_profile_selector()
        self.metric_vars["schedule"].set(self._schedule_summary())
        self.metric_vars["schedule_meta"].set(self._schedule_meta())
        self._mount_settings_panel(
            self.config_value,
            status=settings_status,
            severity=settings_severity,
        )
        if select_settings:
            self.show_settings_tab()
        self.refresh()

    def reload_config(self) -> None:
        self.reload_profiles()

    def new_profile(self) -> None:
        if self.busy:
            return
        if self.settings_panel and self.settings_panel.tests_running:
            self.show_settings_tab()
            self.settings_panel.set_status(
                "连接测试尚未完成，暂时不能新增配置。",
                "warning",
            )
            return
        current = self.config_value
        config = AppConfig(
            profile_id="",
            display_name="",
            enabled=True,
            collector_id="",
            drive_remote="",
            drive_prefix=current.drive_prefix,
            rclone_binary=current.rclone_binary,
            r2_endpoint="",
            r2_bucket="",
            r2_prefix=current.r2_prefix,
            r2_region=current.r2_region,
            local_root=current.local_root,
            preferred_replica=current.preferred_replica,
            require_both_replicas=current.require_both_replicas,
            history_days=current.history_days,
            download_workers=current.download_workers,
        )
        self._mount_settings_panel(
            config,
            is_new=True,
            status="填写新服务器配置；配置 ID 建议与 collector ID 相同。",
        )
        self.show_settings_tab()

    def delete_profile(self, profile_id: str) -> None:
        next_active = self.config_store.delete_profile(profile_id)
        credential_warning = ""
        try:
            self.credentials.clear_r2(profile_id)
        except Exception as exc:
            credential_warning = str(exc)
        status = "配置已删除"
        severity = "success"
        if credential_warning:
            status += "，但清理本机 R2 凭据失败：" + credential_warning
            severity = "warning"
        self.reload_profiles(
            next_active,
            settings_status=status,
            select_settings=True,
            settings_severity=severity,
        )

    def open_local_root(self) -> None:
        try:
            path = self.manager.collector_root
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except Exception as exc:
            self._show_result("无法打开目录", str(exc), "error")
            self.show_archive_tab()

    def _on_table_double_click(self, event: tk.Event) -> None:
        archive_date = self.table.identify_row(event.y)
        if not archive_date:
            return
        self.table.selection_set(archive_date)
        try:
            path = self.manager.final_root(archive_date)
            if path.is_dir():
                os.startfile(path)
                return
            self._show_result(
                "本地归档尚未完成",
                f"{archive_date} 还没有完整验证的本地归档。",
                "warning",
            )
        except Exception as exc:
            self._show_result("无法打开目录", str(exc), "error")

    def toggle_schedule(self) -> None:
        first_run_error = ""
        try:
            if task_installed():
                if not messagebox.askyesno(
                    "停止自动同步",
                    "确认删除同步所有启用配置的 Windows 自动任务？",
                    parent=self,
                ):
                    self.schedule_toggle_var.set(True)
                    return
                remove_task()
            else:
                install_task()
                if not task_installed():
                    raise RuntimeError(
                        "Windows 计划任务创建后未能读取，请检查任务计划程序"
                    )
                try:
                    self._start_scheduled_sync()
                except Exception as exc:
                    first_run_error = str(exc)
        except Exception as exc:
            self.schedule_toggle_var.set(task_installed())
            self._show_result("计划任务失败", str(exc), "error")
            self.show_archive_tab()
            return
        self.schedule_installed = task_installed()
        self._startup_sync_pending = False
        self.schedule_toggle_var.set(self.schedule_installed)
        self.metric_vars["schedule"].set(self._schedule_summary())
        self.metric_vars["schedule_meta"].set(self._schedule_meta())
        self.schedule_detail_var.set("")
        self._update_download_button_state()
        if self.schedule_installed:
            target_date = utc_yesterday()
            if first_run_error:
                self._show_result(
                    "自动同步已启用，但首次同步启动失败",
                    f"目标 UTC 日期：{target_date}\n{first_run_error}",
                    "warning",
                )
            else:
                self.schedule_detail_var.set(f"检查中 · UTC {target_date}")
        else:
            self._show_result("自动同步已停止", "Windows 计划任务已删除。", "success")

    def _render_metrics(self) -> None:
        rows = list(self.rows.values())
        drive_ok = sum(row.drive.state == "verified" for row in rows)
        r2_ok = sum(row.r2.state == "verified" for row in rows)
        local_ok = sum(row.local_state == "verified" for row in rows)

        drive_usage = self.usage.get("google_drive") or {}
        drive_metric = f"{drive_ok} 个归档日"
        if "google_drive" in self._usage_refreshing:
            if drive_usage and not drive_usage.get("error"):
                drive_detail = (
                    f"刷新中 · 上次 {human_bytes(int(drive_usage.get('used_bytes') or 0))} / "
                    f"{human_bytes(int(drive_usage.get('total_bytes') or 0))}"
                )
            else:
                drive_detail = "正在读取账号容量"
        elif drive_usage.get("error"):
            drive_detail = "容量读取失败"
        elif not drive_usage:
            drive_detail = "容量尚未读取"
        else:
            drive_detail = (
                f"已用 {human_bytes(int(drive_usage.get('used_bytes') or 0))} / "
                f"{human_bytes(int(drive_usage.get('total_bytes') or 0))}"
            )

        r2_usage = self.usage.get("r2") or {}
        r2_metric = f"{r2_ok} 个归档日"
        if "r2" in self._usage_refreshing:
            if r2_usage and not r2_usage.get("error"):
                r2_metric = (
                    f"{r2_ok} 个归档日 · "
                    f"{int(r2_usage.get('bucket_objects') or 0):,} 个对象"
                )
                r2_detail = (
                    f"刷新中 · 上次 {human_bytes(int(r2_usage.get('bucket_bytes') or 0))} / "
                    f"{human_bytes(R2_FREE_ALLOWANCE_BYTES)}"
                )
            else:
                r2_detail = "正在读取 Bucket 用量"
        elif r2_usage.get("error"):
            r2_detail = "用量读取失败"
        elif not r2_usage:
            r2_detail = "用量尚未读取"
        else:
            r2_metric = (
                f"{r2_ok} 个归档日 · "
                f"{int(r2_usage.get('bucket_objects') or 0):,} 个对象"
            )
            r2_detail = (
                f"已用 {human_bytes(int(r2_usage.get('bucket_bytes') or 0))} / "
                f"{human_bytes(R2_FREE_ALLOWANCE_BYTES)} · 免费额度"
            )

        local_usage = self.usage.get("local") or {}
        local_metric = f"{local_ok} 个归档日"
        if "local" in self._usage_refreshing:
            if local_usage and not local_usage.get("error"):
                local_detail = (
                    f"刷新中 · 上次 {human_bytes(int(local_usage.get('used_bytes') or 0))} / "
                    f"{human_bytes(int(local_usage.get('total_bytes') or 0))}"
                )
            else:
                local_detail = "正在读取磁盘容量"
        elif local_usage.get("error"):
            local_detail = "磁盘容量读取失败"
        elif not local_usage:
            local_detail = "磁盘容量尚未读取"
        else:
            local_detail = (
                f"已用 {human_bytes(int(local_usage.get('used_bytes') or 0))} / "
                f"{human_bytes(int(local_usage.get('total_bytes') or 0))}"
            )

        self.metric_vars["drive"].set(drive_metric)
        self.metric_vars["drive_detail"].set(drive_detail)
        self.metric_vars["r2"].set(r2_metric)
        self.metric_vars["r2_detail"].set(r2_detail)
        self.metric_vars["local"].set(local_metric)
        self.metric_vars["local_detail"].set(local_detail)
        drive_total = int(drive_usage.get("total_bytes") or 0)
        drive_used = int(drive_usage.get("used_bytes") or 0)
        r2_used = int(r2_usage.get("bucket_bytes") or 0)
        local_total = int(local_usage.get("total_bytes") or 0)
        local_used = int(local_usage.get("used_bytes") or 0)
        self.capacity_bars["drive"].configure(
            value=min(drive_used / drive_total * 100, 100) if drive_total else 0
        )
        self.capacity_bars["r2"].configure(
            value=min(r2_used / R2_FREE_ALLOWANCE_BYTES * 100, 100)
        )
        self.capacity_bars["local"].configure(
            value=min(local_used / local_total * 100, 100) if local_total else 0
        )

    def _render_row(
        self, row: ArchiveDayStatus, row_index: int | None = None
    ) -> None:
        match_text = (
            "✓ 一致"
            if row.replicas_match is True
            else "! 不一致"
            if row.replicas_match is False
            else "✓ 已核准清理"
            if row.drive.state == "cleaned" and row.r2.state == "cleaned"
            else "... 读取中"
            if "loading" in (row.drive.state, row.r2.state)
            else "--"
        )
        detail = next(
            (
                status.detail
                for status in (row.drive, row.r2)
                if status.snapshot is not None
            ),
            "正在读取 manifest"
            if "loading" in (row.drive.state, row.r2.state)
            else "--",
        )
        state_values = (row.drive.state, row.r2.state, row.local_state)
        tag = (
            "error"
            if row.replicas_match is False or "error" in state_values
            else "loading"
            if "loading" in state_values
            else "ok"
            if (
                row.replicas_match is True
                or (row.drive.state == "cleaned" and row.r2.state == "cleaned")
            )
            and row.local_state == "verified"
            else "warn"
        )
        values = (
            row.archive_date,
            STATE_TEXT.get(row.drive.state, row.drive.state),
            STATE_TEXT.get(row.r2.state, row.r2.state),
            match_text,
            STATE_TEXT.get(row.local_state, row.local_state),
            detail,
        )
        if self.table.exists(row.archive_date):
            if row_index is None:
                row_index = self.table.index(row.archive_date)
            stripe = "stripe_odd" if row_index % 2 else "stripe_even"
            self.table.item(row.archive_date, values=values, tags=(tag, stripe))
        else:
            if row_index is None:
                row_index = len(self.table.get_children())
            stripe = "stripe_odd" if row_index % 2 else "stripe_even"
            self.table.insert(
                "", "end", iid=row.archive_date, values=values, tags=(tag, stripe)
            )

    def _handle_scan_rows(self, rows: list[ArchiveDayStatus]) -> None:
        selected = set(self.table.selection())
        current = set(self.table.get_children())
        incoming = {row.archive_date for row in rows}
        for archive_date in current - incoming:
            self.table.delete(archive_date)
        self.rows = {row.archive_date: row for row in rows}
        for index, row in enumerate(rows):
            self._render_row(row, index)
            self.table.move(row.archive_date, "", index)
        retained = tuple(
            row.archive_date for row in rows if row.archive_date in selected
        )
        if retained:
            self.table.selection_set(*retained)
        elif rows:
            self.table.selection_set(rows[0].archive_date)

        self._scan_progress_keys = {
            ("replica", row.archive_date, remote)
            for row in rows
            for remote, status in (
                ("google_drive", row.drive),
                ("r2", row.r2),
            )
            if status.state == "loading"
        }
        self._scan_progress_keys.update(
            ("usage", name) for name in self._usage_refreshing
        )
        self._scan_progress_total = len(self._scan_progress_keys)
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self.progress_percent_var.set("0%" if self._scan_progress_total else "")
        self.status_var.set(
            f"已发现 {len(rows)} 个归档日期，正在逐项校验双云端 manifest"
        )
        self.progress_detail_var.set(
            f"还有 {self._scan_progress_total} 项等待完成"
            if self._scan_progress_total
            else "日期状态已读取，正在汇总结果"
        )
        self._render_metrics()

    def _mark_scan_item(self, key: tuple[str, ...], label: str) -> None:
        if key not in self._scan_progress_keys:
            self.progress_detail_var.set(f"刚完成：{label}")
            return
        self._scan_progress_keys.remove(key)
        completed = self._scan_progress_total - len(self._scan_progress_keys)
        percent = (
            completed / self._scan_progress_total * 100
            if self._scan_progress_total
            else 100
        )
        self.progress.configure(value=percent)
        self.progress_percent_var.set(f"{percent:.0f}%")
        self.progress_detail_var.set(
            f"刚完成：{label} · {completed}/{self._scan_progress_total} 项"
        )

    def _handle_scan_update(self, event: str, payload: Any) -> None:
        if event == "date_source":
            detail = (
                f"读取失败：{payload['error']}"
                if payload.get("error")
                else f"发现 {payload['count']} 个日期"
            )
            self.status_var.set("正在读取双云端日期目录")
            self.progress_detail_var.set(
                f"刚完成：{payload['label']} 日期目录 · {detail}"
            )
            return
        if event == "rows":
            self._handle_scan_rows(payload)
            return
        if event == "usage":
            name = payload["name"]
            self.usage[name] = payload["value"]
            self._usage_refreshing.discard(name)
            self._render_metrics()
            label = {
                "google_drive": "Google Drive 容量",
                "r2": "Cloudflare R2 用量",
                "local": "本地磁盘容量",
            }.get(name, name)
            self._mark_scan_item(("usage", name), label)
            return
        if event == "replica":
            archive_date = payload["archive_date"]
            remote = payload["remote"]
            row = self.rows.get(archive_date)
            if row is None:
                return
            if remote == "google_drive":
                row.drive = payload["status"]
            else:
                row.r2 = payload["status"]
            row.replicas_match = payload["replicas_match"]
            self._render_row(row)
            self._render_metrics()
            remote_name = replica_label(remote)
            state = STATE_TEXT.get(payload["status"].state, payload["status"].state)
            self.status_var.set("正在逐项读取双云端 manifest")
            self._mark_scan_item(
                ("replica", archive_date, remote),
                f"{remote_name} {archive_date} · {state}",
            )

    def _handle_scan_complete(
        self,
        result: tuple[
            list[ArchiveDayStatus],
            list[str],
            dict[str, dict[str, Any]],
        ],
    ) -> None:
        rows, errors, usage = result
        self.usage = usage
        self._usage_refreshing.clear()
        self._scan_progress_keys.clear()
        self._render_metrics()
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100)
        self._set_busy(False)
        self.progress.configure(value=100)
        self.progress_percent_var.set("100%")
        self.status_var.set(
            "；".join(errors)
            if errors
            else "双云端清单已更新"
        )
        schedule_detail = self.schedule_detail_var.get()
        if not errors and schedule_detail.endswith("列表刷新中"):
            self.schedule_detail_var.set(
                schedule_detail.removesuffix("列表刷新中") + "列表已刷新"
            )
        if errors:
            self._show_result("清单读取完成但有异常", "\n".join(errors), "warning")
        verified = sum(row.replicas_match is True for row in rows)
        self.progress_detail_var.set(
            f"读取完成：{len(rows)} 个日期 · {verified} 个双副本一致"
        )
        if self._startup_sync_pending:
            self._startup_sync_pending = False
            self.after(0, self._run_startup_sync)

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if (
                    self._active_cancel is not None
                    and self._active_cancel.is_set()
                    and event in {"progress", "download_progress"}
                ):
                    continue
                if event == "progress":
                    name, current, total = payload
                    percent = (current / total * 100) if total else 0
                    self.progress.configure(value=min(percent, 100))
                    self.progress_percent_var.set(f"{percent:.0f}%")
                    self.status_var.set(f"{name}  {percent:.0f}%")
                elif event == "download_progress":
                    update, batch_index, batch_total = payload
                    self._handle_download_progress(
                        update,
                        batch_index,
                        batch_total,
                    )
                elif event == "download_date_start":
                    archive_date, batch_index, batch_total = payload
                    self._reset_transfer_metrics()
                    if batch_total > 1:
                        self.status_var.set(
                            f"批量下载 {batch_index}/{batch_total} · "
                            f"{archive_date} · 正在读取双云端 manifest"
                        )
                        self.progress_percent_var.set(
                            f"总 {((batch_index - 1) / batch_total * 100):.0f}% · "
                            "本日 0%"
                        )
                    else:
                        self.status_var.set(
                            f"{archive_date} · 正在读取双云端 manifest"
                        )
                        self.progress_percent_var.set("0%")
                    self.progress_detail_var.set("正在比较双云端副本并选择下载来源")
                elif event == "download_date_failed":
                    archive_date, batch_index, batch_total, error = payload
                    percent = batch_index / batch_total * 100
                    self.progress.configure(value=percent)
                    self.progress_percent_var.set(f"总 {percent:.0f}%")
                    self.status_var.set(
                        f"{archive_date} 下载失败，继续处理其余日期"
                    )
                    self.progress_detail_var.set(compact_name(error, 76))
                elif event == "scan_update":
                    update_event, update_payload = payload
                    self._handle_scan_update(update_event, update_payload)
                elif event == "scan_complete":
                    if self._closing:
                        self.destroy()
                        return
                    self._handle_scan_complete(payload)
                elif event == "scan_error":
                    if self._closing:
                        self.destroy()
                        return
                    self._usage_refreshing.clear()
                    self._scan_progress_keys.clear()
                    self._render_metrics()
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self._set_busy(False, "双云端清单读取失败")
                    self._show_result("清单读取失败", str(payload), "error")
                    self.show_archive_tab()
                elif event == "download_batch":
                    if self._closing:
                        self.destroy()
                        return
                    batch: BatchDownloadResult = payload
                    requested_count = len(batch.requested_dates)
                    downloaded = sum(
                        result.status != "already_verified"
                        for result in batch.results
                    )
                    existing = sum(
                        result.status == "already_verified"
                        for result in batch.results
                    )
                    downloaded_bytes = sum(
                        result.bytes_downloaded for result in batch.results
                    )
                    failure_count = len(batch.failures)
                    self._set_busy(
                        False,
                        f"处理完成：成功 {len(batch.results)}，失败 {failure_count}",
                    )
                    if requested_count == 1 and batch.results:
                        result = batch.results[0]
                        completion = (
                            "本地已有完整归档"
                            if result.status == "already_verified"
                            else "已下载并完整验证"
                        )
                        warning_text = (
                            "\n注意：" + "；".join(result.warnings)
                            if result.warnings
                            else ""
                        )
                        self._show_result(
                            "归档完成",
                            f"{result.archive_date}\n"
                            f"{completion}\n"
                            f"来源：{replica_label(result.replica)}\n"
                            f"{result.object_count} 个对象 / {result.row_count:,} 行\n"
                            f"本次下载 {human_bytes(result.bytes_downloaded)}"
                            f"{warning_text}",
                            "warning" if result.warnings else "success",
                        )
                    elif requested_count == 1:
                        archive_date = batch.requested_dates[0]
                        self._show_result(
                            "下载失败",
                            f"{archive_date}\n{batch.failures[archive_date]}",
                            "error",
                        )
                    else:
                        summary = (
                            f"已选择 {requested_count} 个日期\n"
                            f"新下载并验证：{downloaded}\n"
                            f"本地原有完整归档：{existing}\n"
                            f"失败：{failure_count}\n"
                            f"本次下载：{human_bytes(downloaded_bytes)}"
                        )
                        if batch.failures:
                            summary += "\n\n失败详情：\n" + "\n".join(
                                f"{archive_date}: {error}"
                                for archive_date, error in batch.failures.items()
                            )
                            self._show_result(
                                "批量下载完成但有失败",
                                summary,
                                "warning",
                            )
                        else:
                            self._show_result(
                                "批量下载完成",
                                summary,
                                "success",
                            )
                    self.show_archive_tab()
                    self.refresh()
                elif event == "verify":
                    if self._closing:
                        self.destroy()
                        return
                    self._set_busy(False, f"{payload['archive_date']} 校验通过")
                    self._show_result(
                        "重新校验通过",
                        f"{payload['archive_date']} 的本地归档完整且与 manifest 匹配。",
                        "success",
                    )
                    self.show_archive_tab()
                    self.refresh()
                elif event == "operation_cancelled":
                    if self._closing:
                        self.destroy()
                        return
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self._set_busy(False, "当前任务已安全停止")
                    self.progress_detail_var.set(str(payload))
                    self._show_result("当前任务已安全停止", str(payload), "warning")
                    self.show_archive_tab()
                elif event == "error":
                    if self._closing:
                        self.destroy()
                        return
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self._set_busy(False, "操作失败")
                    self._show_result("操作失败", str(payload), "error")
                    self.show_archive_tab()
        except queue.Empty:
            pass
        except Exception as exc:
            LOGGER.exception("UI event handling failed")
            self.progress.stop()
            self.progress.configure(mode="determinate")
            thread = self._active_thread
            if thread is not None and thread.is_alive():
                if self._active_cancel is not None:
                    self._active_cancel.set()
                    self.cancel_button.configure(state="disabled")
                self.status_var.set("界面事件处理失败，正在安全停止后台任务")
            else:
                self._set_busy(False, "界面事件处理失败")
            if not self._closing:
                self._show_result("界面处理失败", str(exc), "error")
                self.show_archive_tab()
        self.after(100, self._drain_events)

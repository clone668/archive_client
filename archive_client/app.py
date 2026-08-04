from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

from .config import AppConfig, ConfigStore
from .credentials import CredentialStore
from .manager import ArchiveManager, replica_label
from .models import (
    ArchiveDayStatus,
    BatchDownloadResult,
    DownloadProgress,
    DownloadResult,
)
from .remotes import DriveRemote, R2Remote, resolve_rclone_binary
from .scheduler import install_task, remove_task, task_installed


BG = "#f4f6f8"
SURFACE = "#ffffff"
INK = "#1f2933"
MUTED = "#64748b"
BORDER = "#d9e0e7"
GREEN = "#13795b"
AMBER = "#9a6700"
RED = "#b42318"
BLUE = "#1d4ed8"


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
    "loading": "读取中",
    "verified": "已验证",
    "cleaned": "已清理",
    "missing": "缺失",
    "error": "异常",
    "partial": "下载中断",
    "unverified": "未验证",
}


class SettingsDialog(tk.Toplevel):
    def __init__(
        self,
        parent: "ArchiveClientApp",
        config_store: ConfigStore,
        credentials: CredentialStore,
        config: AppConfig,
        *,
        is_new: bool = False,
    ) -> None:
        super().__init__(parent)
        self.parent = parent
        self.config_store = config_store
        self.credentials = credentials
        self.config = config
        self.is_new = is_new
        self.title(f"连接与存储设置 - {config.display_name}")
        self.geometry("760x580")
        self.minsize(700, 520)
        self.configure(bg=BG)
        self.transient(parent)
        self.grab_set()

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
            "access_key": tk.StringVar(),
            "secret_key": tk.StringVar(),
        }
        self.drive_status_var = tk.StringVar(value="连接：尚未测试")
        self.r2_status_var = tk.StringVar()
        self.r2_connection_text = "尚未测试"
        self.r2_credential_text = self._credential_status()
        self.test_events: queue.Queue[tuple[str, bool, str]] = queue.Queue()
        self.test_buttons: dict[str, ttk.Button] = {}
        self._build()
        self._update_r2_status()
        self._poll_id = self.after(100, self._drain_test_events)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _section(self, parent: tk.Widget, title: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=(14, 10))
        frame.columnconfigure(1, weight=1)
        frame.pack(fill="x", pady=(0, 10))
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
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        entry = ttk.Entry(frame, textvariable=variable, show=show or "")
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        return entry

    def _build(self) -> None:
        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)

        actions = ttk.Frame(container)
        actions.pack(side="bottom", fill="x", pady=(12, 0))
        if not self.is_new and self.parent.profile_count > 1:
            ttk.Button(
                actions, text="删除此配置", command=self._delete_profile
            ).pack(side="left")
        ttk.Button(actions, text="取消", command=self._close).pack(side="right")
        ttk.Button(
            actions, text="保存设置", style="Primary.TButton", command=self._save
        ).pack(side="right", padx=(0, 8))

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(side="top", fill="both", expand=True)
        general_tab = ttk.Frame(self.notebook, padding=12)
        drive_tab = ttk.Frame(self.notebook, padding=12)
        r2_tab = ttk.Frame(self.notebook, padding=12)
        policy_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(general_tab, text="常规")
        self.notebook.add(drive_tab, text="Google Drive")
        self.notebook.add(r2_tab, text="Cloudflare R2")
        self.notebook.add(policy_tab, text="同步策略")

        identity = self._section(general_tab, "归档流")
        profile_id_entry = self._field(
            identity, 0, "配置 ID", self.vars["profile_id"]
        )
        profile_id_entry.configure(state="readonly")
        self._field(identity, 1, "配置名称", self.vars["display_name"])
        self._field(identity, 2, "采集流 ID", self.vars["collector_id"])
        self._field(identity, 3, "本地目录", self.vars["local_root"])
        ttk.Button(identity, text="选择目录", command=self._choose_root).grid(
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
        ttk.Button(drive, text="配置 OAuth", command=self._configure_drive).grid(
            row=2, column=2, padx=(8, 0)
        )
        ttk.Label(drive, textvariable=self.drive_status_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(5, 0)
        )
        self.test_buttons["google_drive"] = ttk.Button(
            drive, text="测试连接", command=lambda: self._test_connection("google_drive")
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
        ttk.Label(r2, textvariable=self.r2_status_var).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(5, 0)
        )
        self.test_buttons["r2"] = ttk.Button(
            r2, text="测试连接", command=lambda: self._test_connection("r2")
        )
        self.test_buttons["r2"].grid(
            row=6, column=2, padx=(8, 0), pady=(5, 0)
        )

        policy = self._section(policy_tab, "同步策略")
        ttk.Label(policy, text="首选下载副本").grid(
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
        ttk.Label(policy, text="显示历史天数").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Spinbox(
            policy, from_=7, to=3650, textvariable=self.vars["history_days"]
        ).grid(row=2, column=1, sticky="w", pady=5)

    def _choose_root(self) -> None:
        selected = filedialog.askdirectory(
            parent=self, initialdir=str(self.vars["local_root"].get())
        )
        if selected:
            self.vars["local_root"].set(selected)

    def _configure_drive(self) -> None:
        binary = str(self.vars["rclone_binary"].get()).strip() or "rclone"
        resolved = resolve_rclone_binary(binary)
        if not resolved:
            messagebox.showerror("rclone 不可用", "未找到 rclone。", parent=self)
            return
        subprocess.Popen(
            [resolved, "config"],
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
        )
        self.drive_status_var.set("连接：OAuth 配置窗口已打开，完成后请测试")

    def _credential_status(self) -> str:
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
        )

    def _test_connection(self, replica: str) -> None:
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
            messagebox.showerror("无法测试连接", str(exc), parent=self)
            return

        button = self.test_buttons[replica]
        button.configure(state="disabled")

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
                self.test_buttons[replica].configure(state="normal")
        except queue.Empty:
            pass
        self._poll_id = self.after(100, self._drain_test_events)

    def _close(self) -> None:
        try:
            self.after_cancel(self._poll_id)
        except (AttributeError, tk.TclError):
            pass
        self.destroy()

    def _save(self) -> None:
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
            messagebox.showerror("无法保存", str(exc), parent=self)
            return
        self.parent.reload_profiles(config.profile_id)
        self._close()

    def _delete_profile(self) -> None:
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
            messagebox.showerror("无法删除配置", str(exc), parent=self)
            return
        self._close()


class ArchiveClientApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SMSI 归档客户端")
        self.geometry("1180x760")
        self.minsize(920, 620)
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
        self._speed_sample_at: float | None = None
        self._speed_sample_bytes = 0
        self._transfer_speed: float | None = None
        self.schedule_installed = task_installed()
        self._configure_styles()
        self._build()
        self.after(100, self._drain_events)
        self.after(250, self.refresh)

    @property
    def profile_count(self) -> int:
        return len(self.profiles)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background=BG)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("TLabel", background=BG, foreground=INK, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Header.TLabel", font=("Segoe UI Semibold", 20), foreground=INK)
        style.configure("Metric.TLabel", background=SURFACE, font=("Segoe UI Semibold", 13))
        style.configure("MetricName.TLabel", background=SURFACE, foreground=MUTED)
        style.configure("Status.TLabel", font=("Segoe UI Semibold", 9), foreground=INK)
        style.configure("Percent.TLabel", font=("Segoe UI Semibold", 10), foreground=INK)
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 9))
        style.configure("Treeview", rowheight=34, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))

    def _build(self) -> None:
        header = ttk.Frame(self, padding=(22, 18, 22, 14))
        header.pack(fill="x")
        title_area = ttk.Frame(header)
        title_area.pack(side="left")
        ttk.Label(title_area, text="SMSI 归档客户端", style="Header.TLabel").pack(
            anchor="w"
        )
        self.identity_label = ttk.Label(
            title_area,
            text=(
                f"profile={self.config_value.profile_id} · "
                f"collector={self.config_value.collector_id}"
            ),
            style="Muted.TLabel",
        )
        self.identity_label.pack(anchor="w", pady=(3, 0))

        tools = ttk.Frame(header)
        tools.pack(side="right")
        self.profile_var = tk.StringVar()
        self.profile_choice_to_id: dict[str, str] = {}
        self.profile_combo = ttk.Combobox(
            tools,
            textvariable=self.profile_var,
            state="readonly",
            width=32,
        )
        self.profile_combo.pack(side="left", padx=(0, 8))
        self.profile_combo.bind("<<ComboboxSelected>>", self._select_profile)
        self.add_profile_button = ttk.Button(
            tools, text="新增配置", command=self.new_profile
        )
        self.add_profile_button.pack(side="left", padx=(0, 8))
        self.settings_button = ttk.Button(tools, text="设置", command=self.open_settings)
        self.settings_button.pack(side="left", padx=(0, 8))
        self.refresh_button = ttk.Button(
            tools,
            text="刷新",
            command=lambda: self.refresh(force=True),
        )
        self.refresh_button.pack(side="left")
        self._sync_profile_selector()

        metrics = ttk.Frame(self, style="Surface.TFrame", padding=(22, 14))
        metrics.pack(fill="x", padx=22)
        self.metric_vars = {
            "drive": tk.StringVar(value="--"),
            "r2": tk.StringVar(value="--"),
            "local": tk.StringVar(value="--"),
            "schedule": tk.StringVar(value=self._schedule_summary()),
        }
        for index, (name, label) in enumerate(
            (
                ("drive", "Google Drive 账号"),
                ("r2", "Cloudflare R2 Bucket"),
                ("local", "本地归档磁盘"),
                ("schedule", "自动同步"),
            )
        ):
            cell = ttk.Frame(metrics, style="Surface.TFrame")
            cell.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 18, 0))
            metrics.columnconfigure(index, weight=1)
            ttk.Label(cell, text=label, style="MetricName.TLabel").pack(anchor="w")
            ttk.Label(cell, textvariable=self.metric_vars[name], style="Metric.TLabel").pack(
                anchor="w", pady=(3, 0)
            )

        action_bar = ttk.Frame(self, padding=(22, 16, 22, 10))
        action_bar.pack(fill="x")
        self.download_button = ttk.Button(
            action_bar,
            text="下载选中日期",
            style="Primary.TButton",
            command=self.download_selected,
        )
        self.download_button.pack(side="left")
        self.verify_button = ttk.Button(
            action_bar, text="重新校验", command=self.verify_selected
        )
        self.verify_button.pack(side="left", padx=(8, 0))
        self.reports_button = ttk.Button(
            action_bar, text="打开报告", command=self.open_reports
        )
        self.reports_button.pack(side="left", padx=(8, 0))
        self.local_button = ttk.Button(
            action_bar, text="打开本地目录", command=self.open_local_root
        )
        self.local_button.pack(side="left", padx=(8, 0))
        self.schedule_button = ttk.Button(
            action_bar,
            text="停止自动同步" if self.schedule_installed else "启用自动同步",
            command=self.toggle_schedule,
        )
        self.schedule_button.pack(side="right")

        table_frame = ttk.Frame(self, padding=(22, 0, 22, 0))
        table_frame.pack(fill="both", expand=True)
        columns = ("date", "drive", "r2", "match", "local", "detail")
        self.table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        headings = {
            "date": "UTC 日期",
            "drive": "Google Drive",
            "r2": "Cloudflare R2",
            "match": "双副本",
            "local": "本地",
            "detail": "归档规模",
        }
        widths = {"date": 120, "drive": 130, "r2": 130, "match": 110, "local": 130, "detail": 320}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], anchor="w")
        self.table.tag_configure("ok", foreground=GREEN)
        self.table.tag_configure("loading", foreground=BLUE)
        self.table.tag_configure("warn", foreground=AMBER)
        self.table.tag_configure("error", foreground=RED)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind("<Double-1>", self._on_table_double_click)
        self.table.bind("<<TreeviewSelect>>", self._on_table_selection)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        status_bar = ttk.Frame(self, padding=(22, 10, 22, 16))
        status_bar.pack(fill="x")
        status_bar.columnconfigure(0, weight=1)
        status_text = ttk.Frame(status_bar)
        status_text.grid(row=0, column=0, sticky="ew", padx=(0, 18))
        self.status_var = tk.StringVar(value="正在连接")
        ttk.Label(status_text, textvariable=self.status_var, style="Status.TLabel").pack(
            anchor="w"
        )
        self.progress_detail_var = tk.StringVar()
        ttk.Label(
            status_text,
            textvariable=self.progress_detail_var,
            style="Muted.TLabel",
            anchor="w",
        ).pack(anchor="w", pady=(3, 0))
        progress_area = ttk.Frame(status_bar)
        progress_area.grid(row=0, column=1, sticky="e")
        self.progress_percent_var = tk.StringVar()
        ttk.Label(
            progress_area,
            textvariable=self.progress_percent_var,
            style="Percent.TLabel",
            anchor="e",
            width=36,
        ).pack(anchor="e")
        self.progress = ttk.Progressbar(progress_area, mode="determinate", length=300)
        self.progress.pack(anchor="e", pady=(3, 0))

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
                f"对象 {update.object_index}/{update.object_count} · "
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
                f"对象 {update.object_index}/{update.object_count}",
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
            self.download_button,
            self.verify_button,
            self.settings_button,
            self.add_profile_button,
        ):
            button.configure(state=state)
        self.profile_combo.configure(state="disabled" if value else "readonly")
        if status:
            self.status_var.set(status)
        if not value:
            self.progress.configure(value=0)

    def _schedule_summary(self) -> str:
        if not self.schedule_installed:
            return "未启用"
        enabled = sum(profile.enabled for profile in self.profiles)
        return f"已启用 · {enabled} 配置"

    def _worker(self, task: Callable[[], Any], success_event: str) -> None:
        def run() -> None:
            try:
                result = task()
            except Exception as exc:
                self.events.put(("error", exc))
            else:
                self.events.put((success_event, result))

        threading.Thread(target=run, daemon=True).start()

    def refresh(self, *, force: bool = False) -> None:
        if self.busy:
            return
        self._usage_refreshing = {"google_drive", "r2", "local"}
        self._scan_progress_keys.clear()
        self._scan_progress_total = 0
        self._set_busy(True, "正在读取双云端日期目录")
        self._reset_transfer_metrics()
        self.progress_detail_var.set(
            "同时读取 Google Drive、Cloudflare R2 与容量信息"
        )
        self.progress_percent_var.set("")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._render_metrics()

        def report(event: str, payload: Any) -> None:
            self.events.put(("scan_update", (event, payload)))

        def run() -> None:
            try:
                result = self.manager.scan_dashboard(
                    force_refresh=force,
                    update=report,
                )
            except Exception as exc:
                self.events.put(("scan_error", exc))
            else:
                self.events.put(("scan_complete", result))

        threading.Thread(target=run, daemon=True).start()

    def _selected_dates(self) -> tuple[str, ...]:
        selected = set(self.table.selection())
        return tuple(
            item for item in self.table.get_children() if item in selected
        )

    def _on_table_selection(self, _event: tk.Event | None = None) -> None:
        count = len(self.table.selection())
        text = "下载选中日期" if count <= 1 else f"下载选中日期（{count}）"
        self.download_button.configure(text=text)

    def _progress_callback(self, name: str, current: int, total: int) -> None:
        self.events.put(("progress", (name, current, total)))

    def _download_dates(self, archive_dates: tuple[str, ...]) -> None:
        if self.busy:
            return
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
                    )
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
            messagebox.showinfo(
                "请选择日期",
                "请先选择一个或多个归档日期。",
                parent=self,
            )
            return
        self._download_dates(archive_dates)

    def verify_selected(self) -> None:
        archive_dates = self._selected_dates()
        if not archive_dates:
            messagebox.showinfo("请选择日期", "请先选择一个归档日期。", parent=self)
            return
        if len(archive_dates) != 1:
            messagebox.showinfo(
                "只能校验一个日期",
                "“重新校验”一次只能处理一个日期，请只保留一行选中。",
                parent=self,
            )
            return
        archive_date = archive_dates[0]
        if self.busy:
            return
        self._set_busy(True, f"正在校验 {archive_date}")
        self._reset_transfer_metrics()
        self.progress_detail_var.set("正在读取并校验本地归档")
        self.progress_percent_var.set("0%")
        self.progress.configure(mode="determinate", value=0, maximum=100)
        self._worker(
            lambda: self.manager.verify_existing_day(
                archive_date, self._progress_callback
            ),
            "verify",
        )

    def open_settings(self) -> None:
        SettingsDialog(self, self.config_store, self.credentials, self.config_value)

    def _sync_profile_selector(self) -> None:
        self.profile_choice_to_id = {
            profile.label: profile.profile_id for profile in self.profiles
        }
        self.profile_combo.configure(values=list(self.profile_choice_to_id))
        self.profile_var.set(self.config_value.label)

    def _select_profile(self, _event: tk.Event | None = None) -> None:
        profile_id = self.profile_choice_to_id.get(self.profile_var.get())
        if not profile_id or profile_id == self.config_value.profile_id:
            return
        try:
            self.config_store.set_active(profile_id)
        except Exception as exc:
            self.profile_var.set(self.config_value.label)
            messagebox.showerror("无法切换配置", str(exc), parent=self)
            return
        self.reload_profiles(profile_id)

    def reload_profiles(self, profile_id: str | None = None) -> None:
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
        for name in ("drive", "r2", "local"):
            self.metric_vars[name].set("--")
        self.identity_label.configure(
            text=(
                f"profile={self.config_value.profile_id} · "
                f"collector={self.config_value.collector_id}"
            )
        )
        self._sync_profile_selector()
        self.metric_vars["schedule"].set(self._schedule_summary())
        self.refresh()

    def reload_config(self) -> None:
        self.reload_profiles()

    def new_profile(self) -> None:
        profile_id = simpledialog.askstring(
            "新增采集服务器",
            "输入稳定的配置 ID（建议与 collector ID 相同）：",
            parent=self,
        )
        if profile_id is None:
            return
        profile_id = profile_id.strip()
        if profile_id in {profile.profile_id for profile in self.profiles}:
            messagebox.showerror("配置已存在", "该配置 ID 已存在。", parent=self)
            return
        current = self.config_value
        config = AppConfig(
            profile_id=profile_id,
            display_name=profile_id,
            enabled=True,
            collector_id=profile_id,
            drive_remote=f"gdrive-{profile_id}:",
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
        )
        identity_errors = config.validate_identity()
        if identity_errors:
            messagebox.showerror(
                "配置 ID 无效", "；".join(identity_errors), parent=self
            )
            return
        SettingsDialog(
            self,
            self.config_store,
            self.credentials,
            config,
            is_new=True,
        )

    def delete_profile(self, profile_id: str) -> None:
        next_active = self.config_store.delete_profile(profile_id)
        credential_warning = ""
        try:
            self.credentials.clear_r2(profile_id)
        except Exception as exc:
            credential_warning = str(exc)
        self.reload_profiles(next_active)
        if credential_warning:
            messagebox.showwarning(
                "配置已删除",
                "配置已删除，但清理本机 R2 凭据失败：" + credential_warning,
                parent=self,
            )

    def open_reports(self) -> None:
        path = (
            self.config_value.archive_root
            / "reports"
            / f"collector={self.config_value.collector_id}"
        )
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def open_local_root(self) -> None:
        try:
            path = self.manager.collector_root
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self)

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
            messagebox.showinfo(
                "本地归档尚未完成",
                f"{archive_date} 还没有完整验证的本地归档。",
                parent=self,
            )
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self)

    def toggle_schedule(self) -> None:
        try:
            if task_installed():
                if not messagebox.askyesno(
                    "停止自动同步",
                    "确认删除同步所有启用配置的 Windows 自动任务？",
                    parent=self,
                ):
                    return
                remove_task()
            else:
                install_task()
        except Exception as exc:
            messagebox.showerror("计划任务失败", str(exc), parent=self)
            return
        self.schedule_installed = task_installed()
        self.metric_vars["schedule"].set(self._schedule_summary())
        self.schedule_button.configure(
            text="停止自动同步" if self.schedule_installed else "启用自动同步"
        )

    def _render_metrics(self) -> None:
        rows = list(self.rows.values())
        drive_ok = sum(row.drive.state == "verified" for row in rows)
        r2_ok = sum(row.r2.state == "verified" for row in rows)
        local_ok = sum(row.local_state == "verified" for row in rows)

        drive_usage = self.usage.get("google_drive") or {}
        if "google_drive" in self._usage_refreshing:
            if drive_usage and not drive_usage.get("error"):
                drive_metric = (
                    f"{drive_ok} 日 · 刷新中\n"
                    f"上次已用 {human_bytes(int(drive_usage.get('used_bytes') or 0))} / "
                    f"总计 {human_bytes(int(drive_usage.get('total_bytes') or 0))}"
                )
            else:
                drive_metric = f"{drive_ok} 日 · 读取中\n正在读取账号容量"
        elif drive_usage.get("error"):
            drive_metric = f"{drive_ok} 日\n容量读取失败"
        elif not drive_usage:
            drive_metric = f"{drive_ok} 日\n容量尚未读取"
        else:
            drive_metric = (
                f"{drive_ok} 日 · 已用 "
                f"{human_bytes(int(drive_usage.get('used_bytes') or 0))}\n"
                f"总计 {human_bytes(int(drive_usage.get('total_bytes') or 0))} · "
                f"剩余 {human_bytes(int(drive_usage.get('free_bytes') or 0))}"
            )

        r2_usage = self.usage.get("r2") or {}
        if "r2" in self._usage_refreshing:
            if r2_usage and not r2_usage.get("error"):
                r2_metric = (
                    f"{r2_ok} 日 · 刷新中\n"
                    f"上次 Bucket {human_bytes(int(r2_usage.get('bucket_bytes') or 0))} · "
                    f"{int(r2_usage.get('bucket_objects') or 0):,} 对象"
                )
            else:
                r2_metric = f"{r2_ok} 日 · 读取中\n正在读取 Bucket 用量"
        elif r2_usage.get("error"):
            r2_metric = f"{r2_ok} 日\n用量读取失败"
        elif not r2_usage:
            r2_metric = f"{r2_ok} 日\n用量尚未读取"
        else:
            r2_metric = (
                f"{r2_ok} 日 · 归档 "
                f"{human_bytes(int(r2_usage.get('archive_bytes') or 0))}\n"
                f"Bucket {human_bytes(int(r2_usage.get('bucket_bytes') or 0))} · "
                f"{int(r2_usage.get('bucket_objects') or 0):,} 对象"
            )

        local_usage = self.usage.get("local") or {}
        if "local" in self._usage_refreshing:
            if local_usage and not local_usage.get("error"):
                local_metric = (
                    f"{local_ok} 日 · 刷新中\n"
                    f"上次已用 {human_bytes(int(local_usage.get('used_bytes') or 0))} / "
                    f"总计 {human_bytes(int(local_usage.get('total_bytes') or 0))}"
                )
            else:
                local_metric = f"{local_ok} 日 · 读取中\n正在读取磁盘容量"
        elif local_usage.get("error"):
            local_metric = f"{local_ok} 日\n磁盘容量读取失败"
        elif not local_usage:
            local_metric = f"{local_ok} 日\n磁盘容量尚未读取"
        else:
            local_metric = (
                f"{local_ok} 日 · 已用 "
                f"{human_bytes(int(local_usage.get('used_bytes') or 0))}\n"
                f"总计 {human_bytes(int(local_usage.get('total_bytes') or 0))} · "
                f"剩余 {human_bytes(int(local_usage.get('free_bytes') or 0))}"
            )

        self.metric_vars["drive"].set(drive_metric)
        self.metric_vars["r2"].set(r2_metric)
        self.metric_vars["local"].set(local_metric)

    def _render_row(self, row: ArchiveDayStatus) -> None:
        match_text = (
            "一致"
            if row.replicas_match is True
            else "不一致"
            if row.replicas_match is False
            else "已核准清理"
            if row.drive.state == "cleaned" and row.r2.state == "cleaned"
            else "读取中"
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
            self.table.item(row.archive_date, values=values, tags=(tag,))
        else:
            self.table.insert(
                "", "end", iid=row.archive_date, values=values, tags=(tag,)
            )

    def _handle_scan_rows(self, rows: list[ArchiveDayStatus]) -> None:
        selected = set(self.table.selection())
        current = set(self.table.get_children())
        incoming = {row.archive_date for row in rows}
        for archive_date in current - incoming:
            self.table.delete(archive_date)
        self.rows = {row.archive_date: row for row in rows}
        for index, row in enumerate(rows):
            self._render_row(row)
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
            else f"{self.config_value.display_name}：双云端清单已更新"
        )
        verified = sum(row.replicas_match is True for row in rows)
        self.progress_detail_var.set(
            f"读取完成：{len(rows)} 个日期 · {verified} 个双副本一致"
        )

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
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
                    self._handle_scan_complete(payload)
                elif event == "scan_error":
                    self._usage_refreshing.clear()
                    self._scan_progress_keys.clear()
                    self._render_metrics()
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self._set_busy(False, "双云端清单读取失败")
                    messagebox.showerror(
                        "清单读取失败", str(payload), parent=self
                    )
                elif event == "download_batch":
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
                            "\n\n注意：\n" + "\n".join(result.warnings)
                            if result.warnings
                            else ""
                        )
                        messagebox.showinfo(
                            "归档完成",
                            f"{result.archive_date}\n"
                            f"{completion}\n"
                            f"来源：{replica_label(result.replica)}\n"
                            f"{result.object_count} 个对象 / {result.row_count:,} 行\n"
                            f"本次下载 {human_bytes(result.bytes_downloaded)}"
                            f"{warning_text}",
                            parent=self,
                        )
                    elif requested_count == 1:
                        archive_date = batch.requested_dates[0]
                        messagebox.showerror(
                            "下载失败",
                            f"{archive_date}\n{batch.failures[archive_date]}",
                            parent=self,
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
                            messagebox.showwarning(
                                "批量下载完成但有失败",
                                summary,
                                parent=self,
                            )
                        else:
                            messagebox.showinfo(
                                "批量下载完成",
                                summary,
                                parent=self,
                            )
                    self.refresh()
                elif event == "verify":
                    self._set_busy(False, f"{payload['archive_date']} 校验通过")
                    self.refresh()
                elif event == "error":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self._set_busy(False, "操作失败")
                    messagebox.showerror("操作失败", str(payload), parent=self)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

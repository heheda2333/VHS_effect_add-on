#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图形界面。

提供与命令行等价的全部处理能力，采用可视化控件调节参数，
适合不熟悉命令行的使用场景。

启动方式::

    python gui.py

界面上的各项设置会自动保存到 ``_gui_settings.json``，下次启动时沿用。
"""

from __future__ import annotations

import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from typing import List, Optional, Tuple

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

LIBS_DIR = os.path.join(_ROOT, "libs")
if os.path.isdir(LIBS_DIR) and LIBS_DIR not in sys.path:
    sys.path.insert(0, LIBS_DIR)

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from vhs_lib import vhs_video
from vhs_lib.vhs_effects import (
    PRESETS,
    VHSParams,
    describe_time,
    parse_hex_color,
    parse_start_time,
    resolve_preset,
)
from vhs_lib.vhs_task import (
    Interrupted,
    do_image,
    do_video,
    is_interrupt,
    mirror_out_path,
    scan_tree,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
APP_TITLE = "VHS 效果处理工具"
SETTINGS_PATH = os.path.join(_ROOT, "_gui_settings.json")
INPUT_DIR = os.path.join(_ROOT, "input")
OUTPUT_DIR = os.path.join(_ROOT, "output")

PRESET_CHOICES = [
    ("light", "轻微 —— 仅带一点录像带质感"),
    ("classic", "经典 —— 各项表现均衡（推荐）"),
    ("heavy", "重度 —— 噪点、抖动、串色明显"),
    ("worn", "破损 —— 模拟严重磨损的旧带子"),
    ("clean_tv", "电视 —— 干净带子在老式显像管电视上播放"),
]
PRESET_LABELS = {key: label for key, label in PRESET_CHOICES}
PRESET_KEYS = [key for key, _ in PRESET_CHOICES]

TIMESTAMP_MODE_CHOICES = [
    ("auto", "自动（照片固定 / 视频逐秒递增）"),
    ("fixed", "固定（始终显示同一时间）"),
    ("advance", "递增（一律按秒推进）"),
]

ENCODER_CHOICES = [
    ("auto", "自动（优先硬件编码器）"),
    ("cpu", "CPU（libx264）"),
    ("gpu", "硬件（自动选择）"),
    ("nvenc", "NVIDIA NVENC"),
    ("qsv", "Intel Quick Sync"),
    ("amf", "AMD AMF"),
]

# 可调效果参数：(参数名, 界面标签, 最小值, 最大值, 提示)
EFFECT_SLIDERS = [
    ("chroma_bleed", "色度串色", 0.0, 2.0, "色度横向拖尾与红蓝色边，VHS 最显著的特征"),
    ("chroma_subsample", "色度降采样", 0.0, 1.0, "色度带宽不足形成的块状色斑"),
    ("luma_noise", "亮度噪点", 0.0, 2.0, "整体颗粒感"),
    ("chroma_noise", "色度噪点", 0.0, 2.0, "彩色雪花"),
    ("edge_noise", "脏边与污渍", 0.0, 2.0, "低频污渍与边缘噪点"),
    ("scanlines", "扫描线", 0.0, 2.0, "显像管扫描线强度"),
    ("vignette", "暗角", 0.0, 2.0, "画面四周压暗程度"),
    ("jitter", "画面抖动", 0.0, 2.0, "整幅画面的水平抖动"),
    ("line_jitter", "逐行抖动", 0.0, 2.0, "每行独立的水平错位，形成撕裂感"),
    ("vertical_roll", "垂直滚动", 0.0, 2.0, "画面上下缓慢漂移"),
    ("head_switch", "磁头切换噪声带", 0.0, 2.0, "画面底部的固定噪声带"),
    ("tracking_band", "追踪失真带", 0.0, 2.0, "缓慢上移的失真区域"),
    ("dropout", "掉磁横线", 0.0, 2.0, "随机出现的亮线或暗线"),
    ("softness", "柔化", 0.0, 2.0, "带宽不足造成的画面发虚"),
    ("bloom", "高光泛光", 0.0, 2.0, "亮部向外晕染"),
    ("sharpen", "边缘增强", 0.0, 2.0, "轮廓强化，会同时放大噪点"),
    ("saturation", "饱和度", 0.0, 2.0, "1.0 为原样"),
    ("contrast", "对比度", 0.5, 1.8, "1.0 为原样"),
    ("brightness", "亮度", 0.5, 1.6, "1.0 为原样"),
]

OSD_SLIDERS = [
    ("osd_scale", "文字与图标大小", 0.4, 3.0, "整体缩放 OSD 元素"),
    ("osd_opacity", "文字不透明度", 0.0, 1.0, "OSD 叠加强度"),
    ("osd_glitch", "文字抖动", 0.0, 2.0, "文字位置的随机偏移"),
    ("osd_ghost", "文字色差重影", 0.0, 1.0, "文字边缘的红蓝重影"),
    ("osd_rec_blink", "REC 闪烁频率", 0.0, 4.0, "单位赫兹，0 为常亮（照片始终常亮）"),
]

# 以整型保存的参数（界面滑块为浮点，保存时转换）
INT_PARAMS = {"seed", "max_dim", "chroma_bleed_offset"}


def human_size(num_bytes: float) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return "%dB" % int(value) if unit == "B" else "%.1f%s" % (value, unit)
        value /= 1024.0
    return "%.1fGB" % value


def human_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "%.1f 秒" % seconds
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return "%d 分 %02d 秒" % (minutes, sec)
    hours, minutes = divmod(minutes, 60)
    return "%d 时 %02d 分" % (hours, minutes)


def open_in_file_manager(path: str) -> None:
    """在系统文件管理器中打开目录。"""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)                        # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


class App:
    """主窗口。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.param_vars = {}
        self.start_time_value: Optional[datetime.datetime] = None
        self.result_dirs: List[str] = []
        self._settings = self._load_settings()

        root.title("%s  v%s" % (APP_TITLE, "2.0"))
        root.geometry("980x760")
        root.minsize(880, 660)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_toolbar()
        self._build_body()
        self._apply_settings()
        self._refresh_time_preview()
        self.root.after(120, self._drain_events)

    # -- 设置持久化 --------------------------------------------------------
    def _load_settings(self) -> dict:
        if not os.path.exists(SETTINGS_PATH):
            return {}
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as handle:
                return json.load(handle) or {}
        except Exception:
            return {}

    def _save_settings(self) -> None:
        data = {
            "input_dir": self.input_var.get(),
            "output_dir": self.output_var.get(),
            "preset": self.preset_var.get(),
            "strength": self.strength_var.get(),
            "params": {name: var.get() for name, var in self.param_vars.items()},
            "time_text": self.time_var.get(),
            "timestamp_mode": self.timestamp_mode_var.get(),
            "timestamp_speed": self.timestamp_speed_var.get(),
            "encoder": self.encoder_var.get(),
            "target_height": self.height_var.get(),
            "crf": self.crf_var.get(),
            "encode_preset": self.encode_preset_var.get(),
            "image_max_dim": self.max_dim_var.get(),
            "quality": self.quality_var.get(),
            "keep_audio": self.keep_audio_var.get(),
            "process_images": self.process_images_var.get(),
            "process_videos": self.process_videos_var.get(),
            "add_suffix": self.suffix_var.get(),
            "rec_color": self.rec_color_var.get(),
            "seed": self.seed_var.get(),
        }
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def _apply_settings(self) -> None:
        saved = self._settings
        if saved.get("input_dir"):
            self.input_var.set(saved["input_dir"])
        if saved.get("output_dir"):
            self.output_var.set(saved["output_dir"])

        preset = saved.get("preset")
        if preset in PRESET_KEYS:
            self.preset_var.set(preset)
        for name, value in (saved.get("params") or {}).items():
            if name in self.param_vars:
                try:
                    self.param_vars[name].set(float(value))
                except (TypeError, ValueError):
                    pass

        if saved.get("time_text"):
            self.time_var.set(saved["time_text"])
        if saved.get("timestamp_mode") in dict(TIMESTAMP_MODE_CHOICES):
            self.timestamp_mode_var.set(saved["timestamp_mode"])
        if saved.get("timestamp_speed") is not None:
            self.timestamp_speed_var.set(saved["timestamp_speed"])
        if saved.get("encoder") in dict(ENCODER_CHOICES):
            self.encoder_var.set(saved["encoder"])
        if saved.get("target_height"):
            self.height_var.set(str(saved["target_height"]))
        if saved.get("crf") is not None:
            self.crf_var.set(int(saved["crf"]))
        if saved.get("encode_preset"):
            self.encode_preset_var.set(saved["encode_preset"])
        if saved.get("image_max_dim"):
            self.max_dim_var.set(str(saved["image_max_dim"]))
        if saved.get("quality") is not None:
            self.quality_var.set(int(saved["quality"]))
        if saved.get("rec_color"):
            self.rec_color_var.set(saved["rec_color"])
        if saved.get("seed") is not None:
            self.seed_var.set(int(saved["seed"]))
        for key, var in (("keep_audio", self.keep_audio_var),
                         ("process_images", self.process_images_var),
                         ("process_videos", self.process_videos_var),
                         ("add_suffix", self.suffix_var)):
            if saved.get(key) is not None:
                var.set(bool(saved[key]))

    # -- 界面构建 ----------------------------------------------------------
    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill="x")

        ttk.Label(bar, text="输入目录").pack(side="left")
        self.input_var = tk.StringVar(value=INPUT_DIR)
        ttk.Entry(bar, textvariable=self.input_var, width=42).pack(side="left", padx=(6, 4))
        ttk.Button(bar, text="浏览…", width=7,
                   command=self._choose_input).pack(side="left")

        ttk.Label(bar, text="输出目录").pack(side="left", padx=(14, 0))
        self.output_var = tk.StringVar(value=OUTPUT_DIR)
        ttk.Entry(bar, textvariable=self.output_var, width=42).pack(side="left", padx=(6, 4))
        ttk.Button(bar, text="浏览…", width=7,
                   command=self._choose_output).pack(side="left")

    def _build_body(self) -> None:
        outer = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        outer.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)

        self._build_effect_tab()
        self._build_output_tab()
        self._build_time_tab()
        self._build_osd_tab()
        self._build_log_tab()

        # 底部操作区
        bottom = ttk.Frame(outer, padding=(0, 8, 0, 0))
        bottom.pack(fill="x")

        self.start_button = ttk.Button(bottom, text="开始处理", width=14,
                                       command=self._on_start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(bottom, text="中止", width=10,
                                      command=self._on_stop, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        ttk.Button(bottom, text="预览单张", width=12,
                   command=self._on_preview).pack(side="left", padx=(6, 0))
        ttk.Button(bottom, text="打开输出目录", width=14,
                   command=lambda: open_in_file_manager(self.output_var.get())
                   ).pack(side="left", padx=(6, 0))

        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=1000)
        self.progress.pack(side="left", fill="x", expand=True, padx=(14, 8))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom, textvariable=self.status_var, width=26,
                  anchor="e").pack(side="left")

    def _make_slider(self, parent, key: str, label: str, low: float, high: float,
                     hint: str, default: float, row: int) -> None:
        ttk.Label(parent, text=label, width=16, anchor="w").grid(
            row=row, column=0, sticky="w", pady=2)
        var = tk.DoubleVar(value=default)
        scale = ttk.Scale(parent, from_=low, to=high, variable=var,
                          orient="horizontal", length=380)
        scale.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=2)
        value_label = ttk.Label(parent, width=6, anchor="e")

        def on_change(_value=None, v=var, lbl=value_label):
            try:
                lbl.configure(text="%.2f" % float(v.get()))
            except Exception:
                pass

        var.trace_add("write", lambda *_: on_change())
        on_change()
        value_label.grid(row=row, column=2, sticky="e")
        ttk.Label(parent, text=hint, foreground="#666666").grid(
            row=row, column=3, sticky="w", padx=(10, 0), pady=2)
        self.param_vars[key] = var

    def _build_effect_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="效果强度")

        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="预设").pack(side="left")
        self.preset_var = tk.StringVar(value="classic")
        combo = ttk.Combobox(top, textvariable=self.preset_var, state="readonly",
                             width=38, values=[label for _, label in PRESET_CHOICES])
        combo.pack(side="left", padx=(6, 16))
        combo.bind("<<ComboboxSelected>>", lambda _e: self._on_preset_changed())
        ttk.Label(top, text="整体强度").pack(side="left")
        self.strength_var = tk.DoubleVar(value=1.0)
        ttk.Scale(top, from_=0.0, to=2.5, variable=self.strength_var,
                  orient="horizontal", length=180).pack(side="left", padx=(6, 4))
        self.strength_label = ttk.Label(top, width=5)
        self.strength_label.pack(side="left")
        self.strength_var.trace_add("write", lambda *_: self.strength_label.configure(
            text="%.2f" % float(self.strength_var.get())))
        ttk.Button(top, text="按预设重置各项参数", command=self._reset_params).pack(
            side="left", padx=(16, 0))

        # 可滚动区域
        canvas = tk.Canvas(tab, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        holder = ttk.Frame(canvas)
        holder.bind("<Configure>",
                    lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=holder, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        holder.columnconfigure(1, weight=1)
        defaults = resolve_preset("classic")
        for row, (key, label, low, high, hint) in enumerate(EFFECT_SLIDERS):
            self._make_slider(holder, key, label, low, high, hint,
                              float(getattr(defaults, key)), row)
        # 额外提供两个非滑块参数
        extra_row = len(EFFECT_SLIDERS) + 1
        ttk.Label(holder, text="随机种子", width=16, anchor="w").grid(
            row=extra_row, column=0, sticky="w", pady=2)
        self.seed_var = tk.IntVar(value=20240517)
        ttk.Spinbox(holder, from_=0, to=2 ** 31 - 1, textvariable=self.seed_var,
                    width=12).grid(row=extra_row, column=1, sticky="w", pady=2)
        ttk.Label(holder, text="相同种子可得到完全一致的结果",
                  foreground="#666666").grid(row=extra_row, column=3, sticky="w",
                                             padx=(10, 0))

    def _build_output_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="输出设置")

        images = ttk.LabelFrame(tab, text="图片", padding=10)
        images.pack(fill="x", pady=(0, 10))
        ttk.Label(images, text="处理最长边").grid(row=0, column=0, sticky="w")
        self.max_dim_var = tk.StringVar(value="1920")
        ttk.Combobox(images, textvariable=self.max_dim_var, width=10, values=[
            "原始尺寸", "1280", "1920", "2560", "3840"]).grid(
            row=0, column=1, sticky="w", padx=(8, 20))
        ttk.Label(images, text="超出该值时先缩小处理，完成后还原原始分辨率",
                  foreground="#666666").grid(row=0, column=2, sticky="w")

        ttk.Label(images, text="JPEG 质量").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.quality_var = tk.IntVar(value=95)
        ttk.Scale(images, from_=50, to=100, variable=self.quality_var,
                  orient="horizontal", length=220).grid(
            row=1, column=1, sticky="w", padx=(8, 6), pady=(6, 0))
        self.quality_label = ttk.Label(images, width=4)
        self.quality_label.grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.quality_var.trace_add("write", lambda *_: self.quality_label.configure(
            text="%d" % int(self.quality_var.get())))

        videos = ttk.LabelFrame(tab, text="视频", padding=10)
        videos.pack(fill="x", pady=(0, 10))
        ttk.Label(videos, text="输出最大高度").grid(row=0, column=0, sticky="w")
        self.height_var = tk.StringVar(value="1080")
        ttk.Combobox(videos, textvariable=self.height_var, width=10,
                     values=["480", "720", "1080", "1440", "2160"]).grid(
            row=0, column=1, sticky="w", padx=(8, 20))
        ttk.Label(videos, text="降低分辨率可显著缩短处理时间",
                  foreground="#666666").grid(row=0, column=2, sticky="w")

        ttk.Label(videos, text="质量参数 CRF").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.crf_var = tk.IntVar(value=18)
        ttk.Scale(videos, from_=10, to=32, variable=self.crf_var,
                  orient="horizontal", length=220).grid(
            row=1, column=1, sticky="w", padx=(8, 6), pady=(6, 0))
        self.crf_label = ttk.Label(videos, width=4)
        self.crf_label.grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.crf_var.trace_add("write", lambda *_: self.crf_label.configure(
            text="%d" % int(self.crf_var.get())))
        ttk.Label(videos, text="数值越小质量越高、文件越大",
                  foreground="#666666").grid(row=1, column=3, sticky="w", padx=(10, 0),
                                             pady=(6, 0))

        ttk.Label(videos, text="编码速度预设").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.encode_preset_var = tk.StringVar(value="medium")
        ttk.Combobox(videos, textvariable=self.encode_preset_var, width=12,
                     state="readonly",
                     values=["ultrafast", "veryfast", "fast", "medium",
                             "slow", "slower"]).grid(
            row=2, column=1, sticky="w", padx=(8, 20), pady=(6, 0))

        ttk.Label(videos, text="编码器").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.encoder_var = tk.StringVar(value="auto")
        ttk.Combobox(videos, textvariable=self.encoder_var, width=30,
                     state="readonly",
                     values=[label for _, label in ENCODER_CHOICES]).grid(
            row=3, column=1, columnspan=2, sticky="w", padx=(8, 20), pady=(6, 0))

        self.keep_audio_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(videos, text="保留音轨", variable=self.keep_audio_var).grid(
            row=4, column=1, sticky="w", padx=(8, 0), pady=(6, 0))

        scope = ttk.LabelFrame(tab, text="处理范围", padding=10)
        scope.pack(fill="x")
        self.process_images_var = tk.BooleanVar(value=True)
        self.process_videos_var = tk.BooleanVar(value=True)
        self.suffix_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(scope, text="处理图片", variable=self.process_images_var).grid(
            row=0, column=0, sticky="w")
        ttk.Checkbutton(scope, text="处理视频", variable=self.process_videos_var).grid(
            row=0, column=1, sticky="w", padx=(20, 0))
        ttk.Checkbutton(scope, text="输出文件名追加 _vhs 后缀（默认沿用原名）",
                        variable=self.suffix_var).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_time_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="日期时间叠加")

        ttk.Label(tab, text="起始日期时间").grid(row=0, column=0, sticky="w")
        self.time_var = tk.StringVar(value="1994-06-11 12:00:00")
        entry = ttk.Entry(tab, textvariable=self.time_var, width=30)
        entry.grid(row=0, column=1, sticky="w", padx=(8, 8))
        entry.bind("<KeyRelease>", lambda _e: self._refresh_time_preview())
        ttk.Button(tab, text="设为当前时间",
                   command=self._set_time_now).grid(row=0, column=2, sticky="w")

        ttk.Label(tab, text="可识别多种写法，例如 1994-06-11 12:00:00、"
                            "2001/9/11 08:46、12:00:00、1994年6月11日 12点00分、"
                            "19940611120000、now",
                  foreground="#666666", wraplength=780, justify="left").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 10))

        self.time_preview_var = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.time_preview_var,
                  foreground="#0a6b0a").grid(row=2, column=0, columnspan=4,
                                             sticky="w", pady=(0, 12))

        ttk.Label(tab, text="推演方式").grid(row=3, column=0, sticky="w")
        self.timestamp_mode_var = tk.StringVar(value="auto")
        ttk.Combobox(tab, textvariable=self.timestamp_mode_var, width=34,
                     state="readonly",
                     values=[label for _, label in TIMESTAMP_MODE_CHOICES]).grid(
            row=3, column=1, columnspan=2, sticky="w", padx=(8, 0))

        ttk.Label(tab, text="时间流逝倍率").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.timestamp_speed_var = tk.DoubleVar(value=1.0)
        ttk.Scale(tab, from_=0.1, to=10.0, variable=self.timestamp_speed_var,
                  orient="horizontal", length=240).grid(
            row=4, column=1, sticky="w", padx=(8, 6), pady=(10, 0))
        self.speed_label = ttk.Label(tab, width=6)
        self.speed_label.grid(row=4, column=2, sticky="w", pady=(10, 0))
        self.timestamp_speed_var.trace_add("write", lambda *_: self.speed_label.configure(
            text="%.2fx" % float(self.timestamp_speed_var.get())))
        ttk.Label(tab, text="2.0 表示视频内时间流逝速度加倍",
                  foreground="#666666").grid(row=4, column=3, sticky="w",
                                             padx=(10, 0), pady=(10, 0))

        self.time_hint = ttk.Label(
            tab,
            text="照片：直接印制起始时间。\n"
                 "视频：自起始时间开始，按秒递增。",
            foreground="#444444", justify="left")
        self.time_hint.grid(row=5, column=0, columnspan=4, sticky="w", pady=(16, 0))

    def _build_osd_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="叠加文字")

        switches = ttk.LabelFrame(tab, text="显示内容", padding=10)
        switches.pack(fill="x", pady=(0, 10))
        defaults = resolve_preset("classic")
        self.osd_vars = {}
        for column, (key, label) in enumerate((
                ("osd", "总开关"),
                ("osd_rec", "左上角 ● REC"),
                ("osd_play", "右上角 ▶ PLAY / SP"),
                ("osd_timestamp", "右下角日期时间"))):
            var = tk.BooleanVar(value=bool(getattr(defaults, key)))
            self.osd_vars[key] = var
            ttk.Checkbutton(switches, text=label, variable=var).grid(
                row=0, column=column, sticky="w", padx=(0, 18))

        ttk.Label(switches, text="REC 圆点颜色").grid(row=1, column=0, sticky="w",
                                                      pady=(8, 0))
        self.rec_color_var = tk.StringVar(value="#FF2E2E")
        ttk.Entry(switches, textvariable=self.rec_color_var, width=12).grid(
            row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(switches, text="形如 #FF2E2E 或 255,46,46",
                  foreground="#666666").grid(row=1, column=2, columnspan=2,
                                             sticky="w", padx=(8, 0), pady=(8, 0))

        sliders = ttk.Frame(tab)
        sliders.pack(fill="x")
        sliders.columnconfigure(1, weight=1)
        for row, (key, label, low, high, hint) in enumerate(OSD_SLIDERS):
            self._make_slider(sliders, key, label, low, high, hint,
                              float(getattr(defaults, key)), row)

        ttk.Label(tab, text="说明：照片始终显示常亮的 REC 圆点；"
                            "视频中圆点按设定频率闪烁。",
                  foreground="#666666").pack(anchor="w", pady=(12, 0))

    def _build_log_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="处理日志")
        self.log_text = tk.Text(tab, wrap="none", height=18, state="disabled",
                                font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(tab, orient="vertical",
                                  command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # -- 交互 --------------------------------------------------------------
    def _choose_input(self) -> None:
        path = filedialog.askdirectory(title="选择输入目录",
                                       initialdir=self.input_var.get() or _ROOT)
        if path:
            self.input_var.set(os.path.normpath(path))

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录",
                                       initialdir=self.output_var.get() or _ROOT)
        if path:
            self.output_var.set(os.path.normpath(path))

    def _on_preset_changed(self) -> None:
        self._reset_params()

    def _reset_params(self) -> None:
        """把各滑块恢复为当前预设的取值。"""
        preset_key = self._current_preset_key()
        params = resolve_preset(preset_key)
        for name, var in self.param_vars.items():
            if hasattr(params, name):
                var.set(float(getattr(params, name)))
        for key, var in self.osd_vars.items():
            var.set(bool(getattr(params, key)))
        self.rec_color_var.set(params.osd_rec_color)
        self.seed_var.set(int(params.seed))
        self.strength_var.set(1.0)

    def _current_preset_key(self) -> str:
        label = self.preset_var.get()
        for key, text in PRESET_CHOICES:
            if text == label:
                return key
        return "classic"

    def _set_time_now(self) -> None:
        self.time_var.set(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def _refresh_time_preview(self) -> None:
        raw = self.time_var.get().strip()
        try:
            moment = parse_start_time(raw)
            self.start_time_value = moment
            self.time_preview_var.set("解析结果：%s" % describe_time(moment))
        except ValueError as exc:
            self.start_time_value = None
            self.time_preview_var.set("无法识别：%s" % str(exc).splitlines()[0])

    # -- 参数收集 ----------------------------------------------------------
    def _collect_params(self) -> VHSParams:
        """把界面上的控件取值汇总为参数对象。"""
        params = resolve_preset(self._current_preset_key())

        overrides = {}
        for name, var in self.param_vars.items():
            try:
                overrides[name] = float(var.get())
            except Exception:
                continue
        allowed = set(VHSParams.__dataclass_fields__.keys())
        overrides = {k: v for k, v in overrides.items() if k in allowed}

        for key, var in self.osd_vars.items():
            overrides[key] = bool(var.get())

        overrides["osd_rec_color"] = self.rec_color_var.get().strip() or "#FF2E2E"
        overrides["seed"] = int(self.seed_var.get())
        overrides["timestamp_mode"] = self._current_timestamp_mode()
        overrides["timestamp_speed"] = float(self.timestamp_speed_var.get())
        overrides["timestamp_start"] = self.time_var.get().strip()

        max_dim_text = self.max_dim_var.get().strip()
        overrides["max_dim"] = 0 if max_dim_text in ("原始尺寸", "0", "") else int(
            float(max_dim_text))

        params = params.merged(**overrides)
        return params.scaled(float(self.strength_var.get()))

    def _current_timestamp_mode(self) -> str:
        label = self.timestamp_mode_var.get()
        for key, text in TIMESTAMP_MODE_CHOICES:
            if text == label:
                return key
        return "auto"

    def _current_encoder(self) -> str:
        label = self.encoder_var.get()
        for key, text in ENCODER_CHOICES:
            if text == label:
                return key
        return "auto"

    def _validate(self) -> Optional[Tuple[str, str]]:
        """校验输入，返回 (输入目录, 输出目录)；不合法时返回 None。"""
        in_dir = self.input_var.get().strip()
        out_dir = self.output_var.get().strip()
        if not in_dir or not os.path.isdir(in_dir):
            messagebox.showerror(APP_TITLE, "输入目录不存在，请重新选择。")
            return None
        if not out_dir:
            messagebox.showerror(APP_TITLE, "请先选择输出目录。")
            return None
        try:
            if os.path.commonpath([os.path.abspath(in_dir),
                                   os.path.abspath(out_dir)]) == os.path.abspath(in_dir):
                messagebox.showerror(APP_TITLE, "输出目录不能位于输入目录之内。")
                return None
        except ValueError:
            pass
        if not self.process_images_var.get() and not self.process_videos_var.get():
            messagebox.showerror(APP_TITLE, "请至少勾选图片或视频中的一项。")
            return None
        try:
            self._collect_params()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "参数不合法：%s" % exc)
            return None
        if self.osd_vars["osd_timestamp"].get():
            self._refresh_time_preview()
            if self.start_time_value is None:
                messagebox.showerror(APP_TITLE, "起始日期时间无法识别，请检查。")
                return None
        return in_dir, out_dir

    # -- 日志与状态 --------------------------------------------------------
    def log(self, message: str) -> None:
        self.events.put(("log", message))

    def set_progress(self, ratio: float) -> None:
        self.events.put(("progress", ratio))

    def set_status(self, text: str) -> None:
        self.events.put(("status", text))

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -- 处理流程 ----------------------------------------------------------
    def _on_start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        validated = self._validate()
        if validated is None:
            return
        in_dir, out_dir = validated

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.result_dirs = []
        self.stop_event.clear()
        self._save_settings()

        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.configure(value=0)
        self.notebook.select(4)
        self.set_status("正在准备…")

        self.worker = threading.Thread(target=self._worker_run,
                                       args=(in_dir, out_dir), daemon=True)
        self.worker.start()

    def _on_stop(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.stop_event.set()
            self.set_status("正在中止…")
            self.log("已请求中止。当前文件处理完成后停止。")

    def _on_preview(self) -> None:
        """处理输入目录中的第一张图片，便于快速确认效果。"""
        in_dir = self.input_var.get().strip()
        if not os.path.isdir(in_dir):
            messagebox.showerror(APP_TITLE, "输入目录不存在，请重新选择。")
            return
        images, _videos, _skipped = scan_tree(in_dir)
        if not images:
            messagebox.showinfo(APP_TITLE, "输入目录中没有可预览的图片。")
            return
        try:
            params = self._collect_params()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "参数不合法：%s" % exc)
            return

        out_dir = os.path.join(_ROOT, "_preview")
        os.makedirs(out_dir, exist_ok=True)
        source = images[0]
        target = os.path.join(out_dir, os.path.splitext(os.path.basename(source))[0]
                              + "_preview.jpg")
        self.set_status("正在生成预览…")
        self.log("生成预览：%s" % os.path.basename(source))

        def run():
            try:
                start_time = (self.start_time_value
                              if self.osd_vars["osd_timestamp"].get() else None)
                info = do_image(source, target, params,
                                quality=int(self.quality_var.get()),
                                start_time=start_time)
                self.events.put(("log", "预览已生成：%s（%dx%d，%.1f KB）"
                                 % (target, info["src_size"][0], info["src_size"][1],
                                    info["out_bytes"] / 1024.0)))
                open_in_file_manager(out_dir)
            except Exception as exc:
                self.events.put(("log", "预览失败：%s" % exc))
            finally:
                self.events.put(("status", "就绪"))

        threading.Thread(target=run, daemon=True).start()

    def _worker_run(self, in_dir: str, out_dir: str) -> None:
        """后台处理线程。

        界面刷新通过事件队列回到主线程，避免跨线程操作控件。
        """
        try:
            params = self._collect_params()
            start_time = (self.start_time_value
                          if self.osd_vars["osd_timestamp"].get() else None)
            images, videos, skipped = scan_tree(in_dir)
            if not self.process_images_var.get():
                skipped.extend(images)
                images = []
            if not self.process_videos_var.get():
                skipped.extend(videos)
                videos = []

            total_items = len(images) + len(videos)
            if total_items == 0:
                self.events.put(("log", "输入目录中没有可处理的文件。"))
                self.events.put(("done", (0, 0, True)))
                return

            self.events.put(("log", "输入目录：%s" % in_dir))
            self.events.put(("log", "输出目录：%s" % out_dir))
            self.events.put(("log", "预设：%s，整体强度 %.2f"
                             % (self.preset_var.get(), self.strength_var.get())))
            if start_time is not None:
                self.events.put(("log", "起始日期时间：%s" % describe_time(start_time)))
            self.events.put(("log", "待处理：%d 张图片，%d 个视频"
                             % (len(images), len(videos))))
            if skipped:
                self.events.put(("log", "已跳过 %d 个不支持的文件" % len(skipped)))
            self.events.put(("log", "-" * 66))

            os.makedirs(out_dir, exist_ok=True)
            quality = int(self.quality_var.get())
            height_text = self.height_var.get().strip()
            target_height = int(float(height_text)) if height_text else None
            crf = int(self.crf_var.get())
            encoder = self._current_encoder()

            succeeded = 0
            failed = 0
            stopped = False
            started = time.time()

            for index, source in enumerate(images, 1):
                if self.stop_event.is_set():
                    stopped = True
                    break
                target = mirror_out_path(source, in_dir, out_dir)
                label = os.path.relpath(target, out_dir)
                self.events.put(("status", "图片 %d/%d" % (index, len(images))))
                try:
                    info = do_image(source, target, params, quality=quality,
                                    start_time=start_time)
                    succeeded += 1
                    self.events.put(("log", "[图片 %d/%d] %s  %dx%d -> %dx%d  %s  %.2f 秒"
                                     % (index, len(images), label,
                                        info["src_size"][0], info["src_size"][1],
                                        info["work_size"][0], info["work_size"][1],
                                        human_size(info["out_bytes"]),
                                        info["seconds"])))
                    self.result_dirs.append(os.path.dirname(target))
                except BaseException as exc:
                    if is_interrupt(exc):
                        stopped = True
                        break
                    failed += 1
                    self.events.put(("log", "[图片 %d/%d] %s 处理失败：%s"
                                     % (index, len(images), label, exc)))
                self.events.put(("progress", index / float(total_items)))

            if not stopped:
                for index, source in enumerate(videos, 1):
                    if self.stop_event.is_set():
                        stopped = True
                        break
                    target = mirror_out_path(
                        source, in_dir, out_dir,
                        suffix="_vhs" if self.suffix_var.get() else "")
                    label = os.path.relpath(target, out_dir)
                    self.events.put(("status", "视频 %d/%d" % (index, len(videos))))
                    self.events.put(("log", "[视频 %d/%d] %s"
                                     % (index, len(videos), label)))
                    done_images = len(images)

                    def on_progress(done, total, _elapsed,
                                    _index=index, _base=done_images):
                        if total:
                            ratio = (_base + min(1.0, done / float(total))) / total_items
                        else:
                            ratio = (_base + 0.5) / total_items
                        self.events.put(("progress", ratio))

                    try:
                        info = do_video(
                            source, target, params,
                            target_height=target_height, crf=crf,
                            preset=self.encode_preset_var.get(),
                            keep_audio=bool(self.keep_audio_var.get()),
                            progress=on_progress, encoder=encoder,
                            start_time=start_time)
                        succeeded += 1
                        self.events.put(("log",
                                         "    完成 %dx%d -> %dx%d，%d 帧，%s%s，耗时 %s"
                                         % (info["src_size"][0], info["src_size"][1],
                                            info["out_size"][0], info["out_size"][1],
                                            info["frames"], human_size(info["out_bytes"]),
                                            "，含音轨" if info["audio"] else "，无音轨",
                                            human_time(info["seconds"]))))
                        self.result_dirs.append(os.path.dirname(target))
                    except Interrupted:
                        stopped = True
                        self.events.put(("log", "    已中止，未完成的文件已删除。"))
                        break
                    except BaseException as exc:
                        if is_interrupt(exc):
                            stopped = True
                            break
                        failed += 1
                        self.events.put(("log", "    处理失败：%s" % exc))
                    self.events.put(("progress",
                                     (done_images + index) / float(total_items)))

            elapsed = time.time() - started
            self.events.put(("log", "-" * 66))
            if stopped:
                self.events.put(("log", "已中止。成功 %d 个，失败 %d 个，耗时 %s"
                                 % (succeeded, failed, human_time(elapsed))))
            else:
                self.events.put(("log", "处理完成。成功 %d 个，失败 %d 个，耗时 %s"
                                 % (succeeded, failed, human_time(elapsed))))
            self.events.put(("done", (succeeded, failed, not stopped)))

        except Exception as exc:
            self.events.put(("log", "发生未预期的错误：%s" % exc))
            self.events.put(("log", traceback.format_exc()))
            self.events.put(("done", (0, 1, False)))

    # -- 事件循环 ----------------------------------------------------------
    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    self.progress.configure(value=max(0.0, min(1.0, payload)) * 1000)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "done":
                    succeeded, failed, normal = payload
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.progress.configure(value=1000 if normal else self.progress["value"])
                    self.status_var.set("完成" if normal else "已中止")
                    self._announce_results(succeeded, failed, normal)
        except queue.Empty:
            pass
        self.root.after(120, self._drain_events)

    def _announce_results(self, succeeded: int, failed: int, normal: bool) -> None:
        if succeeded == 0 and failed == 0:
            return
        unique_dirs = []
        for path in self.result_dirs:
            if path and path not in unique_dirs:
                unique_dirs.append(path)

        lines = ["成功 %d 个，失败 %d 个。" % (succeeded, failed)]
        if unique_dirs:
            lines.append("")
            lines.append("结果所在的文件夹：")
            for path in unique_dirs[:8]:
                lines.append("  " + path)
            if len(unique_dirs) > 8:
                lines.append("  …… 其余 %d 个" % (len(unique_dirs) - 8))
        lines.append("")
        lines.append("是否立即打开输出目录？")

        if messagebox.askyesno(APP_TITLE, "\n".join(lines)):
            open_in_file_manager(self.output_var.get())

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno(APP_TITLE, "正在处理，确定要退出吗？"):
                return
            self.stop_event.set()
        self._save_settings()
        self.root.destroy()


def main() -> int:
    # 依赖缺失时先给出明确提示，避免在界面初始化阶段抛出难懂的异常
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as exc:
        print("缺少必需的依赖库：%s" % exc)
        print("请先运行 安装依赖.bat，或执行：python setup_deps.py")
        return 1

    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass

    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

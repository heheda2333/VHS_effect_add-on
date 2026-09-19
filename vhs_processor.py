#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""VHS 效果处理工具 —— 命令行入口。

将待处理的照片与视频放入 ``input`` 目录，处理结果写入 ``output`` 目录，
并在输出目录中重建与输入一致的文件夹结构。

处理流程、目录遍历与路径映射由 :mod:`vhs_lib.vhs_task` 提供，
命令行与图形界面共用同一套实现。

用法速查::

    # 处理 input 目录中的全部图片与视频
    python vhs_processor.py

    # 指定预设与整体强度
    python vhs_processor.py --preset heavy
    python vhs_processor.py --preset classic --strength 1.4

    # 自定义右下角日期时间（自动识别多种写法）
    python vhs_processor.py --time "2001-09-11 08:46:00"

    # 处理单个文件并指定输出路径
    python vhs_processor.py image "input/a.jpg" -o "output/a.jpg"
    python vhs_processor.py video "input/a.mp4" -o "output/a.mp4"

    # 查看预设与当前参数
    python vhs_processor.py presets
    python vhs_processor.py show --preset heavy

    # 使用内置素材进行自检
    python selftest.py

处理期间可随时按 Ctrl+C 中止：不保存进度，已完成的文件保留，
正在处理的半成品文件会被删除。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import signal
import sys
import time
import traceback
from dataclasses import asdict, fields
from typing import List, Optional, Tuple

# 将项目根目录加入模块搜索路径，使脚本可从任意工作目录启动
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from vhs_lib import vhs_video
from vhs_lib.vhs_effects import (
    IMAGE_EXTS,
    PRESETS,
    VIDEO_EXTS,
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
    unique_path,
)

__version__ = "2.0"

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
ROOT = _ROOT
INPUT_DIR = os.path.join(ROOT, "input")
OUTPUT_DIR = os.path.join(ROOT, "output")
MATERIAL_DIR = os.path.join(ROOT, "Material")
CONFIG_PATH = os.path.join(ROOT, "vhs_config.json")

BANNER = r"""
 __     __ _   _  ____    _____ ___  ___ _____
 \ \   / /| | | |/ ___|  |  ___/ _ \|  _ \_   _|
  \ \ / / | |_| |\___ \  | |_ | | | | |_) || |
   \ V /  |  _  | ___) | |  _|| |_| |  __/ | |
    \_/   |_| |_||____/  |_|   \___/|_|    |_|    v%s
""" % __version__


# ---------------------------------------------------------------------------
# 终端输出辅助
# ---------------------------------------------------------------------------
class Color:
    """终端 ANSI 配色。输出被重定向或设置了 NO_COLOR 时自动关闭。"""

    enabled = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    @staticmethod
    def _wrap(code: str, text: str) -> str:
        return text if not Color.enabled else "\x1b[%sm%s\x1b[0m" % (code, text)

    @staticmethod
    def dim(text: str) -> str:
        return Color._wrap("2", text)

    @staticmethod
    def bold(text: str) -> str:
        return Color._wrap("1", text)

    @staticmethod
    def red(text: str) -> str:
        return Color._wrap("31", text)

    @staticmethod
    def green(text: str) -> str:
        return Color._wrap("32", text)

    @staticmethod
    def yellow(text: str) -> str:
        return Color._wrap("33", text)

    @staticmethod
    def cyan(text: str) -> str:
        return Color._wrap("36", text)


def human_size(num_bytes: float) -> str:
    """把字节数格式化为便于阅读的字符串。"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return "%dB" % int(value) if unit == "B" else "%.1f%s" % (value, unit)
        value /= 1024.0
    return "%.1fGB" % value


def human_time(seconds: float) -> str:
    """把秒数格式化为便于阅读的字符串。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "%.1fs" % seconds
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return "%dm%02ds" % (minutes, sec)
    hours, minutes = divmod(minutes, 60)
    return "%dh%02dm" % (hours, minutes)


def clip_text(text: str, width: int) -> str:
    """按显示宽度截断文本，超出部分以省略号代替。"""
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "\u2026"


# ---------------------------------------------------------------------------
# 参数装配
# ---------------------------------------------------------------------------
def load_config() -> dict:
    """读取可选的配置文件 ``vhs_config.json``；不存在时返回空字典。"""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle) or {}
    except Exception as exc:
        print(Color.yellow("  配置文件无法解析，已忽略：%s" % exc))
        return {}


def _coerce(raw: str, current):
    """按当前值的类型把命令行字符串转换为合适的类型。"""
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on", "y", "t")
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(float(raw))
        except ValueError:
            return current
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            return current
    return raw


def build_params(args) -> VHSParams:
    """装配处理参数。

    优先级由低到高：预设 → 配置文件 → 命令行选项。
    """
    config = load_config()
    preset_name = getattr(args, "preset", None) or config.get("preset") or "classic"
    try:
        params = resolve_preset(preset_name)
    except KeyError as exc:
        print(Color.red("  %s" % exc))
        print("  已改用默认预设 classic")
        params = resolve_preset("classic")

    valid_fields = {field.name for field in fields(VHSParams)}

    config_params = config.get("params") or {}
    config_params = {k: v for k, v in config_params.items() if k in valid_fields}
    if config_params:
        params = params.merged(**config_params)

    for item in (getattr(args, "set", None) or []):
        if "=" not in item:
            print(Color.yellow("  已忽略无效的 --set %r，正确写法为 key=value" % item))
            continue
        key, raw = item.split("=", 1)
        key = key.strip()
        if key not in valid_fields:
            print(Color.yellow("  未知参数 %r，已忽略" % key))
            continue
        params = params.merged(**{key: _coerce(raw, getattr(params, key))})

    strength = getattr(args, "strength", None)
    if strength is None:
        strength = config.get("strength")
    if strength is not None:
        params = params.scaled(float(strength))

    if getattr(args, "no_osd", False):
        params = params.merged(osd=False)
    if getattr(args, "osd", False):
        params = params.merged(osd=True)
    if getattr(args, "no_timestamp", False):
        params = params.merged(osd_timestamp=False)

    # 时间戳相关选项
    if getattr(args, "timestamp_text", None):
        params = params.merged(osd_timestamp_text=args.timestamp_text, osd_timestamp=True)
    if getattr(args, "time", None):
        try:
            parse_start_time(args.time)
        except ValueError as exc:
            print(Color.red("  %s" % exc))
            raise SystemExit(2)
        params = params.merged(timestamp_start=args.time, osd_timestamp=True)
    if getattr(args, "time_offset", None) is not None:
        params = params.merged(timestamp_offset=float(args.time_offset))
    if getattr(args, "timestamp_mode", None):
        params = params.merged(timestamp_mode=args.timestamp_mode.strip().lower())
    if getattr(args, "timestamp_speed", None) is not None:
        params = params.merged(timestamp_speed=float(args.timestamp_speed))
    if getattr(args, "rec_color", None):
        params = params.merged(osd_rec_color=args.rec_color)
    if getattr(args, "rec_blink", None) is not None:
        params = params.merged(osd_rec_blink=float(args.rec_blink))

    if getattr(args, "seed", None) is not None:
        params = params.merged(seed=int(args.seed))

    return params


def build_start_time(args, params: VHSParams) -> Tuple[Optional[datetime.datetime], str]:
    """解析起始时间，返回 ``(datetime 或 None, 用于显示的说明文字)``。"""
    raw = getattr(args, "time", None) or params.timestamp_start
    try:
        moment = parse_start_time(raw)
    except ValueError as exc:
        print(Color.red("  %s" % exc))
        return None, "解析失败"
    moment += datetime.timedelta(seconds=float(params.timestamp_offset))
    return moment, describe_time(moment)


# ---------------------------------------------------------------------------
# 终端进度显示
# ---------------------------------------------------------------------------
def print_video_progress(done: int, total: int, started: float, name: str,
                         final: bool = False) -> None:
    """在单行内刷新视频处理进度。"""
    elapsed = max(1e-6, time.time() - started)
    speed = done / elapsed
    if total:
        percent = 100.0 * done / total
        remain = (total - done) / speed if speed > 0 else 0.0
        line = "    %-34s %6.1f%%  %5.1f fps  剩余 %s" % (
            clip_text(name, 34), percent, speed, human_time(remain))
    else:
        line = "    %-34s %6d 帧  %5.1f fps" % (clip_text(name, 34), done, speed)
    sys.stdout.write("\r" + line + ("\n" if final else ""))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 命令：批处理
# ---------------------------------------------------------------------------
def cmd_run(args) -> int:
    in_dir = os.path.abspath(args.input or INPUT_DIR)
    out_dir = os.path.abspath(args.output or OUTPUT_DIR)

    if not os.path.isdir(in_dir):
        print(Color.red("输入目录不存在：%s" % in_dir))
        return 2

    # 输出目录位于输入目录之内会导致重复扫描，直接拒绝
    try:
        if os.path.commonpath([in_dir, out_dir]) == in_dir:
            print(Color.red("输出目录不能位于输入目录之内："))
            print("  输入：%s" % in_dir)
            print("  输出：%s" % out_dir)
            return 2
    except ValueError:
        pass  # 不同盘符时 commonpath 会抛出异常，此时必然不嵌套

    params = build_params(args)
    start_time, time_text = build_start_time(args, params)
    images, videos, skipped = scan_tree(in_dir)

    if args.no_video:
        skipped.extend(videos)
        videos = []
    if args.no_image:
        skipped.extend(images)
        images = []

    if not images and not videos:
        print(Color.yellow("在 %s 中未找到可处理的图片或视频。" % in_dir))
        print(Color.dim("  支持的图片格式：%s" % " ".join(sorted(IMAGE_EXTS))))
        print(Color.dim("  支持的视频格式：%s" % " ".join(sorted(VIDEO_EXTS))))
        return 1

    os.makedirs(out_dir, exist_ok=True)

    print(BANNER)
    print("  输入目录：%s" % Color.cyan(in_dir))
    print("  输出目录：%s" % Color.cyan(out_dir))
    print("  预设：%s" % Color.bold(args.preset or load_config().get("preset") or "classic"))
    if params.osd_timestamp:
        print("  起始时间：%s%s" % (Color.bold(time_text),
                                   Color.dim("（照片印该时间，视频自该时间逐秒递增）")))
    else:
        print("  起始时间：%s" % Color.dim("时间戳已关闭"))
    if params.osd_timestamp and abs(float(params.timestamp_speed) - 1.0) > 1e-6:
        print("  时间倍率：%.3gx" % float(params.timestamp_speed))
    print("  待处理：%d 张图片，%d 个视频%s"
          % (len(images), len(videos),
             "，另有 %d 个文件被跳过" % len(skipped) if skipped else ""))
    print("  视频规格：最高 %sp，CRF %d%s"
          % (args.height, args.crf, "" if args.no_audio else "，保留音轨"))
    if videos and vhs_video is not None:
        encoder = vhs_video.resolve_encoder(args.encoder, crf=args.crf,
                                            preset=args.encode_preset)
        print("  编码器：%s" % encoder["label"])
        if encoder["is_gpu"]:
            print(Color.dim("          硬件编码仅降低编码阶段的 CPU 占用，"
                            "总耗时基本不变"))
    print()

    succeeded: List[str] = []
    failed: List[Tuple[str, BaseException]] = []
    overall_start = time.time()

    # ---- 图片 ----
    if images:
        print(Color.bold("[图片] 共 %d 张" % len(images)))
        try:
            for index, src in enumerate(images, 1):
                dst = mirror_out_path(src, in_dir, out_dir)
                rel = os.path.relpath(dst, out_dir)
                try:
                    info = do_image(src, dst, params, quality=args.quality,
                                    start_time=start_time)
                    succeeded.append(dst)
                    print("  [%d/%d] %s  %dx%d -> %dx%d  %s  %s"
                          % (index, len(images), clip_text(rel, 38),
                             info["src_size"][0], info["src_size"][1],
                             info["work_size"][0], info["work_size"][1],
                             human_size(info["out_bytes"]),
                             Color.dim("%.2fs" % info["seconds"])))
                except BaseException as exc:
                    if is_interrupt(exc):
                        raise Interrupted("已中止：图片完成 %d/%d 张"
                                          % (len(succeeded), len(images))) from None
                    failed.append((src, exc))
                    print(Color.red("  [%d/%d] %s 处理失败：%s"
                                    % (index, len(images), clip_text(rel, 38), exc)))
                    if args.traceback:
                        traceback.print_exc()
        except Interrupted as exc:
            _report_interrupted(exc, succeeded, overall_start)
            return 130
        print()

    # ---- 视频 ----
    if videos:
        if vhs_video is None:
            print(Color.red("[视频] 模块不可用，已全部跳过"))
        else:
            if not vhs_video.ffmpeg_available():
                print(Color.yellow("  未找到 ffmpeg，将回退到 OpenCV 写入器"
                                   "（无音轨、画质较低）"))
                print(Color.dim("  建议运行：python setup_deps.py"))
                print()
            print(Color.bold("[视频] 共 %d 个" % len(videos)))
            try:
                for index, src in enumerate(videos, 1):
                    dst = mirror_out_path(src, in_dir, out_dir,
                                          suffix="_vhs" if args.suffix else "",
                                          force_ext=args.video_ext)
                    rel = os.path.relpath(dst, out_dir)
                    name = os.path.basename(src)
                    print("  [%d/%d] %s" % (index, len(videos), clip_text(rel, 46)))

                    def on_progress(done, total, _elapsed, _name=name):
                        print_video_progress(done, total, time.time() - _elapsed,
                                             _name)

                    info = do_video(
                        src, dst, params,
                        target_height=args.height, crf=args.crf,
                        preset=args.encode_preset,
                        keep_audio=not args.no_audio,
                        max_seconds=args.video_seconds,
                        progress=on_progress,
                        encoder=args.encoder,
                        start_time=start_time,
                    )
                    print_video_progress(info["frames"], info["frames"] or 0,
                                         info["seconds"], name, final=True)
                    succeeded.append(dst)
                    print("    完成 %dx%d -> %dx%d  %d 帧  %s%s  %s"
                          % (info["src_size"][0], info["src_size"][1],
                             info["out_size"][0], info["out_size"][1],
                             info["frames"], human_size(info["out_bytes"]),
                             "  含音轨" if info["audio"] else "  无音轨",
                             Color.dim(human_time(info["seconds"]))))
            except Interrupted as exc:
                _report_interrupted(exc, succeeded, overall_start)
                return 130
            except BaseException as exc:
                if is_interrupt(exc):
                    _report_interrupted(None, succeeded, overall_start)
                    return 130
                failed.append((src, exc))
                print(Color.red("    处理失败：%s" % exc))
                if args.traceback:
                    traceback.print_exc()
            print()

    print(Color.bold("处理完成"))
    print("  成功 %d 个，失败 %d 个，总耗时 %s"
          % (len(succeeded), len(failed), human_time(time.time() - overall_start)))
    print("  输出目录：%s" % Color.cyan(out_dir))
    if failed:
        print(Color.red("  失败清单："))
        for src, exc in failed[:20]:
            print("    - %s：%s" % (os.path.basename(src), exc))
    return 0 if not failed else 1


def _report_interrupted(exc: Optional[Interrupted], succeeded: List[str],
                        started: float) -> None:
    """统一打印中止小结。"""
    print()
    if exc is not None:
        print(Color.yellow("  %s" % exc.message))
    else:
        print(Color.yellow("  已中止"))
    print(Color.dim("  不保存进度；已完成的文件保留在输出目录，半成品已删除。"))
    print()
    print(Color.bold("中止汇总：成功 %d 个，耗时 %s"
                     % (len(succeeded), human_time(time.time() - started))))


# ---------------------------------------------------------------------------
# 命令：单文件
# ---------------------------------------------------------------------------
def cmd_image(args) -> int:
    src = os.path.abspath(args.src)
    if not os.path.isfile(src):
        print(Color.red("文件不存在：%s" % src))
        return 2
    params = build_params(args)
    start_time, time_text = build_start_time(args, params)
    dst = (os.path.abspath(args.output) if args.output else unique_path(
        os.path.join(OUTPUT_DIR,
                     os.path.splitext(os.path.basename(src))[0] + "_vhs.jpg")))
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    print("处理图片：%s" % src)
    if params.osd_timestamp:
        print("  印制的日期时间：%s" % Color.bold(time_text))
    info = do_image(src, dst, params, quality=args.quality, start_time=start_time)
    print("  %dx%d -> %dx%d  %s  %.2fs"
          % (info["src_size"][0], info["src_size"][1],
             info["work_size"][0], info["work_size"][1],
             human_size(info["out_bytes"]), info["seconds"]))
    print(Color.green("输出：%s" % dst))
    return 0


def cmd_video(args) -> int:
    src = os.path.abspath(args.src)
    if not os.path.isfile(src):
        print(Color.red("文件不存在：%s" % src))
        return 2
    if vhs_video is None:
        print(Color.red("视频模块不可用"))
        return 2

    params = build_params(args)
    start_time, time_text = build_start_time(args, params)
    ext = args.video_ext or os.path.splitext(src)[1] or ".mp4"
    dst = (os.path.abspath(args.output) if args.output else unique_path(
        os.path.join(OUTPUT_DIR,
                     os.path.splitext(os.path.basename(src))[0] + "_vhs" + ext)))
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    print("处理视频：%s" % src)
    if params.osd_timestamp:
        print("  起始时间：%s（逐秒递增）" % Color.bold(time_text))
    print("  %s" % Color.dim(vhs_video.describe_encoders(args.encoder)))

    name = os.path.basename(src)
    try:
        info = do_video(
            src, dst, params, target_height=args.height, crf=args.crf,
            preset=args.encode_preset, keep_audio=not args.no_audio,
            max_seconds=args.video_seconds,
            progress=lambda d, t, e: print_video_progress(d, t, time.time() - e, name),
            encoder=args.encoder, start_time=start_time)
    except Interrupted as exc:
        print()
        print(Color.yellow("  %s" % exc.message))
        return 130
    print_video_progress(info["frames"], info["frames"] or 0, info["seconds"],
                         name, final=True)
    print("  %dx%d -> %dx%d  %d 帧  %s%s  %s"
          % (info["src_size"][0], info["src_size"][1],
             info["out_size"][0], info["out_size"][1],
             info["frames"], human_size(info["out_bytes"]),
             "  含音轨" if info["audio"] else "  无音轨",
             human_time(info["seconds"])))
    print(Color.green("输出：%s" % dst))
    return 0


# ---------------------------------------------------------------------------
# 命令：预设与参数
# ---------------------------------------------------------------------------
PRESET_DESCRIPTIONS = {
    "light": "轻微。画质损失很小，仅带一点录像带质感。",
    "classic": "经典。默认取值，各方面表现均衡。",
    "heavy": "重度。噪点、抖动与串色均较明显。",
    "worn": "破损。模拟经多次翻录、磨损严重的录像带。",
    "clean_tv": "电视。干净的录像带在老式显像管电视上播放的效果。",
}


def cmd_presets(args) -> int:
    print(BANNER)
    print("  可用预设（配合 --preset 使用）\n")
    for name in ("light", "classic", "heavy", "worn", "clean_tv"):
        print("  %-10s %s" % (Color.bold(name), PRESET_DESCRIPTIONS.get(name, "")))
    print()
    print(Color.dim("  整体强度：--strength 0.5 / 1.0 / 1.5 / 2.0"))
    print(Color.dim("  单项覆盖：--set luma_noise=1.2 --set jitter=0.9"))
    print(Color.dim("  查看全部参数：python vhs_processor.py show --preset heavy"))
    return 0


def cmd_show(args) -> int:
    params = build_params(args)
    cleaned = {key: (round(value, 6) if isinstance(value, float) else value)
               for key, value in asdict(params).items()}
    print("当前生效参数：")
    print(json.dumps(cleaned, indent=2, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# 命令：初始化目录
# ---------------------------------------------------------------------------
def cmd_init(args) -> int:
    for directory in (INPUT_DIR, OUTPUT_DIR):
        os.makedirs(directory, exist_ok=True)
        print("  已确认目录：%s" % directory)
    if not os.path.exists(CONFIG_PATH) or args.force:
        config = {
            "preset": "classic",
            "strength": 1.0,
            "params": {},
            "_说明": "params 段可覆盖任意参数，例如 {\"luma_noise\": 1.2}；"
                     "命令行 --set 的优先级最高",
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, ensure_ascii=False)
        print("  已写入配置：%s" % CONFIG_PATH)
    else:
        print("  配置已存在，如需覆盖请加 --force：%s" % CONFIG_PATH)
    return 0


# ---------------------------------------------------------------------------
# 命令行定义
# ---------------------------------------------------------------------------
def add_common_options(parser: argparse.ArgumentParser) -> None:
    """为子命令添加通用选项。"""
    parser.add_argument("--preset", "-p", default=None,
                        help="预设：light / classic / heavy / worn / clean_tv，默认 classic")
    parser.add_argument("--strength", "-s", type=float, default=None,
                        help="整体强度倍率：1.0 为预设原样，0.5 减半，2.0 加倍")
    parser.add_argument("--set", action="append", metavar="键=值",
                        help="单项参数覆盖，可重复使用，例如 --set jitter=0.9")
    parser.add_argument("--seed", type=int, default=None,
                        help="随机种子；相同种子结果完全一致")
    parser.add_argument("--no-osd", action="store_true", help="关闭全部叠加文字")
    parser.add_argument("--osd", action="store_true", help="强制开启叠加文字")
    parser.add_argument("--no-timestamp", action="store_true", help="仅关闭右下角日期时间")
    parser.add_argument("--quality", type=int, default=95,
                        help="JPEG 输出质量，取值 1-100，默认 95")
    parser.add_argument("--traceback", action="store_true", help="出错时打印完整调用栈")

    group = parser.add_argument_group("日期时间叠加")
    group.add_argument("--time", "-t", default=None, metavar="起始时间",
                       help="起始日期时间，可自动识别多种写法。"
                            "照片直接印该时间，视频自该时间逐秒递增。"
                            "示例：'1994-06-11 12:00:00'、'2001/9/11 08:46'、"
                            "'12:00:00'、'1994年6月11日 12点00分'、"
                            "'19940611120000'、'now'")
    group.add_argument("--time-offset", type=float, default=None, metavar="秒",
                       help="在起始时间基础上平移若干秒，例如 --time-offset -3600")
    group.add_argument("--timestamp-mode", choices=["auto", "fixed", "advance"],
                       default=None,
                       help="auto 为默认：照片固定、视频递增；fixed 恒为同一时间；"
                            "advance 一律递增")
    group.add_argument("--timestamp-speed", type=float, default=None, metavar="倍率",
                       help="时间流逝倍率，2.0 表示视频内时间流逝速度加倍，默认 1.0")
    group.add_argument("--timestamp-text", default=None, metavar="文字",
                       help="直接指定时间戳文字，指定后不再进行时间推演")
    group.add_argument("--rec-color", default=None, metavar="颜色",
                       help="REC 圆点颜色，形如 #FF2E2E 或 255,46,46")
    group.add_argument("--rec-blink", type=float, default=None, metavar="赫兹",
                       help="REC 圆点闪烁频率，单位赫兹；0 表示常亮")


def add_encoder_options(parser: argparse.ArgumentParser) -> None:
    """为涉及视频的子命令添加编码选项。"""
    parser.add_argument("--height", type=int, default=1080,
                        help="视频输出最大高度，默认 1080")
    parser.add_argument("--crf", type=int, default=18,
                        help="视频质量参数，数值越小质量越高，默认 18")
    parser.add_argument("--encode-preset", default="medium",
                        help="编码速度预设，默认 medium")
    parser.add_argument("--encoder", "-e", default="auto",
                        help="编码器：auto（默认，优先硬件）/ cpu / gpu / "
                             "nvenc / qsv / amf")
    parser.add_argument("--no-audio", action="store_true", help="丢弃音轨")
    parser.add_argument("--video-ext", default=None, help="强制输出扩展名，例如 .mp4")
    parser.add_argument("--video-seconds", type=float, default=None,
                        help="每个视频仅处理前若干秒，便于快速预览")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vhs_processor",
        description="为照片与视频叠加 VHS 录像带效果：input 目录进，output 目录出，"
                    "自动镜像子文件夹结构。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version",
                        version="vhs_processor %s" % __version__)
    sub = parser.add_subparsers(dest="command", metavar="命令")

    run = sub.add_parser("run", help="批量处理 input 目录中的全部图片与视频")
    run.add_argument("-i", "--input", default=None,
                     help="输入目录，默认为本工具目录下的 input")
    run.add_argument("-o", "--output", default=None,
                     help="输出目录，默认为本工具目录下的 output")
    run.add_argument("--no-image", action="store_true", help="不处理图片")
    run.add_argument("--no-video", action="store_true", help="不处理视频")
    run.add_argument("--suffix", action="store_true",
                     help="输出文件名追加 _vhs 后缀，默认沿用原名")
    add_encoder_options(run)
    add_common_options(run)
    run.set_defaults(func=cmd_run)

    image = sub.add_parser("image", help="处理单张图片")
    image.add_argument("src", help="输入图片路径")
    image.add_argument("-o", "--output", default=None, help="输出文件路径")
    add_common_options(image)
    image.set_defaults(func=cmd_image)

    video = sub.add_parser("video", help="处理单个视频")
    video.add_argument("src", help="输入视频路径")
    video.add_argument("-o", "--output", default=None, help="输出文件路径")
    add_encoder_options(video)
    add_common_options(video)
    video.set_defaults(func=cmd_video)

    presets = sub.add_parser("presets", help="列出全部预设")
    presets.set_defaults(func=cmd_presets)

    show = sub.add_parser("show", help="打印当前生效的全部参数")
    add_common_options(show)
    show.set_defaults(func=cmd_show)

    init = sub.add_parser("init", help="创建 input 与 output 目录及配置文件")
    init.add_argument("--force", action="store_true", help="覆盖已有配置文件")
    init.set_defaults(func=cmd_init)

    return parser


# ---------------------------------------------------------------------------
# 程序入口
# ---------------------------------------------------------------------------
def install_interrupt_handler() -> None:
    """把 Ctrl+C 统一转换为 KeyboardInterrupt。

    部分平台上外部进程可能截获控制台事件，显式安装处理函数更为可靠。
    处理函数只负责抛出异常，收尾工作由主流程的异常处理完成。
    """
    def handler(_signum, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError, AttributeError):
        pass  # 非主线程或平台不支持时沿用默认行为


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    install_interrupt_handler()

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        print(BANNER)
        parser.print_help()
        return 0

    try:
        return args.func(args)
    except Interrupted as exc:
        print()
        print(Color.yellow(exc.message))
        print(Color.dim("  不保存进度；已完成的文件保留在输出目录。"))
        return 130
    except KeyboardInterrupt:
        print()
        print(Color.yellow("已中止（Ctrl+C）。"))
        print(Color.dim("  不保存进度；已完成的文件保留在输出目录，"
                        "未完成的半成品文件已删除。"))
        return 130


if __name__ == "__main__":
    sys.exit(main())

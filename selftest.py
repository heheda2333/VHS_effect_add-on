#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自检脚本。

使用内置素材依次验证各条处理链路，覆盖图片处理、视频处理、
日期时间推演、参数解析与文件夹镜像，并输出简要结论。

用法::

    python selftest.py                    # 使用 Material 目录中的素材
    python selftest.py --source <目录>    # 指定素材目录
    python selftest.py --keep             # 保留测试产物以便查看
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import traceback
from typing import List, Optional, Tuple

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

LIBS_DIR = os.path.join(_ROOT, "libs")
if os.path.isdir(LIBS_DIR) and LIBS_DIR not in sys.path:
    sys.path.insert(0, LIBS_DIR)

from vhs_lib import vhs_video
from vhs_lib.vhs_effects import (
    describe_time,
    parse_start_time,
    resolve_preset,
)
from vhs_lib.vhs_task import do_image, do_video, mirror_out_path, scan_tree

DEFAULT_SOURCE = os.path.join(_ROOT, "Material")
DEFAULT_OUTPUT = os.path.join(_ROOT, "_selftest")

# 需要验证的起始时间写法
TIME_CASES = (
    "1994-06-11 12:00:00",
    "1994/6/11 12:00",
    "1994-06-11",
    "12:00:00",
    "12:00",
    "1994年6月11日 12点00分00秒",
    "1994年6月11日12时30分",
    "19940611120000",
    "19940611",
    "120000",
)

# 需要验证的参数覆盖写法
PARAM_CASES = (
    "luma_noise=1.2",
    "jitter=0.9",
    "osd_rec_blink=2.0",
    "osd_scale=1.5",
)


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
    return "%d 分 %02d 秒" % (minutes, sec)


class Report:
    """收集检查项结果并统一输出。"""

    def __init__(self) -> None:
        self.failures: List[Tuple[str, str]] = []
        self.total = 0

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        self.total += 1
        mark = "[通过]" if condition else "[失败]"
        print("  %s %s%s" % (mark, name, ("  " + detail) if detail else ""))
        if not condition:
            self.failures.append((name, detail))
        return condition

    def fail(self, name: str, detail: str) -> None:
        self.total += 1
        print("  [失败] %s  %s" % (name, detail))
        self.failures.append((name, detail))


def section(title: str) -> None:
    print()
    print("=" * 66)
    print(" %s" % title)
    print("=" * 66)


def check_environment(report: Report) -> None:
    section("一、运行环境")
    print("  Python        ：%s" % sys.version.split()[0])
    try:
        import numpy

        print("  numpy         ：%s" % numpy.__version__)
    except Exception:
        pass
    try:
        import PIL

        print("  Pillow        ：%s" % PIL.__version__)
    except Exception:
        pass

    report.check("OpenCV 可用（模糊与重采样加速）", vhs_video.HAVE_CV2,
                 "" if vhs_video.HAVE_CV2 else "缺失，将使用后备实现，速度较慢")

    ffmpeg = vhs_video.find_ffmpeg()
    report.check("ffmpeg 可用（视频处理与音轨保留）", bool(ffmpeg),
                 "" if ffmpeg else "缺失，视频将回退为无音轨输出")

    if ffmpeg:
        encoders = vhs_video.probe_hardware_encoders()
        if encoders:
            names = "、".join(meta["label"] for meta in encoders.values())
            print("  硬件编码器    ：%s" % names)
        else:
            print("  硬件编码器    ：未检测到，将使用 CPU 编码")


def check_time_parsing(report: Report, out_dir: os.PathLike) -> None:
    section("二、日期时间解析与推演")

    for text in TIME_CASES:
        try:
            moment = parse_start_time(text)
            report.check("解析 %-24s" % text, True, "-> %s" % describe_time(moment))
        except ValueError as exc:
            report.fail("解析 %r" % text, str(exc).splitlines()[0])

    # 无法识别的输入应明确报错
    try:
        parse_start_time("不是时间")
        report.fail("拒绝无法识别的输入", "本应抛出异常却成功返回")
    except ValueError:
        report.check("拒绝无法识别的输入", True, "已按预期抛出异常")

    # 照片固定、视频递增
    from vhs_lib.vhs_effects import VHSContext

    params = resolve_preset("classic")
    start = parse_start_time("2001-09-11 08:46:00")

    photo_ctx = VHSContext(params, (180, 320), fps=25.0, start_time=start,
                           advancing=False)
    fixed = photo_ctx._timestamp_text(0) == photo_ctx._timestamp_text(2500) == \
        "2001-09-11  08:46:00"
    report.check("照片时间戳固定不变", fixed, photo_ctx._timestamp_text(2500))

    video_ctx = VHSContext(params, (180, 320), fps=25.0, start_time=start,
                           advancing=True)
    advanced = (video_ctx._timestamp_text(0) == "2001-09-11  08:46:00"
                and video_ctx._timestamp_text(25) == "2001-09-11  08:46:01"
                and video_ctx._timestamp_text(250) == "2001-09-11  08:46:10")
    report.check("视频时间戳逐秒递增", advanced,
                 "%s -> %s -> %s" % (video_ctx._timestamp_text(0),
                                     video_ctx._timestamp_text(25),
                                     video_ctx._timestamp_text(250)))

    # REC 圆点：照片常亮、视频闪烁
    lit = photo_ctx._rec_blink_gain(0) == 1.0 and photo_ctx._rec_blink_gain(7) == 1.0
    report.check("照片 REC 圆点常亮", lit)

    gains = [video_ctx._rec_blink_gain(i) for i in range(40)]
    report.check("视频 REC 圆点闪烁", min(gains) < 0.2 and max(gains) > 0.9,
                 "增益范围 %.2f ~ %.2f" % (min(gains), max(gains)))

    # REC 圆点颜色应保持红色，不被重影染成紫色
    # 注意：VHSContext.process 的输入输出均为 BGR，判定前先转换为 RGB。
    import numpy as np

    probe = VHSContext(params, (240, 400), fps=25.0, advancing=False)
    frame = np.full((240, 400, 3), 128, np.uint8)
    bgr = probe.process(frame, 0).astype(int)
    region = bgr[6:22, 8:28]
    saturation = region.max(axis=2) - region.min(axis=2)
    strong = region[saturation > 50]
    if len(strong):
        # strong 的形状是 (像素数, 3)，通道在最后一个轴上，用省略号取其反转
        rgb = strong[..., ::-1]
        red, green, blue = (rgb[:, 0].mean(), rgb[:, 1].mean(), rgb[:, 2].mean())
        is_red = red > blue + 80 and red > green + 80
        report.check("REC 圆点保持红色", is_red,
                     "平均 RGB = %s" % [int(round(v)) for v in (red, green, blue)])
    else:
        report.fail("REC 圆点保持红色", "未在预期区域内找到高饱和像素")


def check_parameters(report: Report) -> None:
    section("三、参数装配")

    import vhs_processor

    parser = vhs_processor.build_parser()

    for case in PARAM_CASES:
        try:
            args = parser.parse_args(["show", "--set", case])
            params = vhs_processor.build_params(args)
            key, raw = case.split("=", 1)
            actual = getattr(params, key)
            report.check("覆盖参数 %-22s" % case, abs(float(actual) - float(raw)) < 1e-6,
                         "-> %s" % actual)
        except Exception as exc:
            report.fail("覆盖参数 %r" % case, str(exc))

    # 整体强度缩放
    try:
        args = parser.parse_args(["show", "--preset", "classic", "--set",
                                  "luma_noise=1.0", "--strength", "0.5"])
        params = vhs_processor.build_params(args)
        report.check("整体强度缩放", abs(params.luma_noise - 0.5) < 1e-6,
                     "luma_noise 1.0 × 0.5 = %.3f" % params.luma_noise)
    except Exception as exc:
        report.fail("整体强度缩放", str(exc))

    # 预设取值应互相不同
    values = {name: resolve_preset(name).luma_noise
              for name in ("light", "classic", "heavy")}
    report.check("预设之间存在差异", len(set(values.values())) == 3,
                 "%s" % values)

    # 时间参数错误时应当报错退出
    try:
        args = parser.parse_args(["show", "--time", "不是时间"])
        vhs_processor.build_params(args)
        report.fail("非法时间应报错", "本应退出却成功返回")
    except SystemExit as exc:
        report.check("非法时间应报错退出", exc.code == 2, "退出码 %s" % exc.code)


def check_mirror(report: Report, images: List[str], out_dir: str) -> None:
    section("四、文件夹镜像")

    base = os.path.join(out_dir, "_mirror")
    in_dir = os.path.join(base, "input")
    res_dir = os.path.join(base, "output")
    shutil.rmtree(base, ignore_errors=True)

    if not images:
        report.fail("文件夹镜像", "没有可用素材")
        return

    first, second = images[0], images[-1]
    plan = [
        (os.path.join(in_dir, "第一卷", os.path.basename(first)), first),
        (os.path.join(in_dir, "第一卷", "夜拍", os.path.basename(second)), second),
        (os.path.join(in_dir, "中文名 与空格", os.path.basename(first)), first),
    ]
    for target, source in plan:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(source, target)

    # 放置一个非媒体文件，应被跳过且不影响目录结构
    with open(os.path.join(in_dir, "第一卷", "说明.txt"), "w", encoding="utf-8") as fh:
        fh.write("非媒体文件")

    found_images, _found_videos, found_skipped = scan_tree(in_dir)
    report.check("递归扫描到全部图片", len(found_images) == len(plan),
                 "找到 %d 个" % len(found_images))
    report.check("非媒体文件被跳过", len(found_skipped) == 1,
                 "跳过 %s" % [os.path.basename(p) for p in found_skipped])

    params = resolve_preset("classic").merged(max_dim=320, osd=False)
    try:
        for source in found_images:
            target = mirror_out_path(source, in_dir, res_dir)
            do_image(source, target, params)
    except Exception as exc:
        report.fail("镜像输出处理", str(exc))
        return

    missing = []
    for target, _source in plan:
        expected = os.path.join(res_dir, os.path.relpath(target, in_dir))
        if not os.path.exists(expected):
            missing.append(os.path.relpath(expected, res_dir))
    report.check("输出目录结构完整镜像", not missing,
                 "缺失 %s" % missing if missing else "含中文名与多层嵌套")

    shutil.rmtree(base, ignore_errors=True)


def check_images(report: Report, params, images: List[str], out_dir: str,
                 quality: int, start_time) -> None:
    section("五、图片处理")

    if not images:
        report.fail("图片处理", "没有可用素材")
        return

    target_dir = os.path.join(out_dir, "images")
    os.makedirs(target_dir, exist_ok=True)

    for index, source in enumerate(images, 1):
        name = os.path.basename(source)
        target = os.path.join(target_dir,
                              os.path.splitext(name)[0] + "_vhs.jpg")
        try:
            info = do_image(source, target, params, quality=quality,
                            start_time=start_time)
            report.check("处理 %-28s" % name, info["out_bytes"] > 0,
                         "%dx%d -> %dx%d，%s，%.2f 秒"
                         % (info["src_size"][0], info["src_size"][1],
                            info["work_size"][0], info["work_size"][1],
                            human_size(info["out_bytes"]), info["seconds"]))
        except Exception as exc:
            report.fail("处理 %s" % name, str(exc))
            traceback.print_exc()


def check_videos(report: Report, params, videos: List[str], out_dir: str,
                 height: int, seconds: float, encode_preset: str,
                 keep_audio: bool, encoder: str, start_time) -> None:
    section("六、视频处理（每个仅处理前 %.1f 秒）" % seconds)

    if not videos:
        report.fail("视频处理", "没有可用素材")
        return
    if not vhs_video.ffmpeg_available() and not vhs_video.HAVE_CV2:
        report.fail("视频处理", "缺少 ffmpeg 与 OpenCV，无法进行")
        return

    target_dir = os.path.join(out_dir, "videos")
    os.makedirs(target_dir, exist_ok=True)

    for index, source in enumerate(videos, 1):
        name = os.path.basename(source)
        target = os.path.join(target_dir,
                              os.path.splitext(name)[0] + "_vhs.mp4")
        print("  [%d/%d] %s" % (index, len(videos), name))
        try:
            info = do_video(source, target, params, target_height=height,
                            crf=24, preset=encode_preset, keep_audio=keep_audio,
                            max_seconds=seconds, encoder=encoder,
                            start_time=start_time)
            report.check("处理 %-28s" % name, info["out_bytes"] > 0,
                         "%dx%d -> %dx%d，%d 帧，%s，%s"
                         % (info["src_size"][0], info["src_size"][1],
                            info["out_size"][0], info["out_size"][1],
                            info["frames"], human_size(info["out_bytes"]),
                            human_time(info["seconds"])))
        except Exception as exc:
            report.fail("处理 %s" % name, str(exc))
            traceback.print_exc()


def main() -> int:
    parser = argparse.ArgumentParser(description="VHS 效果处理工具自检")
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help="素材目录，默认为本工具目录下的 Material")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="测试产物目录，默认为本工具目录下的 _selftest")
    parser.add_argument("--preset", default="classic", help="测试所用预设")
    parser.add_argument("--strength", type=float, default=1.0, help="整体强度倍率")
    parser.add_argument("--height", type=int, default=480,
                        help="测试视频的输出高度，默认 480")
    parser.add_argument("--seconds", type=float, default=1.0,
                        help="每个视频仅处理前若干秒，默认 1.0")
    parser.add_argument("--encode-preset", default="veryfast",
                        help="测试所用编码速度预设，默认 veryfast")
    parser.add_argument("--encoder", default="auto", help="视频编码器")
    parser.add_argument("--quality", type=int, default=92, help="JPEG 输出质量")
    parser.add_argument("--max-dim", type=int, default=960,
                        help="图片处理的最长边，默认 960")
    parser.add_argument("--no-audio", action="store_true", help="测试时丢弃音轨")
    parser.add_argument("--limit", type=int, default=0,
                        help="每类最多测试几个，0 表示全部")
    parser.add_argument("--keep", action="store_true", help="保留测试产物")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    out_dir = os.path.abspath(args.output)
    source_dir = os.path.abspath(args.source)
    os.makedirs(out_dir, exist_ok=True)

    print("=" * 66)
    print(" VHS 效果处理工具 —— 自检")
    print(" 素材目录：%s" % source_dir)
    print(" 产物目录：%s" % out_dir)
    print("=" * 66)

    report = Report()
    check_environment(report)

    if not os.path.isdir(source_dir):
        print()
        print("素材目录不存在：%s" % source_dir)
        print("请使用 --source 指定其他目录，或向该目录放入测试素材。")
        return 1

    images, videos, _skipped = scan_tree(source_dir)
    if args.limit:
        images = images[: args.limit]
        videos = videos[: args.limit]
    print()
    print("  素材统计      ：%d 张图片，%d 个视频" % (len(images), len(videos)))

    check_time_parsing(report, out_dir)
    check_parameters(report)
    check_mirror(report, images, out_dir)

    params = resolve_preset(args.preset).merged(max_dim=args.max_dim)
    if abs(args.strength - 1.0) > 1e-6:
        params = params.scaled(args.strength)
    start_time = parse_start_time("2004-07-23 09:15:00")

    started = time.time()
    check_images(report, params, images, out_dir, args.quality, start_time)
    check_videos(report, params, videos, out_dir, args.height, args.seconds,
                 args.encode_preset, not args.no_audio, args.encoder, start_time)
    elapsed = time.time() - started

    section("自检结果")
    if report.failures:
        print("  共 %d 项检查，失败 %d 项：" % (report.total, len(report.failures)))
        for name, detail in report.failures:
            print("    - %s  %s" % (name, detail))
    else:
        print("  共 %d 项检查，全部通过。" % report.total)
    print("  处理耗时：%s" % human_time(elapsed))

    if not args.keep:
        shutil.rmtree(out_dir, ignore_errors=True)
        print("  测试产物已清理（加 --keep 可保留以便查看）。")
    else:
        print("  测试产物保留在：%s" % out_dir)

    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())

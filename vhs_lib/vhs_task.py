# -*- coding: utf-8 -*-
"""批量处理流程：目录遍历、输出路径映射、单文件处理。

本模块供命令行与图形界面共用，二者只负责界面与参数收集，
具体的文件发现、路径映射与处理调度都在这里完成，避免逻辑重复。
"""

from __future__ import annotations

import datetime
import os
import shutil
import tempfile
import time
from typing import Callable, List, Optional, Tuple

from . import vhs_video
from .vhs_effects import (
    IMAGE_EXTS,
    VIDEO_EXTS,
    VHSContext,
    VHSParams,
    process_image,
)

__all__ = [
    "scan_tree",
    "mirror_out_path",
    "unique_path",
    "remove_quiet",
    "fit_even",
    "do_image",
    "do_video",
    "is_interrupt",
]


class Interrupted(Exception):
    """用户中止了处理。已完成的文件保留，未完成的半成品会被删除。"""

    def __init__(self, message: str = "已中止"):
        super().__init__(message)
        self.message = message


def is_interrupt(exc: BaseException) -> bool:
    """判断异常是否属于"用户中止"。

    外部进程（例如 ffmpeg）被中断时，Python 侧可能收到 BrokenPipeError 或
    带特定错误码的 OSError，这些也一并视为中止，以便走统一的收尾流程。
    """
    if isinstance(exc, (KeyboardInterrupt, Interrupted)):
        return True
    if isinstance(exc, BrokenPipeError):
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "errno", None) in (22, 32, 232):
            return True
        if "pipe" in str(exc).lower():
            return True
    return False


def remove_quiet(path: Optional[str]) -> None:
    """删除文件，忽略一切错误（例如文件仍被外部进程占用）。"""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 目录扫描与输出路径
# ---------------------------------------------------------------------------
def scan_tree(src_dir: str) -> Tuple[List[str], List[str], List[str]]:
    """递归扫描目录。

    返回 ``(图片路径列表, 视频路径列表, 被跳过的文件列表)``，均为绝对路径。
    隐藏文件与临时文件会被跳过。
    """
    images: List[str] = []
    videos: List[str] = []
    skipped: List[str] = []

    if not os.path.isdir(src_dir):
        return images, videos, skipped

    for current, dirs, files in os.walk(src_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or name.startswith("~$"):
                skipped.append(os.path.join(current, name))
                continue
            ext = os.path.splitext(name)[1].lower()
            path = os.path.join(current, name)
            if ext in IMAGE_EXTS:
                images.append(path)
            elif ext in VIDEO_EXTS:
                videos.append(path)
            else:
                skipped.append(path)
    return sorted(images), sorted(videos), sorted(skipped)


def mirror_out_path(src_file: str, src_root: str, out_root: str,
                    suffix: str = "_vhs", force_ext: Optional[str] = None) -> str:
    """把输入目录中的相对路径映射到输出目录，保持目录结构一致。

    若目标文件名已存在，则依次尝试追加 ``suffix``、``suffix2``…… 避免覆盖。
    """
    rel = os.path.relpath(src_file, src_root)
    rel_dir = os.path.dirname(rel)
    stem, ext = os.path.splitext(os.path.basename(rel))
    if force_ext:
        ext = force_ext
    target_dir = os.path.join(out_root, rel_dir) if rel_dir else out_root
    os.makedirs(target_dir, exist_ok=True)

    candidate = os.path.join(target_dir, stem + ext)
    if not os.path.exists(candidate):
        return candidate
    candidate = os.path.join(target_dir, stem + suffix + ext)
    index = 2
    while os.path.exists(candidate):
        candidate = os.path.join(target_dir, "%s%s%d%s" % (stem, suffix, index, ext))
        index += 1
    return candidate


def unique_path(path: str) -> str:
    """若路径已存在，则追加 ``_1``、``_2``…… 直到不冲突。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    index = 1
    while os.path.exists("%s_%d%s" % (stem, index, ext)):
        index += 1
    return "%s_%d%s" % (stem, index, ext)


def fit_even(width: int, height: int,
             target_height: Optional[int]) -> Tuple[int, int, bool]:
    """计算输出尺寸并取偶数（H.264 要求宽高为偶数）。

    返回 ``(宽, 高, 是否发生缩放)``。
    """
    w, h = int(width), int(height)
    if target_height and h > target_height:
        scale = target_height / float(h)
        w = int(round(w * scale))
        h = int(target_height)
    w -= w % 2
    h -= h % 2
    return max(2, w), max(2, h), (max(2, w), max(2, h)) != (int(width), int(height))


# ---------------------------------------------------------------------------
# 单文件处理
# ---------------------------------------------------------------------------
def do_image(src: str, dst: str, params: VHSParams, quality: int = 95,
             start_time: Optional[datetime.datetime] = None) -> dict:
    """处理单张图片。出错或中止时删除未完成的输出文件。"""
    started = time.time()
    try:
        info = process_image(src, dst, params, max_dim=params.max_dim,
                             start_time=start_time)
    except BaseException:
        remove_quiet(dst)
        raise
    info["seconds"] = time.time() - started
    try:
        info["out_bytes"] = os.path.getsize(dst)
    except OSError:
        info["out_bytes"] = 0
    return info


def do_video(src: str, dst: str, params: VHSParams,
             target_height: Optional[int] = 1080, crf: int = 18,
             preset: str = "medium", keep_audio: bool = True,
             max_seconds: Optional[float] = None,
             progress: Optional[Callable[[int, int, float], None]] = None,
             encoder: str = "auto",
             start_time: Optional[datetime.datetime] = None) -> dict:
    """处理单个视频。

    ``progress`` 为可选的进度回调，签名为 ``(已处理帧数, 总帧数, 已用秒数)``。
    """
    if vhs_video is None:                      # pragma: no cover - 依赖缺失
        raise RuntimeError("视频模块不可用")

    started = time.time()
    probe = vhs_video.probe_video(src)
    src_w, src_h = probe["width"], probe["height"]
    fps = probe["fps"] or 25.0
    if not src_w or not src_h:
        raise RuntimeError("无法读取视频尺寸，文件可能损坏或缺少解码支持")

    out_w, out_h, need_resize = fit_even(src_w, src_h, target_height)
    total_frames = probe["frames"] or 0
    if max_seconds and total_frames:
        total_frames = min(total_frames, int(max_seconds * fps))

    # 视频中的时间戳默认逐帧推进
    ctx = VHSContext(params, (out_h, out_w), fps=fps, start_time=start_time,
                     advancing=True)
    reader = vhs_video.VideoFrameReader(src, width=src_w, height=src_h, fps=fps)

    # 音轨单独抽取，视频先写入临时文件，最后再合并
    tmp_dir = None
    audio_path = None
    temp_video = None
    if keep_audio and probe["has_audio"]:
        tmp_dir = tempfile.mkdtemp(prefix="vhs_audio_")
        try:
            audio_path = vhs_video.extract_audio(src, tmp_dir)
        except Exception:
            audio_path = None

    if audio_path:
        # 扩展名必须保留：ffmpeg 依赖它判断封装格式
        temp_video = os.path.join(tmp_dir, "video_only.mp4")
        write_path = temp_video
    else:
        write_path = dst

    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    enc = vhs_video.resolve_encoder(encoder, crf=crf, preset=preset)
    writer = vhs_video.make_writer(write_path, out_w, out_h, fps, crf=crf,
                                   preset=preset, enc_args=enc["args"])

    index = 0
    written = 0
    last_report = 0.0
    try:
        while True:
            frame = reader.read()
            if frame is None:
                break
            if max_seconds and index >= int(max_seconds * fps):
                break
            if need_resize:
                frame = vhs_video.resize_bgr(frame, out_w, out_h)
            writer.write(ctx.process(frame, frame_index=index))
            written += 1
            index += 1
            if progress is not None:
                now = time.time()
                if now - last_report > 0.25:
                    last_report = now
                    progress(written, total_frames, now - started)
        writer.close()
    except BaseException as exc:
        writer.abort()
        reader.release()
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        # 中止时删除半成品，避免在输出目录留下无法播放的文件
        remove_quiet(write_path)
        if write_path != dst:
            remove_quiet(dst)
        if is_interrupt(exc):
            raise Interrupted("视频处理已中止，未完成的文件已删除") from None
        raise
    finally:
        reader.release()

    audio_ok = False
    if temp_video:
        try:
            audio_ok = vhs_video.mux_audio(temp_video, audio_path, dst)
        except Exception:
            audio_ok = False
        if not audio_ok:
            # 合并失败时退化为无音轨视频，但不丢弃已完成的画面
            try:
                shutil.copyfile(temp_video, dst)
            except Exception:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {
        "input": src,
        "output": dst,
        "src_size": (src_w, src_h),
        "out_size": (out_w, out_h),
        "fps": fps,
        "frames": written,
        "audio": bool(audio_path and audio_ok),
        "encoder": enc.get("label", "libx264"),
        "seconds": time.time() - started,
        "out_bytes": os.path.getsize(dst) if os.path.exists(dst) else 0,
    }

"""
vhs_video.py -- 视频读写（基于 ffmpeg 管道）

为什么不用 cv2.VideoWriter：
    OpenCV 在 Windows 上写 H.264 极不可靠（多半只能出 mp4v），
    而且没法保留原始音轨。这里改成把帧通过 stdin 喂给 ffmpeg，
    音轨单独抽取后再 mux 回去，画质与兼容性都好得多。

依赖优先级：
    1. imageio-ffmpeg 自带的 ffmpeg 二进制（pip 安装即有）
    2. 系统 PATH 里的 ffmpeg
    两者都没有时，回退到 cv2.VideoWriter（无音频）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Optional

import numpy as np

# 优先使用项目自带的 libs/ 目录
_LIBS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
if os.path.isdir(_LIBS) and _LIBS not in sys.path:
    sys.path.insert(0, _LIBS)

try:
    import cv2

    _HAVE_CV2 = True
except Exception:  # pragma: no cover
    cv2 = None
    _HAVE_CV2 = False

HAVE_CV2 = _HAVE_CV2

# Windows 下别弹出黑框
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# ffmpeg 定位
# ---------------------------------------------------------------------------
_FFMPEG_CACHE: Optional[str] = None
_FFPROBE_CACHE: Optional[str] = None


def find_ffmpeg() -> Optional[str]:
    """返回可用的 ffmpeg 路径，找不到返回 None。"""
    global _FFMPEG_CACHE
    # 缓存命中直接返回；缓存里是空串表示"上次没找到"，
    # 那种情况要重新探测一次（比如被 monkeypatch 干扰过，或依赖是后来才装上的）。
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE

    # 1) imageio-ffmpeg 自带
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            _FFMPEG_CACHE = exe
            return exe
    except Exception:
        pass

    # 2) 环境变量
    env = os.environ.get("FFMPEG_BINARY") or os.environ.get("FFMPEG_PATH")
    if env and os.path.exists(env):
        _FFMPEG_CACHE = env
        return env

    # 3) PATH
    exe = shutil.which("ffmpeg")
    if exe:
        _FFMPEG_CACHE = exe
        return exe

    _FFMPEG_CACHE = ""
    return None


def find_ffprobe() -> Optional[str]:
    """ffprobe 常常和 ffmpeg 一起，但 imageio-ffmpeg 并不附带。"""
    global _FFPROBE_CACHE
    if _FFPROBE_CACHE is not None:
        return _FFPROBE_CACHE or None
    exe = shutil.which("ffprobe")
    if exe:
        _FFPROBE_CACHE = exe
        return exe
    ff = find_ffmpeg()
    if ff:
        cand = os.path.join(os.path.dirname(ff), "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if os.path.exists(cand):
            _FFPROBE_CACHE = cand
            return cand
    _FFPROBE_CACHE = ""
    return None


def ffmpeg_available() -> bool:
    return find_ffmpeg() is not None


def _run(cmd, **kwargs):
    return subprocess.run(
        cmd,
        creationflags=_CREATE_NO_WINDOW,
        stdout=kwargs.pop("stdout", subprocess.DEVNULL),
        stderr=kwargs.pop("stderr", subprocess.DEVNULL),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 视频信息
# ---------------------------------------------------------------------------
def probe_video(path: str) -> dict:
    """尽量拿到视频的宽高/帧率/帧数/是否有音轨/时长。

    优先 OpenCV（能拿到准确帧数），缺失时从 ffmpeg -i 的输出里解析。
    """
    info = {
        "path": path, "width": 0, "height": 0, "fps": 0.0, "frames": 0,
        "duration": 0.0, "has_audio": False, "audio_codec": None,
        "video_codec": None,
    }

    # ---- 1) OpenCV（快，且能拿准确帧数） ----
    if _HAVE_CV2:
        cap = cv2.VideoCapture(path)
        if cap.isOpened():
            info["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            info["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            info["fps"] = float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
            info["frames"] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.release()

    # ---- 2) ffmpeg -i 解析（OpenCV 缺失时的主力） ----
    ff = find_ffmpeg()
    if ff:
        import re

        try:
            proc = subprocess.run(
                [ff, "-hide_banner", "-i", path],
                creationflags=_CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
            )
            text = proc.stderr or ""

            if "Audio:" in text:
                info["has_audio"] = True
                for line in text.splitlines():
                    if "Audio:" in line:
                        info["audio_codec"] = line.split("Audio:", 1)[1].strip().split(",")[0].strip()
                        break

            for line in text.splitlines():
                if "Video:" not in line:
                    continue
                info["video_codec"] = line.split("Video:", 1)[1].strip().split(",")[0].strip()
                if not info["width"]:
                    m = re.search(r"(\d{2,5})x(\d{2,5})", line)
                    if m:
                        info["width"] = int(m.group(1))
                        info["height"] = int(m.group(2))
                if not info["fps"]:
                    m = re.search(r"([\d.]+)\s*fps", line)
                    if m:
                        info["fps"] = float(m.group(1))
                break

            m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
            if m:
                info["duration"] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                    + float(m.group(3)))
        except Exception:
            pass

    if not info["fps"]:
        info["fps"] = 25.0
    if not info["frames"] and info["duration"]:
        info["frames"] = int(round(info["duration"] * info["fps"]))
    if not info["duration"] and info["frames"]:
        info["duration"] = info["frames"] / info["fps"]
    return info


# ---------------------------------------------------------------------------
# 写入器
# ---------------------------------------------------------------------------
class FFmpegWriter:
    """把 BGR 帧通过 stdin 喂给 ffmpeg。"""

    def __init__(self, out_path: str, width: int, height: int, fps: float,
                 crf: int = 18, preset: str = "medium", pix_fmt: str = "yuv420p",
                 ffmpeg: Optional[str] = None, enc_args: Optional[list] = None):
        exe = ffmpeg or find_ffmpeg()
        if not exe:
            raise RuntimeError("找不到 ffmpeg")
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps) if fps and fps > 0 else 25.0
        self.out_path = out_path
        self.enc_args = list(enc_args) if enc_args else build_encode_args(
            "libx264", crf=crf, preset=preset, pix_fmt=pix_fmt)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

        cmd = [
            exe, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", "%dx%d" % (self.width, self.height),
            "-r", "%.6f" % self.fps,
            "-i", "pipe:0",
            "-an",
        ] + self.enc_args + [
            "-movflags", "+faststart",
            out_path,
        ]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
        self._closed = False

    def write(self, frame_bgr: np.ndarray) -> None:
        if frame_bgr.shape[0] != self.height or frame_bgr.shape[1] != self.width:
            raise ValueError("帧尺寸不匹配")
        if frame_bgr.dtype != np.uint8:
            frame_bgr = frame_bgr.astype(np.uint8)
        try:
            self.proc.stdin.write(np.ascontiguousarray(frame_bgr).tobytes())
        except (BrokenPipeError, OSError) as exc:
            # ffmpeg 挂了（比如编码器不支持）时把它的报错信息带出来
            err = b""
            try:
                err = self.proc.stderr.read() or b""
            except Exception:
                pass
            raise RuntimeError(
                "ffmpeg 写入失败：%s %s" % (exc, err.decode("utf-8", "replace")[:400])
            ) from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        err = b""
        try:
            err = self.proc.stderr.read() or b""
        except Exception:
            pass
        code = self.proc.wait()
        if code != 0:
            raise RuntimeError(
                "ffmpeg 写入失败 (exit %s): %s" % (code, err.decode("utf-8", "replace")[:800])
            )

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 编码器选择（含硬件编码）
# ---------------------------------------------------------------------------
# 参考数据（1920×1080，125 帧，仅统计编码阶段）：
#   libx264 medium   约 108 fps
#   libx264 veryfast 约 129 fps
#   h264_nvenc       约 123 fps
# 而叠加 VHS 效果后的整体速度约为 1 fps，编码仅占总耗时很小一部分，
# 因此改用硬件编码对总时长影响有限，其价值在于降低编码阶段的 CPU 占用与功耗。
GPU_ENCODERS = {
    "nvenc": {
        "name": "h264_nvenc",
        "label": "NVIDIA NVENC",
        "pix_fmt": "yuv420p",
    },
    "qsv": {
        "name": "h264_qsv",
        "label": "Intel Quick Sync",
        "pix_fmt": "nv12",
    },
    "amf": {
        "name": "h264_amf",
        "label": "AMD AMF",
        "pix_fmt": "yuv420p",
    },
}

_ENCODER_CACHE: dict = {}


def build_encode_args(codec: str, crf: int = 18, preset: str = "medium",
                      pix_fmt: str = "yuv420p") -> list:
    """按编码器生成参数。不同硬件编码器的质量控制参数名完全不一样。"""
    key = (codec, int(crf), preset, pix_fmt)
    if key in _ENCODER_CACHE:
        return list(_ENCODER_CACHE[key])

    if codec == "h264_nvenc":
        # NVENC 用 -cq（恒定质量），preset 是 p1(最快)~p7(最慢)
        nv = {"ultrafast": "p1", "superfast": "p2", "veryfast": "p3", "faster": "p4",
              "fast": "p4", "medium": "p5", "slow": "p6", "slower": "p7",
              "veryslow": "p7"}
        args = ["-c:v", "h264_nvenc", "-preset", nv.get(str(preset), "p4"),
                "-cq", str(int(crf)), "-rc", "vbr", "-b:v", "0",
                "-pix_fmt", pix_fmt]
    elif codec == "h264_qsv":
        qp = {"veryfast": "veryfast", "faster": "faster", "fast": "fast",
              "medium": "medium", "slow": "slow", "slower": "slower",
              "veryslow": "veryslow"}.get(str(preset), "medium")
        args = ["-c:v", "h264_qsv", "-preset", qp,
                "-global_quality", str(int(crf)), "-look_ahead", "0",
                "-pix_fmt", pix_fmt]
    elif codec == "h264_amf":
        args = ["-c:v", "h264_amf", "-quality", "balanced",
                "-rc", "cqp", "-qp_i", str(int(crf)), "-qp_p", str(int(crf)),
                "-pix_fmt", pix_fmt]
    else:  # libx264
        args = ["-c:v", "libx264", "-preset", str(preset),
                "-crf", str(int(crf)), "-pix_fmt", pix_fmt]

    _ENCODER_CACHE[key] = list(args)
    return args


def _probe_encoders() -> dict:
    """检测当前环境中可用的硬件编码器。

    编码器出现在 ffmpeg 的列表中并不代表可以实际使用（可能缺少驱动或
    运行库），因此这里会真实编码一小段进行验证。
    """
    if "available" in _ENCODER_CACHE:
        return _ENCODER_CACHE["available"]

    ff = find_ffmpeg()
    available = {}
    if not ff:
        _ENCODER_CACHE["available"] = available
        return available

    # 先看编译进去了哪些
    listed = ""
    try:
        proc = subprocess.run([ff, "-hide_banner", "-encoders"],
                              creationflags=_CREATE_NO_WINDOW, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True,
                              encoding="utf-8", errors="replace")
        listed = proc.stdout or ""
    except Exception:
        pass

    for key, meta in GPU_ENCODERS.items():
        if meta["name"] not in listed:
            continue
        # 真跑一帧，确认驱动/DLL 没问题
        try:
            proc = subprocess.run(
                [ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=c=black:s=256x144:r=25:d=0.1",
                 "-c:v", meta["name"], "-f", "null", "-"],
                creationflags=_CREATE_NO_WINDOW, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=30,
            )
            if proc.returncode == 0:
                available[key] = meta
        except Exception:
            continue

    _ENCODER_CACHE["available"] = available
    return available


def probe_hardware_encoders() -> dict:
    """探测当前环境中可用的硬件编码器，返回 ``{标识: 元信息}``。

    结果在进程内缓存。编码器出现在 ffmpeg 的列表中并不代表可以实际使用
    （可能缺少驱动或运行库），因此这里会真实编码一小段进行验证。
    """
    return _probe_encoders()


def resolve_encoder(encoder: str = "auto", crf: int = 18, preset: str = "medium",
                    pix_fmt: str = "yuv420p") -> dict:
    """把编码器选项解析为 ffmpeg 参数。

    取值：auto（优先硬件，不可用时回退 CPU）、cpu、gpu、nvenc、qsv、amf。
    返回 ``{"args": [...], "codec": ..., "label": ..., "is_gpu": bool}``。
    """
    want = (encoder or "auto").strip().lower()
    available = _probe_encoders()

    order = []
    if want == "cpu":
        order = []
    elif want == "gpu":
        order = ["nvenc", "qsv", "amf"]
    elif want in GPU_ENCODERS:
        order = [want]
    elif want == "auto":
        order = ["nvenc", "qsv", "amf"]
    else:
        order = []

    for key in order:
        meta = available.get(key)
        if not meta:
            continue
        pfmt = meta.get("pix_fmt", pix_fmt)
        return {
            "args": build_encode_args(meta["name"], crf=crf, preset=preset, pix_fmt=pfmt),
            "codec": meta["name"],
            "label": "%s (GPU)" % meta["label"],
            "is_gpu": True,
        }

    return {
        "args": build_encode_args("libx264", crf=crf, preset=preset, pix_fmt=pix_fmt),
        "codec": "libx264",
        "label": "libx264 (CPU)",
        "is_gpu": False,
    }


def describe_encoders(encoder: str = "auto") -> str:
    """给 CLI 用的一行说明。"""
    available = _probe_encoders()
    if not available:
        return "未检测到可用的 GPU 编码器（将使用 CPU libx264）"
    names = ", ".join(meta["label"] for meta in available.values())
    chosen = resolve_encoder(encoder)
    return "可用 GPU 编码器：%s  →  实际使用：%s" % (names, chosen["label"])


class Cv2Writer:
    """没有 ffmpeg 时的兜底写入器（无音频）。"""

    def __init__(self, out_path: str, width: int, height: int, fps: float, **_):
        if not _HAVE_CV2:
            raise RuntimeError("既没有 ffmpeg 也没有 OpenCV，无法写视频")
        self.out_path = out_path
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.writer = cv2.VideoWriter(out_path, fourcc, float(fps), (int(width), int(height)))
        if not self.writer.isOpened():
            raise RuntimeError("cv2.VideoWriter 打不开输出：%s" % out_path)

    def write(self, frame_bgr: np.ndarray) -> None:
        self.writer.write(frame_bgr)

    def close(self) -> None:
        self.writer.release()

    def abort(self) -> None:
        try:
            self.writer.release()
        except Exception:
            pass


def make_writer(out_path: str, width: int, height: int, fps: float, crf: int = 18,
                preset: str = "medium", enc_args: Optional[list] = None):
    if find_ffmpeg():
        return FFmpegWriter(out_path, width, height, fps, crf=crf, preset=preset,
                            enc_args=enc_args)
    return Cv2Writer(out_path, width, height, fps)


def resize_bgr(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    """把 BGR uint8 帧缩放到 (width, height)。优先 OpenCV，缺失时用 PIL。"""
    if frame.shape[1] == width and frame.shape[0] == height:
        return frame
    if _HAVE_CV2:
        return cv2.resize(frame, (int(width), int(height)), interpolation=cv2.INTER_AREA)
    from PIL import Image

    img = Image.fromarray(frame[:, :, ::-1], mode="RGB")
    img = img.resize((int(width), int(height)), Image.LANCZOS)
    return np.asarray(img, dtype=np.uint8)[:, :, ::-1].copy()


# ---------------------------------------------------------------------------
# 音频
# ---------------------------------------------------------------------------
def extract_audio(src: str, temp_dir: Optional[str] = None) -> Optional[str]:
    """把源视频的音轨抽成 m4a；没有音轨或没有 ffmpeg 时返回 None。"""
    ff = find_ffmpeg()
    if not ff:
        return None
    tmp_dir = temp_dir or tempfile.mkdtemp(prefix="vhs_audio_")
    os.makedirs(tmp_dir, exist_ok=True)
    dest = os.path.join(tmp_dir, "audio.m4a")
    if os.path.exists(dest):
        return dest
    cmd = [
        ff, "-hide_banner", "-loglevel", "error", "-y",
        "-i", src, "-vn", "-c:a", "aac", "-b:a", "192k", dest,
    ]
    proc = _run(cmd)
    if proc.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    return None


def mux_audio(video_path: str, audio_path: str, out_path: str) -> bool:
    """把音轨合回视频。成功返回 True。"""
    ff = find_ffmpeg()
    if not ff or not audio_path or not os.path.exists(audio_path):
        return False
    if os.path.abspath(video_path) == os.path.abspath(out_path):
        return False
    cmd = [
        ff, "-hide_banner", "-loglevel", "error", "-y",
        "-i", video_path, "-i", audio_path,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        out_path,
    ]
    proc = _run(cmd)
    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return False
    return True


# ---------------------------------------------------------------------------
# 读取帧（用 OpenCV；不可用时走 ffmpeg rawvideo 管道）
# ---------------------------------------------------------------------------
class VideoFrameReader:
    """逐帧读取视频，统一产出 BGR uint8。"""

    def __init__(self, path: str, width: int = 0, height: int = 0, fps: float = 0.0):
        self.path = path
        self._proc = None
        self._cap = None
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps) if fps else 25.0

        if _HAVE_CV2:
            self._cap = cv2.VideoCapture(path)
            if not self._cap.isOpened():
                self._cap.release()
                self._cap = None
            else:
                if not self.width:
                    self.width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                if not self.height:
                    self.height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                if not fps:
                    self.fps = float(self._cap.get(cv2.CAP_PROP_FPS)) or 25.0
                return

        ff = find_ffmpeg()
        if not ff:
            raise RuntimeError("无法读取视频 %s：既没有可用的 OpenCV，也没有 ffmpeg" % path)
        if not (self.width and self.height):
            raise RuntimeError("读取 %s 失败：拿不到分辨率" % path)
        cmd = [
            ff, "-hide_banner", "-loglevel", "error",
            "-i", path, "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
        ]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )

    def read(self) -> Optional[np.ndarray]:
        if self._cap is not None:
            ok, frame = self._cap.read()
            return frame if ok else None
        frame_size = self.width * self.height * 3
        buf = self._proc.stdout.read(frame_size)
        if not buf or len(buf) < frame_size:
            return None
        return np.frombuffer(buf, np.uint8).reshape(self.height, self.width, 3).copy()

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._proc is not None:
            try:
                self._proc.stdout.close()
            except Exception:
                pass
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

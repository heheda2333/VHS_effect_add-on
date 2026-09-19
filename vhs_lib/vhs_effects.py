"""
vhs_effects.py -- VHS / 老式录像带效果核心库
================================================

纯函数式 + 可复用上下文设计：

    ctx = VHSContext(vhs_params, frame_shape)
    out = apply_vhs(frame_bgr, ctx, frame_index)

设计要点
--------
* 全程 float32 计算，值域 [0, 1]，避免 uint8 反复截断造成的色带。
* 重活（高斯模糊、噪声、暗角、OSD 文字）都做了缓存：
  - 高斯模糊优先使用 OpenCV（若可用），否则退化到 scipy，再退化到 PIL。
  - 静态图层的随机图案（蒙版、暗角、扫描线基底）在 VHSContext 里只算一次。
* 时间维度的失真（抖动、tracking 带、dropout）由 frame_index 驱动，
  所以视频播放时是"动"的，而单张照片取 frame_index 的随机相位。

作者注：所有可调参数集中在 VHSParams，预设见 PRESETS。
"""

from __future__ import annotations

import datetime
import math
import os
import sys
from dataclasses import dataclass, field, asdict, replace
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# 优先使用项目自带的 libs/ 目录（本工具所有依赖都放在项目内部，不污染全局环境）
# ---------------------------------------------------------------------------
_LIBS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
if os.path.isdir(_LIBS) and _LIBS not in sys.path:
    sys.path.insert(0, _LIBS)

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# 可选依赖的优雅降级
# ---------------------------------------------------------------------------
try:  # OpenCV 只用来做高斯模糊/缩放，headless 版即可
    import cv2

    _HAVE_CV2 = True
except Exception:  # pragma: no cover
    cv2 = None
    _HAVE_CV2 = False

try:
    from scipy.ndimage import gaussian_filter

    _HAVE_SCIPY = True
except Exception:  # pragma: no cover
    gaussian_filter = None
    _HAVE_SCIPY = False


HAVE_CV2 = _HAVE_CV2
HAVE_SCIPY = _HAVE_SCIPY


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
@dataclass
class VHSParams:
    """VHS 效果强度参数。0 表示关闭该子效果，1 大致是"经典中等偏强"。"""

    # ---- 色彩 ----
    chroma_bleed: float = 0.85      # 色度横向拖尾 / 串色（VHS 最标志性的特征）
    chroma_bleed_offset: int = 3    # 红/蓝色度分离的像素偏移量
    chroma_subsample: float = 0.6   # 色度降采样程度（VHS 色度带宽极低）

    # ---- 信号劣化 ----
    luma_noise: float = 0.75        # 亮度噪点
    chroma_noise: float = 0.55      # 色度噪点（彩色雪花）
    edge_noise: float = 0.6         # 边缘处噪点增强（脏边）
    scanlines: float = 0.5          # 扫描线
    scanline_period: float = 2.0    # 扫描线周期(像素)，2 = 隔行
    vignette: float = 0.4           # 暗角

    # ---- 时间基准误差 (TBC error) ----
    jitter: float = 0.55            # 画面整体左右抖动
    line_jitter: float = 0.6        # 每一行独立的水平抖动（wobble/撕裂）
    vertical_roll: float = 0.25     # 垂直滚动偏移

    # ---- 磁头切换 / 追踪失真 ----
    head_switch: float = 0.7        # 画面底部磁头切换噪声带
    head_switch_height: float = 0.035   # 噪声带高度（占画面比例）
    tracking_band: float = 0.55     # 纵向滚动的 tracking 失真带
    dropout: float = 0.35           # 随机白/黑掉磁短横线
    dropout_lines: float = 0.5     # 每帧平均掉磁线条数

    # ---- 光学 ----
    softness: float = 0.6           # 低带宽造成的柔化
    bloom: float = 0.45             # 高光泛光/晕染
    sharpen: float = 0.25           # 后期轻微锐化（模拟录像机边缘增强）
    saturation: float = 1.22        # 饱和度
    contrast: float = 1.05          # 对比度
    brightness: float = 1.0         # 亮度
    tint_green: float = 0.012       # 轻微偏绿（老带子色偏）
    tint_sepia: float = 0.0         # 偏棕，0=关闭

    # ---- OSD 文字 ----
    osd: bool = True
    osd_rec: bool = True            # 左上角 ● REC
    osd_play: bool = True           # 右上角 ▶ PLAY  /  SP 速度
    osd_timestamp: bool = True      # 右下角日期时间
    osd_timestamp_text: str = ""    # 固定文字（设置后不再进行时间推演）
    osd_opacity: float = 0.82
    osd_glitch: float = 0.5         # 文字抖动幅度
    osd_ghost: float = 0.35         # 色差重影强度；过大时红色标记会偏紫
    osd_scale: float = 1.0          # 字号 / 图标缩放
    osd_rec_blink: float = 1.4      # REC 圆点闪烁频率（Hz）；0 = 常亮
    osd_rec_color: str = "#FF2E2E"  # REC 圆点颜色

    # ---- 时间戳推演 ----
    # timestamp_start 可以写成很多种形式，程序会自动识别：
    #     "1994-06-11 12:00:00" / "1994/6/11 12:00" / "12:00:00" / "12:00"
    #     中文："1994年6月11日 12点00分00秒" / "12点30分"
    #     纯数字："19940611120000" / "120000"
    #     也接受 8 位日期："19940611"
    timestamp_start: str = "1994-06-11 12:00:00"
    timestamp_offset: float = 0.0   # 在起始时间上再加/减多少秒
    timestamp_mode: str = "auto"    # auto=照片固定、视频递增 | fixed | advance
    timestamp_speed: float = 1.0    # 时间流逝倍率，2.0 = 快一倍

    # ---- 运行控制 ----
    seed: int = 20240517
    max_dim: int = 1920             # 照片处理的最长边；视频默认走 target_height
    letterbox: bool = False          # 输出时是否保留 4:3 黑边（老电视感）

    def merged(self, **kwargs) -> "VHSParams":
        return replace(self, **{k: v for k, v in kwargs.items() if v is not None})

    def scaled(self, factor: float) -> "VHSParams":
        """整体强度缩放：factor=0 等于不处理，1 等于原样，2 等于加倍。"""
        f = float(factor)
        out = {}
        for name, value in asdict(self).items():
            if name in ("seed", "max_dim", "osd", "osd_rec", "osd_play",
                        "osd_timestamp", "osd_timestamp_text", "osd_scale",
                        "letterbox", "chroma_bleed_offset", "scanline_period",
                        "timestamp_start", "timestamp_mode", "osd_rec_color"):
                continue
            if isinstance(value, bool):
                out[name] = value
            elif isinstance(value, (int, float)):
                if name == "contrast":
                    out[name] = 1.0 + (float(value) - 1.0) * f
                elif name == "saturation":
                    out[name] = 1.0 + (float(value) - 1.0) * f
                elif name == "brightness":
                    out[name] = 1.0 + (float(value) - 1.0) * f
                else:
                    out[name] = float(value) * f
        return replace(self, **out)


# 预设：从轻微到"泡水带子"
PRESETS = {
    "light": VHSParams(
        chroma_bleed=0.35, chroma_subsample=0.25, luma_noise=0.3, chroma_noise=0.2,
        edge_noise=0.25, scanlines=0.22, vignette=0.18, jitter=0.2, line_jitter=0.22,
        vertical_roll=0.1, head_switch=0.28, tracking_band=0.18, dropout=0.1,
        dropout_lines=0.15, softness=0.3, bloom=0.2, sharpen=0.15,
        saturation=1.1, contrast=1.03, tint_green=0.006,
    ),
    "classic": VHSParams(),  # 默认值即经典
    "heavy": VHSParams(
        chroma_bleed=1.25, chroma_bleed_offset=5, chroma_subsample=0.95,
        luma_noise=1.15, chroma_noise=0.95, edge_noise=0.9, scanlines=0.72,
        vignette=0.6, jitter=0.85, line_jitter=0.95, vertical_roll=0.4,
        head_switch=1.0, tracking_band=0.95, dropout=0.65, dropout_lines=1.2,
        softness=0.95, bloom=0.7, sharpen=0.35, saturation=1.32, contrast=1.1,
        tint_green=0.02,
    ),
    "worn": VHSParams(
        chroma_bleed=1.7, chroma_bleed_offset=7, chroma_subsample=1.0,
        luma_noise=1.5, chroma_noise=1.3, edge_noise=1.1, scanlines=0.85,
        vignette=0.85, jitter=1.1, line_jitter=1.3, vertical_roll=0.6,
        head_switch=1.3, head_switch_height=0.06, tracking_band=1.35,
        dropout=1.0, dropout_lines=1.8, softness=1.2, bloom=0.85, sharpen=0.3,
        saturation=1.25, contrast=1.08, brightness=0.97, tint_green=0.028,
        tint_sepia=0.1, osd_glitch=0.9,
    ),
    "clean_tv": VHSParams(
        osd=True, luma_noise=0.25, chroma_noise=0.15, scanlines=0.35,
        chroma_bleed=0.4, jitter=0.15, line_jitter=0.12, vertical_roll=0.0,
        head_switch=0.2, tracking_band=0.1, dropout=0.0, softness=0.35,
        bloom=0.25, vignette=0.25, saturation=1.15,
    ),
}

# 支持的文件扩展名（小写，含点号）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v",
              ".mpg", ".mpeg", ".webm", ".ts"}

PRESET_ALIASES = {
    "off": "light", "none": "light", "轻微": "light", "轻度": "light",
    "normal": "classic", "default": "classic", "经典": "classic", "标准": "classic",
    "strong": "heavy", "重度": "heavy", "强": "heavy",
    "worn": "worn", "破损": "worn", "坏带": "worn",
    "tv": "clean_tv", "clean": "clean_tv", "电视": "clean_tv",
}


def resolve_preset(name: str) -> VHSParams:
    key = (name or "").strip().lower()
    key = PRESET_ALIASES.get(key, key)
    if key not in PRESETS:
        raise KeyError(
            "未知预设 %r，可选：%s" % (name, ", ".join(sorted(PRESETS)))
        )
    return replace(PRESETS[key])


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _to_float(arr: np.ndarray) -> np.ndarray:
    """uint8 -> [0,1] float32。已经是浮点的输入原样透传。"""
    if arr.dtype == np.uint8:
        return arr.astype(np.float32) / 255.0
    if arr.dtype == np.float64:
        return arr.astype(np.float32)
    return arr.astype(np.float32, copy=False)


def _to_uint8(arr: np.ndarray) -> np.ndarray:
    """[0,1] float -> uint8。已经是 uint8 的输入原样返回（幂等，避免二次放大）。"""
    if arr.dtype == np.uint8:
        return arr
    return (np.clip(arr, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def gaussian_blur(arr: np.ndarray, sigma: float) -> np.ndarray:
    """对 (H, W) 或 (H, W, C) float32 数组做高斯模糊，自动选择后端。"""
    if sigma <= 0:
        return arr
    if _HAVE_CV2:
        # OpenCV 对多通道最省事，这里统一按 2D 逐通道处理以保证一致性
        if arr.ndim == 2:
            return cv2.GaussianBlur(arr, (0, 0), sigmaX=sigma, borderType=cv2.BORDER_REFLECT)
        out = np.empty_like(arr)
        for c in range(arr.shape[2]):
            out[..., c] = cv2.GaussianBlur(
                arr[..., c], (0, 0), sigmaX=sigma, borderType=cv2.BORDER_REFLECT
            )
        return out
    if _HAVE_SCIPY:
        mode = "reflect"
        if arr.ndim == 2:
            return gaussian_filter(arr, sigma, mode=mode).astype(np.float32, copy=False)
        return gaussian_filter(arr, (sigma, sigma, 0), mode=mode).astype(np.float32, copy=False)
    # 最后兜底：用 PIL 的 BoxBlur 近似
    return _pil_blur(arr, sigma)


def _pil_blur(arr: np.ndarray, sigma: float) -> np.ndarray:
    """纯 PIL 兜底模糊。PIL 只认 (H,W) 灰度或 (H,W,3/4)，所以先把单通道压平。"""
    single = arr.ndim == 2
    if single:
        src2d = arr
    elif arr.shape[2] == 1:
        single = True
        src2d = arr[..., 0]
    else:
        src2d = None

    radius = max(1, int(round(abs(float(sigma)) * 1.6)))

    if single:
        h, w = src2d.shape
        img = Image.fromarray(_to_uint8(src2d), mode="L")
        blurred = img.filter(_pil_boxblur(radius))
        if blurred.mode != "L":
            blurred = blurred.convert("L")
        return _to_float(np.asarray(blurred, dtype=np.uint8)).reshape(h, w)

    h, w, c = arr.shape
    if c == 3:
        img = Image.fromarray(_to_uint8(arr), mode="RGB")
        blurred = img.filter(_pil_boxblur(radius))
        if blurred.mode != "RGB":
            blurred = blurred.convert("RGB")
        return _to_float(np.asarray(blurred, dtype=np.uint8)).reshape(h, w, 3)

    # 4 通道以上：逐通道处理
    out = np.empty_like(arr)
    for ch in range(c):
        out[..., ch] = _pil_blur(arr[..., ch], sigma)
    return out


def _pil_boxblur(radius: int):
    from PIL import ImageFilter

    return ImageFilter.BoxBlur(radius)


def resize_float(arr: np.ndarray, size: Tuple[int, int]) -> np.ndarray:
    """size = (width, height)，对 float 数组做双线性缩放。"""
    w, h = size
    if arr.shape[1] == w and arr.shape[0] == h:
        return arr
    if _HAVE_CV2:
        interp = cv2.INTER_AREA if (w < arr.shape[1] or h < arr.shape[0]) else cv2.INTER_LINEAR
        if arr.ndim == 2:
            return cv2.resize(arr, (w, h), interpolation=interp)
        return cv2.resize(arr, (w, h), interpolation=interp)
    img = Image.fromarray(_to_uint8(arr))
    img = img.resize((w, h), Image.BILINEAR)
    if arr.ndim == 2:
        img = img.convert("L")
        return _to_float(np.asarray(img, dtype=np.uint8)).reshape(h, w)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return _to_float(np.asarray(img, dtype=np.uint8)).reshape(h, w, 3)


def nearest_upscale(arr: np.ndarray, width: int, height: int) -> np.ndarray:
    """把低分辨率噪声放大到全尺寸。用最近邻，保持颗粒感、不做插值平滑。"""
    h, w = arr.shape[:2]
    if h == height and w == width:
        return arr
    if _HAVE_CV2:
        interp = cv2.INTER_NEAREST
        return cv2.resize(arr, (int(width), int(height)), interpolation=interp)
    single = arr.ndim == 2
    img = Image.fromarray(_to_uint8(arr), mode="L" if single else "RGB")
    img = img.resize((int(width), int(height)), Image.NEAREST)
    if single:
        return _to_float(np.asarray(img, dtype=np.uint8))
    return _to_float(np.asarray(img, dtype=np.uint8))


def rgb_to_ycbcr(rgb: np.ndarray) -> np.ndarray:
    """RGB[0,1] -> YCbCr，Y/Cb/Cr 都归一到大致 [0,1] 便于统一处理。"""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = -0.168736 * r - 0.331264 * g + 0.5 * b + 0.5
    cr = 0.5 * r - 0.418688 * g - 0.081312 * b + 0.5
    return np.stack((y, cb, cr), axis=-1).astype(np.float32, copy=False)


def ycbcr_to_rgb(ycbcr: np.ndarray) -> np.ndarray:
    y = ycbcr[..., 0]
    cb = ycbcr[..., 1] - 0.5
    cr = ycbcr[..., 2] - 0.5
    r = y + 1.402 * cr
    g = y - 0.344136 * cb - 0.714136 * cr
    b = y + 1.772 * cb
    return np.clip(np.stack((r, g, b), axis=-1), 0.0, 1.0).astype(np.float32, copy=False)


def _shift_horizontal(arr: np.ndarray, offset: int) -> np.ndarray:
    """整幅水平平移 offset 像素，空出来的部分用边缘值填充。"""
    if offset == 0:
        return arr
    out = np.empty_like(arr)
    if offset > 0:
        out[:, offset:] = arr[:, :-offset]
        out[:, :offset] = arr[:, :1]
    else:
        out[:, :offset] = arr[:, -offset:]
        out[:, offset:] = arr[:, -1:]
    return out


# ---------------------------------------------------------------------------
# 时间戳解析
# ---------------------------------------------------------------------------
DEFAULT_START_TIME = "1994-06-11 12:00:00"

# 覆盖常见的输入写法。顺序有意义：先试信息量最大的（带年月日时分秒）。
_TIME_PATTERNS = [
    # 1994-06-11 12:00:00 / 1994/6/11 12:00:00 / 1994.06.11 12:00
    r"(?P<Y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})[ T,]+"
    r"(?P<H>\d{1,2}):(?P<M>\d{1,2})(?::(?P<S>\d{1,2}))?",
    # 1994年6月11日 12点00分00秒 / 1994年6月11日12时0分
    r"(?P<Y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?[ ]*"
    r"(?P<H>\d{1,2})\s*[点时:]\s*(?P<M>\d{1,2})?\s*[分:]?\s*(?P<S>\d{1,2})?\s*秒?",
    # 19940611120000（14 位纯数字）
    r"^(?P<Y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})$",
    # 19940611（8 位纯数字，只有日期）
    r"^(?P<Y>\d{4})(?P<m>\d{2})(?P<d>\d{2})$",
    # 1994-06-11 / 1994年6月11日（只有日期）
    r"(?P<Y>\d{4})[-/.年](?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})",
    # 120000（6 位纯数字，只有时间）
    r"^(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})$",
    # 12:00:00 / 12:00 / 12点30分
    r"(?P<H>\d{1,2})\s*[点时:]\s*(?P<M>\d{1,2})?\s*[分:]?\s*(?P<S>\d{1,2})?\s*秒?",
]

_TIME_WORDS = {"now": None, "今天": None, "现在": None}


def parse_start_time(text: str, default: Optional[datetime.datetime] = None) -> datetime.datetime:
    """把用户输入的时间字符串解析成 datetime。

    识别不了的部分沿用 default 的对应字段（默认 1994-06-11 12:00:00）。
    抛出 ValueError 表示完全无法解析。
    """
    import re

    base = default or datetime.datetime(1994, 6, 11, 12, 0, 0)
    if text is None:
        return base
    raw = str(text).strip()
    if not raw:
        return base
    if raw.lower() in _TIME_WORDS:
        return datetime.datetime.now().replace(microsecond=0)

    for pattern in _TIME_PATTERNS:
        m = re.search(pattern, raw, flags=re.IGNORECASE)
        if not m:
            continue
        g = m.groupdict()
        try:
            year = int(g["Y"]) if g.get("Y") else base.year
            month = int(g["m"]) if g.get("m") else base.month
            day = int(g["d"]) if g.get("d") else base.day
            hour = int(g["H"]) if g.get("H") else base.hour
            minute = int(g["M"]) if g.get("M") else 0
            second = int(g["S"]) if g.get("S") else 0
            # 支持 "19点" 这类 12 小时制写法
            if hour > 23 or month > 12 or day > 31 or minute > 59 or second > 59:
                continue
            return datetime.datetime(year, month, day, hour, minute, second)
        except (ValueError, TypeError):
            continue

    raise ValueError(
        "无法识别的时间格式：%r\n"
        "  支持示例：1994-06-11 12:00:00 / 1994/6/11 12:00 / 12:00:00 / 12:00\n"
        "            1994年6月11日 12点00分00秒 / 19940611120000 / 19940611 / 120000 / now"
        % text
    )


def describe_time(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_hex_color(text: str, fallback=(255, 46, 46)) -> Tuple[int, int, int]:
    """解析 "#RRGGBB" / "RRGGBB" / "r,g,b"，失败时返回 fallback。"""
    import re

    if not text:
        return fallback
    raw = str(text).strip()
    m = re.search(r"^#?([0-9a-fA-F]{6})$", raw)
    if m:
        v = int(m.group(1), 16)
        return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)
    m = re.search(r"^\s*(\d{1,3})\s*[,\s]\s*(\d{1,3})\s*[,\s]\s*(\d{1,3})\s*$", raw)
    if m:
        return tuple(max(0, min(255, int(g))) for g in m.groups())
    return fallback


# 字体路径探测结果，进程内共享
_FONT_CACHE = {"probed": False, "path": ""}

# OSD 元素类型与配色
OSD_TEXT = "text"
OSD_REC_DOT = "rec_dot"
# 家用录像机 OSD 的经典淡绿白
OSD_TEXT_COLOR = (232, 255, 232)


# ---------------------------------------------------------------------------
# 运行上下文：缓存一切与单帧无关的东西
# ---------------------------------------------------------------------------
class VHSContext:
    """绑定「一组参数 + 一个分辨率」，缓存所有静态图层。"""

    def __init__(self, params: VHSParams, shape: Tuple[int, int], fps: float = 25.0,
                 start_time: Optional[datetime.datetime] = None,
                 advancing: bool = True):
        self.p = params
        self.h, self.w = int(shape[0]), int(shape[1])
        self.fps = float(fps) if fps and fps > 0 else 25.0
        self.rng = np.random.default_rng(params.seed)

        # 时间戳推演：start_time 优先于 params.timestamp_start
        if start_time is not None:
            parsed = start_time
        else:
            try:
                parsed = parse_start_time(params.timestamp_start)
            except ValueError:
                parsed = parse_start_time(DEFAULT_START_TIME)
        self.start_time = parsed + datetime.timedelta(seconds=float(params.timestamp_offset))
        self.advancing = bool(advancing)

        yy, xx = np.mgrid[0:self.h, 0:self.w].astype(np.float32)
        self._yy = yy
        self._xx = xx

        # 稳定的"每行"随机量 -> 行抖动 / 扫描线相位 / 行亮度差
        row_rng = np.random.default_rng(params.seed + 101)
        self.row_rand = row_rng.standard_normal(self.h).astype(np.float32)
        self.row_uniform = row_rng.random(self.h).astype(np.float32)
        self.col_rand = row_rng.standard_normal(self.w).astype(np.float32)

        # 暗角
        self.vignette_mask = self._build_vignette()

        # 扫描线剖面
        self.scanline_profile = self._build_scanline_profile()

        # 亮度掩膜用的低频噪声（脏边/颗粒）
        base = row_rng.standard_normal((self.h, self.w)).astype(np.float32)
        self.grain_low = gaussian_blur(base, 1.2)
        del base

        # 噪声池：噪点在逐帧之间本就相互独立，预先算好一批循环使用，
        # 与逐帧即时生成在观感上等价，但省去了逐帧最昂贵的生成步骤。
        self._build_noise_pool()

        # OSD 元素表与时间戳缓存
        self._osd_static: Optional[list] = None
        self._osd_stamp_cache: dict = {}
        # 饱和度计算用的可复用亮度缓冲，避免每帧分配 (H, W) 临时数组
        self._luma_buf: Optional[np.ndarray] = None

        # 底部磁头切换带的横向偏移剖面（固定图案 + 随时间漂移）
        hs = max(2, int(round(self.h * params.head_switch_height)))
        self.head_switch_rows = hs

    # -- 噪声池 ------------------------------------------------------------
    def _build_noise_pool(self, size: int = 40) -> None:
        """在四分之一分辨率上生成 size 组噪声，使用时以最近邻放大。

        采用四分之一分辨率的原因：
          1) 内存占用与生成时间均降至约十六分之一
          2) VHS 的颗粒尺度本就大于单像素，放大后更接近真实观感
        """
        cw = max(2, int(round(self.w / 4.0)))
        ch = max(2, int(round(self.h / 4.0)))
        rng = np.random.default_rng(self.p.seed + 7717)
        self.noise_pool_size = int(size)
        self._noise_luma = rng.standard_normal((size, ch, cw)).astype(np.float32)
        # 低频块状颗粒（磁粉不均）：同样在 1/4 分辨率先模糊再放大，
        # 存全分辨率会吃掉几百 MB 内存，没必要。
        self._noise_coarse = gaussian_blur(self._noise_luma, 1.4)
        # 彩色噪点：去均值，避免退化成单纯的亮度噪声
        cn = rng.standard_normal((size, ch, cw, 3)).astype(np.float32)
        self._noise_chroma = cn - cn.mean(axis=3, keepdims=True) * 0.7
        del cn

    # -- 静态图层 ----------------------------------------------------------
    def _build_vignette(self) -> np.ndarray:
        if self.p.vignette <= 0:
            return np.ones((self.h, self.w), np.float32)
        cy, cx = (self.h - 1) / 2.0, (self.w - 1) / 2.0
        ny = (self._yy - cy) / max(cy, 1.0)
        nx = (self._xx - cx) / max(cx, 1.0)
        # 老电视的 CRT 暗角是椭圆形的，边缘衰减比四角柔和
        r = np.sqrt(nx * nx * 0.85 + ny * ny * 1.0)
        r = np.clip(r / 1.18, 0.0, 1.0)
        amount = float(np.clip(self.p.vignette, 0.0, 2.0))
        mask = 1.0 - amount * (r ** 2.3) * 0.75
        return np.clip(mask, 0.25, 1.0).astype(np.float32)

    def _build_scanline_profile(self) -> np.ndarray:
        """返回 (H, 1) 的乘性亮度剖面。"""
        period = max(1.5, float(self.p.scanline_period))
        phase = 2.0 * math.pi * (self._yy / period)
        depth = float(np.clip(self.p.scanlines, 0.0, 2.0))
        # 主扫描线 + 轻微的行间渗色
        prof = 1.0 - depth * 0.16 * (0.5 + 0.5 * np.sin(phase))
        prof -= depth * 0.05 * (0.5 + 0.5 * np.sin(phase * 0.5 + 0.9))
        # 每行随机的亮度差（真实的模拟信号行间不一致）
        prof = prof - self.row_rand[:, None] * depth * 0.022
        return prof.astype(np.float32)

    # -- 每帧生成 ----------------------------------------------------------
    def temporal_rng(self, frame_index: int) -> np.random.Generator:
        return np.random.default_rng((self.p.seed * 7919 + int(frame_index) * 104729) & 0xFFFFFFFF)

    # -- OSD ---------------------------------------------------------------
    @staticmethod
    def _font_search_dirs() -> list:
        """收集当前系统可能存放字体的目录（跨平台，不写死具体文件路径）。"""
        dirs = []
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
        if windir:
            dirs.append(os.path.join(windir, "Fonts"))
            local = os.environ.get("LOCALAPPDATA")
            if local:
                # 用户自行安装的字体
                dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
        dirs += [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/Library/Fonts"),
            "/Library/Fonts",
            "/System/Library/Fonts",
        ]
        return [d for d in dirs if d and os.path.isdir(d)]

    @classmethod
    def _find_font_file(cls, names: Tuple[str, ...]) -> Optional[str]:
        """在系统字体目录中按文件名查找字体；找不到返回 None。

        仅按文件名搜索，不假设具体的字体安装路径，因此更换运行环境通常无需改动。
        """
        for directory in cls._font_search_dirs():
            for root, _dirs, files in os.walk(directory):
                lowered = {f.lower(): f for f in files}
                for want in names:
                    hit = lowered.get(want.lower())
                    if hit:
                        return os.path.join(root, hit)
        return None

    # 按优先级排列的等宽字体候选（等宽字形更接近录像机 OSD 的观感）
    _MONO_FONTS = (
        "lucon.ttf", "consola.ttf", "cour.ttf", "DejaVuSansMono.ttf",
        "LiberationMono-Regular.ttf", "NotoSansMono-Regular.ttf",
        "Menlo.ttc", "Courier New.ttf",
    )
    _ANY_FONTS = (
        "arial.ttf", "segoeui.ttf", "DejaVuSans.ttf",
        "LiberationSans-Regular.ttf", "NotoSans-Regular.ttf", "Helvetica.ttc",
    )

    def _load_font(self, size: int):
        """返回 (字体对象, 是否支持任意字号)。

        优先等宽字体；其次任意无衬线字体；均不可用时使用 Pillow 内置位图字体
        （只有单一字号，调用方会按倍数放大）。字体路径在进程内只探测一次。
        """
        if not _FONT_CACHE["probed"]:
            _FONT_CACHE["probed"] = True
            _FONT_CACHE["path"] = (self._find_font_file(self._MONO_FONTS)
                                   or self._find_font_file(self._ANY_FONTS) or "")
        path = _FONT_CACHE["path"]
        if path:
            try:
                return ImageFont.truetype(path, size), True
            except Exception:
                _FONT_CACHE["path"] = ""
        try:
            return ImageFont.load_default(), False
        except Exception:
            return None, False

    def _timestamp_text(self, frame_index: int) -> str:
        """当前帧应该显示的日期时间文字。"""
        if self.p.osd_timestamp_text:
            return self.p.osd_timestamp_text
        if not self.advancing or self.p.timestamp_mode == "fixed":
            secs = 0.0
        else:
            secs = (frame_index / self.fps) * float(self.p.timestamp_speed)
        moment = self.start_time + datetime.timedelta(seconds=secs)
        return moment.strftime("%Y-%m-%d  %H:%M:%S")

    def _osd_metrics(self) -> dict:
        p = self.p
        scale = max(0.35, float(p.osd_scale))
        base_size = max(9, int(round(self.h / 42.0 * scale)))
        small_size = max(8, int(round(base_size * 0.92)))
        font, scalable = self._load_font(base_size)
        small_font, _ = self._load_font(small_size)
        return {
            "base_size": base_size,
            "small_size": small_size,
            "font": font,
            "small_font": small_font,
            "scalable": scalable,
            "margin_x": max(8, int(round(self.w * 0.022))),
            "margin_y": max(6, int(round(self.h * 0.03))),
        }

    def _text_size(self, text: str, fnt) -> Tuple[int, int]:
        bbox = fnt.getbbox(text) if not isinstance(fnt, str) else None
        if bbox:
            return max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
        tmp = Image.new("L", (1, 1))
        bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=fnt)
        return max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])

    def _blit_glyph(self, glyph: Image.Image, xy, elements: list, col,
                    kind: str = OSD_TEXT) -> None:
        """把一个字形登记进 OSD 元素表（不预先渲染整帧图层）。

        OSD 只占画面极小一块，因此刻意不构建全分辨率图层：这里仅记录
        "字形图 + 位置 + 颜色 + 类型"，合成时只在小矩形内做混合。
        """
        elements.append((glyph, int(round(xy[0])), int(round(xy[1])),
                         tuple(col), kind))

    def _render_text_glyph(self, text: str, fnt, upscale_to: int,
                           col) -> Optional[Image.Image]:
        """把文字渲染成 L 字形图。内置位图字体没有字号概念，按倍率硬放大。"""
        if fnt is None:
            return None
        if isinstance(fnt, ImageFont.FreeTypeFont):
            tw, th = self._text_size(text, fnt)
            glyph = Image.new("L", (tw + 4, th + 6), 0)
            ImageDraw.Draw(glyph).text((2, 2), text, font=fnt, fill=255)
            return glyph
        # 内置位图字体：先在小画布上画，再整数倍最近邻放大，保住像素感
        tw, th = self._text_size(text, fnt)
        small = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(small).text((0, 0), text, font=fnt, fill=255)
        up = max(1, int(round(upscale_to / 11.0)))
        return small.resize((tw * up, th * up), Image.NEAREST)

    def _render_play_arrow(self, height_px: int, col) -> Image.Image:
        """手绘的播放三角。

        采用多边形绘制的必要性：``\\u25b6`` 在部分系统自带字体中不含对应字形，
        Pillow 会将其绘制为一个方框，因此不依赖字体更为可靠，
        同时多边形的形状也更接近录像机的屏显观感。
        """
        h = max(6, int(height_px))
        w = max(5, int(round(h * 0.80)))
        ss = 4  # 4 倍超采样后缩小，得到干净的斜边
        img = Image.new("L", (w * ss, h * ss), 0)
        ImageDraw.Draw(img).polygon(
            [(0, 0), (w * ss - 1, (h * ss) // 2), (0, h * ss - 1)], fill=255
        )
        return img.resize((w, h), Image.LANCZOS)

    def _apply_osd(self, img: np.ndarray, frame_index: int, rng) -> np.ndarray:
        """把小尺寸的 OSD 字形直接混合进画面。

        只改动每个字形所在的小矩形，全帧数组一次都不碰 —— 这是 1080p 下
        OSD 从 0.29s/帧 降到 <0.005s/帧 的关键。
        """
        p = self.p
        if not p.osd:
            return img

        elements = list(self._build_osd_elements())
        if p.osd_timestamp:
            elements.extend(self._get_timestamp_elements(frame_index))
        if not elements:
            return img

        g = float(p.osd_glitch)
        if g > 0:
            dx = int(round(rng.standard_normal() * g * 0.9))
            dy = int(round(rng.standard_normal() * g * 0.5))
            ghost_dx = int(round(1 + g * 1.6)) if g > 0.2 else 0
        else:
            dx = dy = 0
            ghost_dx = 0

        aa = float(np.clip(p.osd_opacity, 0.0, 1.0))
        ghost = float(np.clip(p.osd_ghost, 0.0, 1.0))
        blink_gain = self._rec_blink_gain(frame_index)
        # 形状上用一次 copy 换掉"全帧三次运算"，非常划算
        out = img.copy()

        for glyph, gx, gy, col, kind in elements:
            gain = aa
            if kind == OSD_REC_DOT:
                gain *= blink_gain
                if gain <= 0.002:
                    continue
            self._blend_patch(out, glyph, gx + dx, gy + dy, col, gain)
            if ghost_dx and ghost > 0.002:
                # 色差重影沿用字形自身的颜色。若改用固定的洋红等颜色，
                # 红色 REC 圆点会被染成紫色，故此处必须复用原色。
                self._blend_patch(out, glyph, gx + dx + ghost_dx, gy + dy,
                                  col, gain * ghost)
        return out

    def _rec_blink_gain(self, frame_index: int) -> float:
        """REC 圆点的闪烁系数，取值 0..1。

        照片只处理单帧（frame_index 恒为 0），此时返回 1.0，圆点保持常亮；
        视频逐帧调用，圆点按 osd_rec_blink 指定的频率闪烁。
        """
        rate = float(self.p.osd_rec_blink)
        if rate <= 0 or not self.advancing:
            return 1.0
        period = self.fps / max(0.05, rate)          # 一个明暗周期占多少帧
        phase = (frame_index % period) / period      # 0..1
        # 亮 65% / 暗 35%，接近真实摄录设备的录制指示
        duty = 0.65
        if phase < duty:
            return 1.0
        # 熄灭段用余弦平滑过渡，避免生硬的闪烁
        t = (phase - duty) / (1.0 - duty)
        return float(0.5 - 0.5 * math.cos(math.pi * 2.0 * t))

    def _blend_patch(self, out: np.ndarray, glyph: Image.Image, x: int, y: int,
                     col, gain: float) -> None:
        """把一个字形以 col 颜色、gain 强度混合到 out 的对应小矩形里。

        ``col`` 为 0..255 的 RGB 三元组。``out`` 是 RGB 顺序的工作缓冲
        （float32 值域 [0,1]，或 uint8 值域 [0,255]），两种都要能处理。
        注意此处不做通道翻转：BGR 顺序只出现在 process() 的输入输出边界上。
        """
        if gain <= 0.002:
            return
        gh, gw = glyph.height, glyph.width
        # 目标区域与画面求交，越界部分裁掉
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + gw), min(self.h, y + gh)
        if x1 <= x0 or y1 <= y0:
            return
        # 字形坐标系里对应的子矩形
        sx0, sy0 = x0 - x, y0 - y
        sx1, sy1 = sx0 + (x1 - x0), sy0 + (y1 - y0)

        sub = np.asarray(glyph, dtype=np.uint8)[sy0:sy1, sx0:sx1]
        alpha = ((sub.astype(np.float32) / 255.0) ** 0.85 * gain)[..., None]

        # col 为 0..255；转换为 0..1 以便与 float 缓冲混合。
        # 绝不能乘 255：缓冲是 [0,1]，混入 0..255 的量会直接过曝为白色。
        colour = np.array(col, np.float32) / 255.0

        region = out[y0:y1, x0:x1].astype(np.float32)
        region *= (1.0 - alpha)
        region += colour * alpha
        # 写回时按目标 dtype 还原量纲
        if out.dtype == np.uint8:
            out[y0:y1, x0:x1] = np.clip(region * 255.0, 0.0, 255.0).astype(np.uint8)
        else:
            out[y0:y1, x0:x1] = np.clip(region, 0.0, 1.0).astype(out.dtype)

    def _build_osd_elements(self) -> list:
        """构建固定不变的 OSD 元素：左上 ● REC、右上 ▶ PLAY / SP。

        返回 [(字形图, x, y, RGB 颜色, 元素类型), ...]，
        只在参数或分辨率变化时重建，逐帧复用。
        """
        if self._osd_static is not None:
            return self._osd_static

        p = self.p
        met = self._osd_metrics()
        base_size = met["base_size"]
        font = met["font"]
        small_font = met["small_font"]
        margin_x, margin_y = met["margin_x"], met["margin_y"]
        col = OSD_TEXT_COLOR

        elements = []

        if p.osd_rec:
            r = max(4, int(base_size * 0.32))
            # 圆点同样做成字形图，与文字走同一套混合流程
            ss = 3
            dot = Image.new("L", (2 * r * ss, 2 * r * ss), 0)
            ImageDraw.Draw(dot).ellipse([0, 0, 2 * r * ss - 1, 2 * r * ss - 1], fill=240)
            dot = dot.resize((2 * r, 2 * r), Image.LANCZOS)
            dot_col = parse_hex_color(p.osd_rec_color)
            self._blit_glyph(dot, (margin_x, margin_y - r + 2), elements,
                             dot_col, OSD_REC_DOT)
            glyph = self._render_text_glyph("REC", font, base_size, col)
            if glyph is not None:
                self._blit_glyph(glyph, (margin_x + 2 * r + 6, margin_y - r),
                                 elements, col, OSD_TEXT)

        if p.osd_play:
            # 右对齐排版：[三角] 间隙 PLAY
            text_w, text_h = self._text_size("PLAY", font)
            gap = max(3, int(base_size * 0.28))
            tri_h = max(6, int(text_h * 0.92))
            tri = self._render_play_arrow(tri_h, col)
            total_w = tri.width + gap + text_w
            x0 = self.w - margin_x - total_w
            self._blit_glyph(tri, (x0, margin_y + (text_h - tri.height) // 2),
                             elements, col, OSD_TEXT)
            glyph = self._render_text_glyph("PLAY", font, base_size, col)
            if glyph is not None:
                self._blit_glyph(glyph, (x0 + tri.width + gap - 2, margin_y - 2),
                                 elements, col, OSD_TEXT)

            glyph = self._render_text_glyph("SP", small_font, met["small_size"], col)
            if glyph is not None:
                self._blit_glyph(glyph, (self.w - margin_x - glyph.width,
                                         margin_y + base_size * 1.35),
                                 elements, col, OSD_TEXT)

        self._osd_static = elements
        return elements

    def _get_timestamp_elements(self, frame_index: int) -> list:
        """时间戳字形，按"秒"缓存。

        真实录像机的 OSD 秒数也是每秒跳一次的，所以按秒缓存既忠实又省算力。
        """
        p = self.p
        text = self._timestamp_text(frame_index)
        if self.advancing and p.timestamp_mode != "fixed" and not p.osd_timestamp_text:
            secs = int((frame_index / self.fps) * float(p.timestamp_speed))
        else:
            secs = 0
        key = (text, secs, self.w, self.h)
        cached = self._osd_stamp_cache.get(key)
        if cached is not None:
            return cached

        met = self._osd_metrics()
        glyph = self._render_text_glyph(text, met["font"], met["base_size"],
                                        OSD_TEXT_COLOR)
        if glyph is None:
            return []
        x = self.w - met["margin_x"] - glyph.width
        y = self.h - met["margin_y"] - glyph.height
        result = [(glyph, max(0, x), max(0, y), OSD_TEXT_COLOR, OSD_TEXT)]

        if len(self._osd_stamp_cache) > 240:
            self._osd_stamp_cache.clear()
        self._osd_stamp_cache[key] = result
        return result

    # -- 各子效果 ----------------------------------------------------------
    def _apply_chroma_bleed(self, ycc: np.ndarray, rng) -> np.ndarray:
        p = self.p
        if p.chroma_bleed <= 0:
            return ycc
        amt = float(p.chroma_bleed)
        # --- 低通：VHS 色度带宽只有约 0.5MHz，横向拖尾远大于纵向
        sigma_x = 1.0 + 3.4 * amt
        sigma_y = 0.5 + 1.1 * amt
        cb = ycc[..., 1]
        cr = ycc[..., 2]
        # 各向异性模糊：先横向后纵向
        cb_b = gaussian_blur(cb, sigma_x)
        cr_b = gaussian_blur(cr, sigma_x)
        if sigma_y > 0.05:
            if _HAVE_CV2:
                cb_b = cv2.GaussianBlur(cb_b, (0, 0), sigmaX=0.01, sigmaY=sigma_y,
                                        borderType=cv2.BORDER_REFLECT)
                cr_b = cv2.GaussianBlur(cr_b, (0, 0), sigmaX=0.01, sigmaY=sigma_y,
                                        borderType=cv2.BORDER_REFLECT)
            elif _HAVE_SCIPY:
                cb_b = gaussian_filter(cb_b, (sigma_y, 0.01), mode="reflect")
                cr_b = gaussian_filter(cr_b, (sigma_y, 0.01), mode="reflect")
        blend = min(1.0, 0.45 + 0.55 * amt)
        cb_m = cb * (1.0 - blend) + cb_b * blend
        cr_m = cr * (1.0 - blend) + cr_b * blend

        # --- 色度降采样（大块的彩色斑块，VHS 味的关键）
        sub = float(p.chroma_subsample)
        if sub > 0.02:
            factor = 1.0 + 5.0 * min(sub, 1.0)
            dw = max(2, int(round(self.w / factor)))
            dh = max(2, int(round(self.h / factor)))
            for ch, target in ((1, cb_m), (2, cr_m)):
                small = resize_float(target, (dw, dh))
                big = resize_float(small, (self.w, self.h))
                if ch == 1:
                    cb_m = cb_m * (1.0 - sub) + big * sub
                else:
                    cr_m = cr_m * (1.0 - sub) + big * sub

        # --- 色度横向错位：红/蓝分离（色边）
        off = int(round(p.chroma_bleed_offset * min(amt, 2.0)))
        if off > 0:
            cr_m = _shift_horizontal(cr_m, off)
            cb_m = _shift_horizontal(cb_m, -off)

        out = ycc.copy()
        out[..., 1] = cb_m
        out[..., 2] = cr_m
        return out

    def _apply_tbc_error(self, img: np.ndarray, frame_index: int, rng) -> np.ndarray:
        """TBC / 时基误差：整体抖动 + 每行独立抖动 + 垂直滚动。"""
        p = self.p
        h, w = self.h, self.w
        drift = 0.0

        # 整体左右抖动（低频漂移 + 高频抖动）
        if p.jitter > 0:
            t = frame_index / self.fps
            slow = math.sin(t * 0.7) * 0.5 + math.sin(t * 1.9 + 1.1) * 0.3
            amp = p.jitter * w * 0.006
            drift = slow * amp + rng.standard_normal() * amp * 0.7

        # 垂直滚动
        v_shift = 0
        if p.vertical_roll > 0:
            t = frame_index / self.fps
            v_shift = int(round(math.sin(t * 0.45) * p.vertical_roll * h * 0.02))
            if v_shift:
                img = np.roll(img, v_shift, axis=0)

        # 每行独立抖动 —— 用 np.take 一次性重采样所有行，避免 Python 循环
        if p.line_jitter > 0 or abs(drift) > 0.01:
            base = np.arange(w, dtype=np.float32)
            row_off = (self.row_rand * (p.line_jitter * w * 0.0045)
                       + (self.row_uniform - 0.5) * (p.line_jitter * w * 0.0022)
                       + drift)
            # 加入随时间变化的行抖动（低频），让相邻帧连贯
            t = frame_index / self.fps
            wave = (np.sin(self._yy[:, 0] * 0.11 + t * 2.4) * 0.5
                    + np.sin(self._yy[:, 0] * 0.037 - t * 1.3) * 0.5)
            row_off = row_off + wave * (p.line_jitter * w * 0.0038)

            x_src = base[None, :] - row_off[:, None]
            if _HAVE_CV2:
                # 用 remap 做逐行偏移，速度最快
                map_x = np.broadcast_to(x_src, (h, w)).astype(np.float32)
                map_y = np.broadcast_to(self._yy, (h, w)).astype(np.float32)
                img = cv2.remap(img, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_REPLICATE)
            else:
                xi = np.clip(np.rint(x_src).astype(np.int32), 0, w - 1)
                img = np.take_along_axis(img, xi[..., None], axis=1)
        return img

    def _apply_head_switch(self, img: np.ndarray, frame_index: int, rng) -> np.ndarray:
        """画面底部的磁头切换噪声带。"""
        p = self.p
        if p.head_switch <= 0:
            return img
        h, w = self.h, self.w
        hs = max(2, int(self.h * p.head_switch_height))
        amt = float(p.head_switch)

        # 带宽随时间轻微变化
        jitter_h = int(rng.integers(0, max(1, hs // 3) + 1))
        band_h = min(h, hs + jitter_h)
        start = h - band_h
        band = img[start:h]

        # 带内的横向撕裂：位移随行递增
        ramp = np.linspace(0.25, 1.0, band_h, dtype=np.float32)
        tear = (ramp * (rng.standard_normal(band_h).astype(np.float32) * 0.5 + 0.5)
                * w * 0.035 * amt)
        base = np.arange(w, dtype=np.float32)[None, :]
        x_src = base - tear[:, None]
        if _HAVE_CV2:
            map_x = x_src.astype(np.float32)
            map_y = np.broadcast_to(self._yy[start:h], (band_h, w)).astype(np.float32)
            band = cv2.remap(band, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE)
        else:
            xi = np.clip(np.rint(x_src).astype(np.int32), 0, w - 1)
            band = np.take_along_axis(band, xi[..., None], axis=1)

        # 带内加噪 + 亮度错位
        noise = rng.standard_normal((band_h, w, 1)).astype(np.float32) * 0.16 * amt
        band = band + noise
        luma_kick = (np.linspace(-0.14, 0.06, band_h, dtype=np.float32)[:, None, None] * amt)
        band = band + luma_kick
        # 高频明暗条纹（只作用在随机挑中的行上）
        stripe = (0.5 + 0.5 * np.sin(self._yy[start:h, :] * 1.7 + rng.random() * 6.28))
        mask = (self.row_uniform[start:h] > 0.55).astype(np.float32)
        stripe_masked = stripe * mask[:, None]
        band = band * (1.0 - 0.22 * amt * stripe_masked[..., None])

        img = img.copy()
        img[start:h] = np.clip(band, 0.0, 1.4)
        return img

    def _apply_tracking_band(self, img: np.ndarray, frame_index: int, rng) -> np.ndarray:
        """缓慢向上滚动的 tracking 失真带。"""
        p = self.p
        if p.tracking_band <= 0:
            return img
        h, w = self.h, self.w
        amt = float(p.tracking_band)
        band_h = max(4, int(h * (0.06 + 0.10 * min(amt, 2.0))))
        speed = 0.35 + 0.5 * amt
        center = int((h + band_h) - ((frame_index * speed * max(1.0, self.h / 480.0))
                                      % (h + band_h * 2)))
        top = center - band_h // 2
        lo, hi = max(0, top), min(h, top + band_h)
        if hi - lo < 3:
            return img

        seg = img[lo:hi]
        rows = hi - lo
        # 半余弦包络：带中心失真最大
        env = np.sin(np.linspace(0, math.pi, rows, dtype=np.float32))
        shift = (env * (rng.standard_normal(rows).astype(np.float32) * 0.5 + 1.0)
                 * w * 0.02 * amt)
        base = np.arange(w, dtype=np.float32)[None, :]
        x_src = base - shift[:, None]
        img = img.copy()
        if _HAVE_CV2:
            map_x = x_src.astype(np.float32)
            map_y = np.broadcast_to(self._yy[lo:hi], (rows, w)).astype(np.float32)
            seg2 = cv2.remap(seg, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE)
        else:
            xi = np.clip(np.rint(x_src).astype(np.int32), 0, w - 1)
            seg2 = np.take_along_axis(seg, xi[..., None], axis=1)
        # 带内亮度压暗 + 噪点，模拟信号瞬时丢失
        seg2 = seg2 * (1.0 - 0.12 * amt * env[:, None, None])
        seg2 = seg2 + rng.standard_normal((rows, w, 1)).astype(np.float32) * 0.06 * amt * env[:, None, None]
        img[lo:hi] = np.clip(seg2, 0.0, 1.4)
        return img

    def _apply_dropouts(self, img: np.ndarray, rng) -> np.ndarray:
        """掉磁：随机的短横线，白或黑。"""
        p = self.p
        if p.dropout <= 0 or p.dropout_lines <= 0:
            return img
        h, w = self.h, self.w
        expected = p.dropout_lines * min(p.dropout, 2.0)
        n = int(rng.poisson(max(0.0, expected)))
        if n <= 0:
            return img
        img = img.copy()
        out = img
        for _ in range(min(n, 40)):
            y = int(rng.integers(0, h))
            th = 1 if rng.random() < 0.8 else 2
            length = int(rng.integers(max(2, w // 120), max(4, w // 12)))
            x = int(rng.integers(0, max(1, w - length)))
            bright = rng.random() < 0.65
            val = (0.88 + 0.5 * rng.random()) if bright else (0.02 + 0.1 * rng.random())
            out[y:y + th, x:x + length] = val
        return out

    def _apply_noise(self, img: np.ndarray, rng) -> np.ndarray:
        p = self.p
        w, h = self.w, self.h
        luma = float(p.luma_noise)
        chroma = float(p.chroma_noise)
        edge = float(p.edge_noise)
        if luma <= 0 and chroma <= 0 and edge <= 0:
            return img

        # 噪点在 1/4 分辨率生成再最近邻放大：
        #   1) 快 10 倍以上（生成 h*w 个高斯随机数是逐帧最贵的一步）
        #   2) 颗粒更大，反而更接近真实磁带的噪声尺度
        slot = int(rng.integers(0, self.noise_pool_size))

        out = img
        if luma > 0:
            n = nearest_upscale(self._noise_luma[slot], w, h)[..., None]
            out = out + n * (0.030 * luma)
            if edge > 0:
                coarse = nearest_upscale(self._noise_coarse[slot], w, h)[..., None]
                out = out + coarse * (0.032 * luma)

        if chroma > 0:
            nc = nearest_upscale(self._noise_chroma[slot], w, h)
            out = out + nc * (0.032 * chroma)

        if edge > 0:
            # 低频脏边/污渍（每帧固定）
            dirt = np.clip(self.grain_low, -2.5, 2.5)[..., None]
            out = out + dirt * (0.012 * edge)
            out = out * (1.0 + dirt * 0.01 * edge)
        return out

    def _apply_optical(self, img: np.ndarray) -> np.ndarray:
        p = self.p
        out = img

        # --- 柔化
        if p.softness > 0:
            s = float(p.softness)
            blurred = gaussian_blur(out, 0.7 + 1.3 * s)
            # blend 越大越糊；留一点原始高频，避免糊成一团
            blend = min(0.82, 0.30 + 0.34 * s)
            out = out * (1.0 - blend) + blurred * blend

        # --- 泛光 / 高光晕染
        if p.bloom > 0:
            b = float(p.bloom)
            luma = out.mean(axis=2)
            # 只在亮部起作用的软阈值
            knee = np.clip((luma - 0.72) / 0.28, 0.0, 1.0)
            hi = out * knee[..., None]
            # 大半径模糊在 1/2 分辨率上算，再放大回去。
            # 泛光本身就是低频信号，这样做肉眼无差别但快约 3 倍。
            hw = max(8, self.w // 2)
            hh = max(8, self.h // 2)
            hi_small = resize_float(hi, (hw, hh))
            glow_small = gaussian_blur(hi_small, (3.5 + 6.0 * b) * 0.5)
            glow = resize_float(glow_small, (self.w, self.h))
            out = out + glow * (0.75 * b)

        # --- 轻微锐化（录像机的边缘增强，会让噪点更明显）
        if p.sharpen > 0:
            k = float(p.sharpen)
            blur = gaussian_blur(out, 1.0)
            out = out + (out - blur) * (0.85 * k)

        # --- 色彩调整
        out = self._apply_color(out)
        return out

    def _apply_color(self, img: np.ndarray) -> np.ndarray:
        p = self.p
        out = img

        # 对比度（绕 0.5 中灰）
        c = float(p.contrast)
        if abs(c - 1.0) > 1e-4:
            out = (out - 0.5) * c + 0.5

        # 亮度
        if abs(p.brightness - 1.0) > 1e-4:
            out = out * float(p.brightness)

        # 饱和度
        s = float(p.saturation)
        if abs(s - 1.0) > 1e-4:
            # 用可复用的缓冲区算加权亮度，避免每帧新建 (H,W) 临时数组
            if self._luma_buf is None or self._luma_buf.shape != out.shape[:2]:
                self._luma_buf = np.empty(out.shape[:2], np.float32)
            luma = self._luma_buf
            np.multiply(out[..., 0], 0.299, out=luma)
            luma += out[..., 1] * 0.587
            luma += out[..., 2] * 0.114
            out = luma[..., None] + (out - luma[..., None]) * s
            # VHS 的色度是压缩过的，高饱和处会削顶
            over = out > 1.0
            if over.any():
                np.copyto(out, 1.0 - (out - 1.0) * 0.55, where=over)

        # 色偏：两项合起来只做一次加法，省一个满帧临时数组
        g = float(p.tint_green)
        t = float(p.tint_sepia)
        if g or t > 0:
            if t > 0:
                sep = np.empty_like(out)
                # 棕褐色调矩阵（原地写入每个通道）
                np.multiply(out[..., 0], 0.393, out=sep[..., 0])
                sep[..., 0] += out[..., 1] * 0.769
                sep[..., 0] += out[..., 2] * 0.189
                np.multiply(out[..., 0], 0.349, out=sep[..., 1])
                sep[..., 1] += out[..., 1] * 0.686
                sep[..., 1] += out[..., 2] * 0.168
                np.multiply(out[..., 0], 0.272, out=sep[..., 2])
                sep[..., 2] += out[..., 1] * 0.534
                sep[..., 2] += out[..., 2] * 0.131
                # out = out*(1-t) + sep*t，原地完成
                out *= (1.0 - t)
                np.multiply(sep, t, out=sep)
                out += sep
            if g:
                out[..., 0] -= g * 0.35
                out[..., 1] += g
                out[..., 2] -= g * 0.5

        return out

    def _apply_scanlines(self, img: np.ndarray, frame_index: int) -> np.ndarray:
        p = self.p
        if p.scanlines <= 0:
            return img
        # 让扫描线随帧缓慢滚动，模拟 CRT 与录像带不同步
        phase = int(round((frame_index * 0.5) % max(1.0, float(p.scanline_period))))
        prof = np.roll(self.scanline_profile, phase, axis=0)
        return img * prof[..., None]

    # -- 主流程 ------------------------------------------------------------
    def process(self, frame_bgr: np.ndarray, frame_index: int = 0) -> np.ndarray:
        """输入/输出均为 BGR uint8 (H, W, 3)。"""
        p = self.p
        if frame_bgr.shape[0] != self.h or frame_bgr.shape[1] != self.w:
            raise ValueError(
                "帧尺寸 %s 与上下文 %s 不一致" % (frame_bgr.shape[:2], (self.h, self.w))
            )
        rng = self.temporal_rng(frame_index)

        # BGR -> RGB float
        rgb = frame_bgr[:, :, ::-1].astype(np.float32) / 255.0
        rgb = np.ascontiguousarray(rgb)

        # 1) 色度通道劣化
        ycc = rgb_to_ycbcr(rgb)
        ycc = self._apply_chroma_bleed(ycc, rng)
        work = ycbcr_to_rgb(ycc)

        # 2) 时基误差
        work = self._apply_tbc_error(work, frame_index, rng)

        # 3) 光学（柔化/泛光/锐化/色彩）
        work = self._apply_optical(work)

        # 4) 磁头切换 + tracking + 掉磁
        work = self._apply_head_switch(work, frame_index, rng)
        work = self._apply_tracking_band(work, frame_index, rng)
        work = self._apply_dropouts(work, rng)

        # 5) 噪点
        work = self._apply_noise(work, rng)

        # 6) 扫描线 + 暗角
        work = self._apply_scanlines(work, frame_index)
        if p.vignette > 0:
            work = work * self.vignette_mask[..., None]

        # 7) OSD 文字（放在噪点之后；_apply_osd 内部会 copy 一份再局部混合，
        #    所以这里直接接管返回值，避免再多一次全帧拷贝）
        work = self._apply_osd(work, frame_index, rng)

        # 8) 最终软限幅：模拟复合视频的过冲并压回合法范围
        return _to_uint8(np.clip(work, 0.0, 1.0))[:, :, ::-1].copy()


# ---------------------------------------------------------------------------
# 图片 / 视频 高层接口
# ---------------------------------------------------------------------------
def _open_image_float(path: str) -> Tuple[np.ndarray, dict]:
    """读取图片 -> (RGB float [0,1], info)。会应用 EXIF 旋转。"""
    img = Image.open(path)
    info = {"format": img.format, "mode": img.mode, "size": img.size}
    # 处理超大图的 decompression bomb 误报
    Image.MAX_IMAGE_PIXELS = None
    try:
        from PIL import ImageOps

        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode in ("RGBA", "LA", "PA"):
        bg = Image.new("RGB", img.size, (0, 0, 0))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return _to_float(np.asarray(img, dtype=np.uint8)), info


def _save_image_float(arr_rgb: np.ndarray, out_path: str, quality: int = 95,
                      also_png: bool = False) -> None:
    img = Image.fromarray(_to_uint8(arr_rgb), mode="RGB")
    ext = os.path.splitext(out_path)[1].lower()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    if ext in (".jpg", ".jpeg"):
        img.save(out_path, "JPEG", quality=int(quality), subsampling=0, optimize=True)
    elif ext == ".png":
        img.save(out_path, "PNG", optimize=False)
    elif ext == ".webp":
        img.save(out_path, "WEBP", quality=int(quality))
    elif ext == ".bmp":
        img.save(out_path, "BMP")
    elif ext == ".tif" or ext == ".tiff":
        img.save(out_path, "TIFF")
    else:
        img.save(out_path, "JPEG", quality=int(quality), subsampling=0, optimize=True)


def process_image(in_path: str, out_path: str, params: VHSParams,
                  max_dim: Optional[int] = None,
                  start_time: Optional[datetime.datetime] = None) -> dict:
    """处理单张图片。返回处理信息字典。

    照片默认把时间戳"固定"在起始时间上（`timestamp_mode="auto"` 时的行为），
    也就是你想让它印几点就印几点。
    """
    src, info = _open_image_float(in_path)
    h, w = src.shape[:2]

    limit = max_dim or params.max_dim
    if limit and max(h, w) > limit:
        scale = limit / float(max(h, w))
        nw = max(2, int(round(w * scale)))
        nh = max(2, int(round(h * scale)))
        # 降采样用面积平均，避免摩尔纹
        src_small = resize_float(src, (nw, nh))
    else:
        src_small = src
        nw, nh = w, h

    mode = (params.timestamp_mode or "auto").strip().lower()
    advancing = mode in ("advance", "advancing", "video", "递增", "推进")
    ctx = VHSContext(params, (nh, nw), fps=25.0, start_time=start_time,
                     advancing=advancing)
    # 图片用固定相位，保证"确定性"（同参数同图必然同结果）
    frame = _to_uint8(src_small)[:, :, ::-1].copy()  # RGB->BGR
    out_bgr = ctx.process(frame, frame_index=0)
    out_rgb = out_bgr[:, :, ::-1]

    if (nw, nh) != (w, h):
        out_rgb = resize_float(_to_float(out_rgb), (w, h))
        out_rgb = _to_uint8(out_rgb)

    _save_image_float(out_rgb, out_path)
    return {
        "input": in_path, "output": out_path, "src_size": (w, h),
        "work_size": (nw, nh), "orig_format": info.get("format"),
        "timestamp": ctx._timestamp_text(0) if params.osd_timestamp else None,
    }


def list_video_streams(path: str) -> dict:
    """用 OpenCV 探测视频信息。"""
    if not _HAVE_CV2:
        return {}
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        cap.release()
        return {}
    info = {
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "fps": float(cap.get(cv2.CAP_PROP_FPS)) or 25.0,
        "frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
    }
    cap.release()
    return info

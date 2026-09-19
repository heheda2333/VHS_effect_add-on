# -*- coding: utf-8 -*-
"""VHS 效果处理核心库。

本包提供三个层次的能力，可单独使用，也可由图形界面或命令行调用：

``vhs_effects``
    画面处理核心。参数容器 :class:`vhs_effects.VHSParams`，
    逐帧处理上下文 :class:`vhs_effects.VHSContext`，
    以及图片与视频的高层入口。

``vhs_video``
    视频读写。基于 ffmpeg 管道完成编解码与音轨处理，
    并提供硬件编码器的探测与选择。

``vhs_task``
    目录遍历、输出路径映射与批量处理流程，供命令行与图形界面共用。

典型用法::

    from vhs_lib import resolve_preset, VHSContext

    params = resolve_preset("classic")
    ctx = VHSContext(params, (1080, 1920), fps=25.0)
    out_frame = ctx.process(frame, frame_index=0)
"""

from __future__ import annotations

import os as _os
import sys as _sys

# ---------------------------------------------------------------------------
# 依赖目录定位
# ---------------------------------------------------------------------------
# 本工具的全部第三方依赖安装于项目根目录下的 libs/，不写入系统 Python 目录。
# 本包位于 <项目根>/vhs_lib/，因此项目根目录为上两级。
# 这段逻辑放在包初始化中，无论通过脚本入口、图形界面还是直接 import 使用，
# 都能保证依赖可被找到。
_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_LIBS_DIR = _os.path.join(_PROJECT_ROOT, "libs")
for _path in (_LIBS_DIR, _PROJECT_ROOT):
    if _os.path.isdir(_path) and _path not in _sys.path:
        _sys.path.insert(0, _path)

__all__ = [
    "VHSParams",
    "VHSContext",
    "PRESETS",
    "resolve_preset",
    "parse_start_time",
    "describe_time",
    "process_image",
    "parse_hex_color",
    "image_extensions",
    "video_extensions",
]

__version__ = "2.0"

from .vhs_effects import (  # noqa: E402
    PRESETS,
    VHSContext,
    VHSParams,
    describe_time,
    parse_hex_color,
    parse_start_time,
    process_image,
    resolve_preset,
)


def image_extensions() -> tuple:
    """返回支持的图片扩展名（小写，含点号）。"""
    from .vhs_effects import IMAGE_EXTS

    return tuple(sorted(IMAGE_EXTS))


def video_extensions() -> tuple:
    """返回支持的视频扩展名（小写，含点号）。"""
    from .vhs_effects import VIDEO_EXTS

    return tuple(sorted(VIDEO_EXTS))

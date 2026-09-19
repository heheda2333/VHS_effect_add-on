#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""运行环境检查。

在启动图形界面或命令行之前调用，检查 Python 版本与依赖库是否就绪，
并给出可直接执行的修复命令。检查通过时退出码为 0，否则为 1。

用法::

    python check_env.py
    python check_env.py --quiet      # 仅在出错时输出
"""

from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 依赖库目录：与本项目一同分发，无需写入系统 Python 目录
LIBS_DIR = os.path.join(_ROOT, "libs")
if os.path.isdir(LIBS_DIR) and LIBS_DIR not in sys.path:
    sys.path.insert(0, LIBS_DIR)

# 最低 Python 版本
MIN_PYTHON = (3, 8)

# 依赖清单：(导入名, 安装名, 是否必需, 用途说明)
DEPENDENCIES = (
    ("numpy", "numpy", True, "像素运算"),
    ("PIL", "Pillow", True, "图片读写与文字绘制"),
    ("cv2", "opencv-python-headless", False, "高斯模糊与重采样加速"),
    ("scipy", "scipy", False, "缺少 OpenCV 时的模糊后备实现"),
    ("imageio_ffmpeg", "imageio-ffmpeg", False, "视频编解码与音轨处理"),
)


def check_python_version() -> bool:
    if sys.version_info >= MIN_PYTHON:
        print("  [通过] Python %d.%d.%d"
              % (sys.version_info.major, sys.version_info.minor,
                 sys.version_info.micro))
        return True
    print("  [失败] Python 版本过低：当前 %d.%d，需要 %d.%d 或更高"
          % (sys.version_info.major, sys.version_info.minor,
             MIN_PYTHON[0], MIN_PYTHON[1]))
    return False


def check_dependencies() -> bool:
    """检查依赖库。返回是否满足最低要求（必需库全部可用）。"""
    import importlib

    required_ok = True
    for module_name, package_name, required, purpose in DEPENDENCIES:
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", "未知版本")
            print("  [通过] %-16s %-10s %s" % (module_name, version, purpose))
        except Exception:
            if required:
                required_ok = False
                print("  [失败] %-16s %-10s 缺少，%s（必需）"
                      % (module_name, package_name, purpose))
            else:
                print("  [提示] %-16s %-10s 缺少，%s（可选）"
                      % (module_name, package_name, purpose))
    return required_ok


def check_project_layout() -> None:
    """检查项目自带目录是否存在，缺失时自动创建。"""
    for name in ("input", "output"):
        path = os.path.join(_ROOT, name)
        if os.path.isdir(path):
            print("  [通过] 目录 %s 存在" % name)
        else:
            try:
                os.makedirs(path, exist_ok=True)
                print("  [提示] 目录 %s 不存在，已自动创建" % name)
            except OSError as exc:
                print("  [提示] 目录 %s 无法创建：%s" % (name, exc))


def check_video_support() -> None:
    """报告视频链路状态。视频为可选功能，缺失不影响图片处理。"""
    try:
        from vhs_lib import vhs_video
    except Exception as exc:
        print("  [提示] 视频模块无法载入：%s" % exc)
        return

    if vhs_video.ffmpeg_available():
        print("  [通过] ffmpeg 可用，视频处理与音轨保留功能正常")
    else:
        print("  [提示] 未找到 ffmpeg，视频将回退为无音轨输出（图片处理不受影响）")

    available = vhs_video.probe_hardware_encoders()
    if available:
        names = "、".join(meta["label"] for meta in available.values())
        print("  [通过] 可用硬件编码器：%s" % names)
    else:
        print("  [提示] 未检测到可用的硬件编码器，视频将使用 CPU 编码")


def main() -> int:
    parser = argparse.ArgumentParser(description="检查运行环境与依赖")
    parser.add_argument("--quiet", action="store_true",
                        help="仅在检查未通过时输出内容")
    args = parser.parse_args()

    if not args.quiet:
        print("=" * 62)
        print(" VHS 效果处理工具 —— 运行环境检查")
        print(" 项目目录：%s" % _ROOT)
        print(" 依赖目录：%s" % LIBS_DIR)
        print("=" * 62)

    python_ok = check_python_version()
    deps_ok = check_dependencies()
    if not args.quiet or not (python_ok and deps_ok):
        check_project_layout()
        check_video_support()

    if python_ok and deps_ok:
        if not args.quiet:
            print()
            print("环境检查通过。")
        return 0

    print()
    print("环境检查未通过。请依次尝试：")
    print("  1) 运行 安装依赖.bat")
    print("  2) 或执行命令：python setup_deps.py")
    return 1


if __name__ == "__main__":
    sys.exit(main())

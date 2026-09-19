#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
setup_deps.py -- 把依赖装进项目自带的 libs/ 目录
================================================

本工具的**所有依赖都放在 VHS_effect_add-on/libs/ 里**，不写系统 site-packages，
所以整包拷走就能跑，也不会影响你其他的 Python 项目。

用法
----
    python setup_deps.py              # 有网：直接下载并安装
    python setup_deps.py --offline    # 离线：只用 _wheels/ 里已有的 whl
    python setup_deps.py --check      # 只检查当前状态，不安装

原理
----
    1. pip download --dest _wheels ...      （只下载，这一步很慢但不写系统目录）
    2. pip install --target libs --no-index --find-links _wheels ...
       （纯本地解包，几秒钟完成）
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
LIBS = os.path.join(ROOT, "libs")
WHEELS = os.path.join(ROOT, "_wheels")

REQUIRED = [
    ("numpy", "numpy", "像素运算（必需）"),
    ("PIL", "Pillow", "图片读写 / OSD 文字（必需）"),
    ("cv2", "opencv-python-headless", "快速模糊与抖动重采样（强烈建议）"),
    ("scipy", "scipy", "OpenCV 缺失时的模糊后备（可选）"),
    ("imageio_ffmpeg", "imageio-ffmpeg", "自带 ffmpeg，视频编码 + 音轨（视频必需）"),
]

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def add_libs() -> None:
    if os.path.isdir(LIBS) and LIBS not in sys.path:
        sys.path.insert(0, LIBS)


def status() -> dict:
    """返回 {模块名: 版本 或 None}。"""
    add_libs()
    import importlib

    out = {}
    for mod, _pkg, _desc in REQUIRED:
        try:
            m = importlib.import_module(mod)
            out[mod] = getattr(m, "__version__", "?")
        except Exception:
            out[mod] = None
    return out


def print_status() -> bool:
    st = status()
    print("依赖状态（优先读 VHS_effect_add-on/libs/）：")
    ok_all = True
    for mod, pkg, desc in REQUIRED:
        ver = st.get(mod)
        if ver:
            print("  [OK]      %-16s %-8s %s" % (mod, ver, desc))
        else:
            print("  [MISSING] %-16s %-8s %s" % (mod, pkg, desc))
            if mod in ("numpy", "PIL"):
                ok_all = False
    return ok_all


def run(cmd, **kw):
    print("  $ %s" % " ".join(cmd))
    return subprocess.run(cmd, creationflags=CREATE_NO_WINDOW, **kw)


def pip_cmd(*args) -> list:
    # 必须附带 --no-user。部分 Python 发行版在自带的 pip 配置中将 install.user
    # 固定为 yes，该设置与 --target 冲突，会使安装失败并提示
    # "Can not combine '--user' and '--target'"。
    # 注意 --no-user 属于子命令级选项，必须置于子命令之后。
    args = list(args)
    if args:
        return [sys.executable, "-u", "-m", "pip", args[0], "--no-user"] + args[1:]
    return [sys.executable, "-u", "-m", "pip"]


def download(names: list, no_deps: bool = False) -> bool:
    os.makedirs(WHEELS, exist_ok=True)
    cmd = pip_cmd("download", "--dest", WHEELS, "--progress-bar", "off", "--no-input")
    if no_deps:
        cmd.append("--no-deps")
    cmd += names
    print("\n[1/2] 下载 wheel 到 _wheels/（可能要几分钟，取决于网速）")
    proc = run(cmd)
    return proc.returncode == 0


def install_from_wheels() -> bool:
    have = glob.glob(os.path.join(WHEELS, "*.whl"))
    if not have:
        print("  ! _wheels/ 里没有 .whl 文件")
        return False
    os.makedirs(LIBS, exist_ok=True)
    print("\n[2/2] 从 _wheels/ 安装到 libs/（不联网、不动系统目录）")
    # 安装时带 --no-deps：依赖在 download 阶段已经解析好了，
    # 而且这样能避免 pip 把另一个 numpy 也塞进 libs/ 覆盖掉全局能用的那份。
    cmd = pip_cmd("install", "--target", LIBS, "--no-index", "--no-deps",
                  "--find-links", WHEELS, "--upgrade", "--no-input",
                  "--no-warn-script-location", "--no-compile")
    cmd += [os.path.basename(w) for w in sorted(have)]
    proc = run(cmd)
    return proc.returncode == 0


def fallback_unzip() -> bool:
    """pip 都用不了时的兜底：直接把 whl 当 zip 解到 libs/。"""
    wheels = glob.glob(os.path.join(WHEELS, "*.whl"))
    if not wheels:
        return False
    print("\n[备选] pip 安装失败，改为直接解包 wheel 到 libs/")
    os.makedirs(LIBS, exist_ok=True)
    for whl in sorted(wheels):
        print("  解包 %s" % os.path.basename(whl))
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(LIBS)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="安装依赖到项目自带的 libs/ 目录")
    ap.add_argument("--offline", action="store_true", help="只用 _wheels/ 里已有的 whl")
    ap.add_argument("--check", action="store_true", help="只检查状态")
    ap.add_argument("--only", nargs="*", default=None,
                    help="只装指定包，如 --only opencv-python-headless")
    args = ap.parse_args()

    print("=" * 68)
    print(" VHS_effect_add-on 依赖安装")
    print(" 项目目录 : %s" % ROOT)
    print(" 依赖目录 : %s" % LIBS)
    print(" wheel缓存: %s" % WHEELS)
    print(" Python   : %s" % sys.version.split()[0])
    print("=" * 68)

    if args.check:
        return 0 if print_status() else 1

    before = status()

    if args.only:
        needed = list(args.only)
    else:
        needed = [pkg for mod, pkg, _ in REQUIRED if before.get(mod) is None]
        if not needed:
            print("\n所有依赖都已就绪，无需安装。")
            print_status()
            return 0

    print("\n需要安装：%s" % ", ".join(needed))

    if not args.offline:
        download(needed)

    ok = install_from_wheels()
    if not ok:
        ok = fallback_unzip()

    if not ok:
        print("\n安装失败。请手动执行：")
        print("  python -m pip install --target \"%s\" %s" % (LIBS, " ".join(needed)))
        return 1

    # 清掉 pip 可能留下的无用的 bin 目录（.exe 启动器在 --target 下没用）
    bin_dir = os.path.join(LIBS, "bin")
    if os.path.isdir(bin_dir):
        shutil.rmtree(bin_dir, ignore_errors=True)

    print()
    good = print_status()
    after = status()
    installed = [m for m in after if after[m] and not before.get(m)]
    if installed:
        print("\n新装/更新：%s" % ", ".join(installed))
    print("\n完成。现在可以运行：python vhs_tool.py selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())

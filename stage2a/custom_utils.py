#!/usr/bin/env python3
"""
custom_utils.py – shared paths and helpers for FirmSolo.
Python 3.7+ compatible (uses typing module, no X|Y union syntax).
"""

from __future__ import annotations

import os
import json
import pickle
import platform
import shutil
from typing import Dict, List, Optional

# ── Directory layout ──────────────────────────────────────────────────────────
kernel_prefix = "linux-"
result_dir_path = os.environ.get("FIRMSOLO_RESULT_DIR", "/output/results/")
kern_dir = os.environ.get("FIRMSOLO_KERN_DIR", "/output/kernel_dirs/")
tar_dir = os.environ.get("FIRMSOLO_TAR_DIR", "/output/kernel_tars/")
openwrt_patch_dir = os.environ.get("FIRMSOLO_PATCH_DIR", "/FirmSolo/openwrt_patches/")
kernel_configs_dir = os.environ.get("FIRMSOLO_CONFIG_DIR", "/FirmSolo/kernel_configs/")
image_db_path = os.environ.get("FIRMSOLO_IMAGE_DB", "/FirmSolo/image_db.json")
scripts_dir = os.environ.get("FIRMSOLO_SCRIPTS_DIR", "/output/scripts/compile_scripts/")

# ── SRCARCH mapping (mirrors kernel top-level Makefile) ──────────────────────
_SRCARCH_MAP: Dict[str, str] = {
    "x86_64": "x86",
    "i386": "x86",
    "sparc64": "sparc",
    "sparc32": "sparc",
    "sh64": "sh",
    "tilegx": "tile",
    "tilepro": "tile",
}


def srcarch(arch: str) -> str:
    """Return the kernel SRCARCH value for a given ARCH string."""
    return _SRCARCH_MAP.get(arch, arch)


# ── LLVM / toolchain helpers ──────────────────────────────────────────────────


def _which_gcc(prefix: str) -> Optional[str]:
    """Return the full path to <prefix>gcc if on PATH, else None."""
    return shutil.which(prefix + "gcc")


def is_llvm_available() -> bool:
    """True when both clang and ld.lld are on PATH."""
    return shutil.which("clang") is not None and shutil.which("ld.lld") is not None


def use_llvm_for_kernel(kernel: str) -> bool:
    """
    Use LLVM=1 (clang + lld) when:
      * kernel >= linux-5.0  (stable Clang support landed here)
      * clang and ld.lld are both on PATH
    """
    if not is_llvm_available():
        return False
    return kernel >= "linux-5.0"


def get_toolchain(kernel: str, arch: str, endianess: str) -> str:
    """
    Return the CROSS_COMPILE prefix for the target arch.

    For x86_64 native builds with LLVM available, returns "" because
    LLVM=1 is used and no cross-compiler prefix is needed.
    For ARM / MIPS returns the first matching GCC cross-compiler on PATH.
    """
    big = endianess in ("big endian", "big", "BE")

    # ── x86 native build ─────────────────────────────────────────────────────
    if arch in ("x86_64", "x86", "i386"):
        host = platform.machine()
        if host in ("x86_64", "i686", "i386"):
            if use_llvm_for_kernel(kernel):
                return ""  # LLVM=1 handles everything; no prefix needed
        candidates: List[str] = ["x86_64-linux-gnu-", "x86_64-linux-musl-"]

    # ── ARM ───────────────────────────────────────────────────────────────────
    elif arch == "arm":
        candidates = (
            ["armeb-linux-gnueabi-", "arm-linux-gnueabi-"]
            if big
            else ["arm-linux-gnueabi-", "arm-linux-gnueabihf-"]
        )

    # ── MIPS ──────────────────────────────────────────────────────────────────
    elif arch == "mips":
        candidates = (
            ["mips-linux-gnu-", "mips-buildroot-linux-gnu-"]
            if big
            else ["mipsel-linux-gnu-", "mipsel-buildroot-linux-gnu-"]
        )

    # ── AArch64 ───────────────────────────────────────────────────────────────
    elif arch in ("arm64", "aarch64"):
        candidates = ["aarch64-linux-gnu-", "aarch64-unknown-linux-gnu-"]

    else:
        candidates = ["{}-linux-gnu-".format(arch)]

    for prefix in candidates:
        if _which_gcc(prefix):
            return prefix

    # Return first candidate even when not installed; LLVM builds ignore it
    return candidates[0]


def get_cc_for_kconfig(arch: str, cross: str) -> str:
    """
    Best available CC for kconfiglib $(cc-option,...) probe macros.
    """
    # 1. If LLVM=1 is active, ALWAYS return clang if available
    if os.environ.get("LLVM") == "1" or cu.is_llvm_available():
        clang = shutil.which("clang")
        if clang:
            print ("USING CLANG YAY")
            return clang

    # 2. Fall back to Cross-GCC if explicitly using a GCC cross-toolchain
    if cross:
        found = shutil.which(cross + "gcc")
        if found:
            return found

    # 3. Generic fallbacks
    for cand in ("clang", "gcc", "cc"):
        found = shutil.which(cand)
        if found:
            return found
    print ("KILL YOURSELF")
    return "cc"

# ── Defconfig resolution ──────────────────────────────────────────────────────
# String values that end in _defconfig are treated as built-in make targets
# (i.e.  make ARCH=x86_64 x86_64_defconfig) rather than file paths.

DEFCONFIGS: Dict[str, str] = {
    "mips": kernel_configs_dir + "config.mips_malta",
    "armv5": kernel_configs_dir + "config.arm_versatile_v5",
    "armv6": kernel_configs_dir + "config.arm_realview_v6",
    "armv7": kernel_configs_dir + "config.arm_realview_v7",
    # x86_64: use the kernel's built-in make target
    "x86_64": "x86_64_defconfig",
    "x86": "x86_64_defconfig",
}


def get_vendor(
    image: str,
    arch: str,
    ds_recovery: int,
    new_kern_dir: str,
    arm_type: Optional[str] = None,
) -> str:
    """Return the defconfig path (or make-target name) for arch/variant."""
    if arch == "mips":
        conf = DEFCONFIGS.get("mips", kernel_configs_dir + "config.mips_malta")
        print("ARGS mips")
    elif arch in ("x86_64", "x86", "i386"):
        conf = DEFCONFIGS.get("x86_64", "x86_64_defconfig")
        print("ARGS x86_64")
    elif arch == "arm":
        key = (arm_type or "armv7").lower()
        conf = DEFCONFIGS.get(key, DEFCONFIGS["armv7"])
        print("ARGS", arm_type if arm_type else "armv7")
    else:
        conf = kernel_configs_dir + "config.{}".format(arch)
        print("ARGS", arch)
    return conf


# ── Image metadata ────────────────────────────────────────────────────────────


def get_image_info(image: str, which_info: List[str]) -> list:
    """Load per-image metadata from image_db.json."""
    try:
        with open(image_db_path, "r") as f:
            db = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            "Image database not found at '{}'. "
            "Set FIRMSOLO_IMAGE_DB env var to override.".format(image_db_path)
        )
    if image not in db:
        raise KeyError("Image '{}' not found in database.".format(image))
    entry = db[image]
    return [entry.get(k) for k in which_info]


# ── Misc helpers ──────────────────────────────────────────────────────────────


def read_file(filepath: str) -> List[str]:
    """Return non-empty stripped lines from a text file."""
    lines: List[str] = []
    try:
        with open(filepath, "r") as f:
            for line in f:
                s = line.strip()
                if s:
                    lines.append(s)
    except FileNotFoundError:
        print("File not found: {}".format(filepath))
    return lines


def write_pickle(path: str, data: object) -> None:
    with open(path, "wb") as f:
        pickle.dump(data, f)


# ── Smoke-test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys as _sys

    print("kernel_prefix      :", kernel_prefix)
    print("kern_dir           :", kern_dir)
    print("is_llvm_available  :", is_llvm_available())

    for _k in ("linux-6.18", "linux-4.19", "linux-3.10"):
        print("use_llvm({:<14}) :".format(_k + ")"), use_llvm_for_kernel(_k))

    for _arch in ("x86_64", "arm", "mips"):
        _cross = get_toolchain("linux-6.18", _arch, "little")
        print("toolchain {:8s}  : '{}'".format(_arch, _cross))

    print("srcarch x86_64     :", srcarch("x86_64"))
    print("srcarch arm        :", srcarch("arm"))
    print("get_vendor x86_64  :", get_vendor("img", "x86_64", 0, "/tmp/"))
    print("get_vendor armv7   :", get_vendor("img", "arm", 0, "/tmp/", "armv7"))

    required = [
        "kernel_prefix",
        "result_dir_path",
        "kern_dir",
        "tar_dir",
        "scripts_dir",
        "is_llvm_available",
        "use_llvm_for_kernel",
        "get_toolchain",
        "get_cc_for_kconfig",
        "srcarch",
        "get_vendor",
        "get_image_info",
        "read_file",
        "write_pickle",
        "DEFCONFIGS",
    ]
    this = _sys.modules[__name__]
    missing = [n for n in required if not hasattr(this, n)]
    if missing:
        print("MISSING:", missing)
        _sys.exit(1)
    print("All required symbols present — OK")

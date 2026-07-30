#!/usr/bin/env python3
"""
driver_tool.py – generate a minimal x86_64 Linux kernel .config for a
set of specified CONFIG symbols.

Usage (after running alpine_setup.sh once):
  python3 stage2a/driver_tool.py

Prerequisites:
  /output/kernel_tars/linux-6.18.tar.gz   – kernel source tarball
  clang + ld.lld on PATH                  – installed by alpine_setup.sh
  binutils 'as' on PATH                   – for $(as-version) probes
"""

from __future__ import annotations

import os
import sys
import shutil

currentdir = os.path.dirname(os.path.realpath(__file__))
os.environ["KCFLAGS"] = "-flto=full -ffat-lto-objects -fexperimental-call-graph-section"
os.environ["MAKEFLAGS"] = f"-j{os.cpu_count() or 1}"
os.environ["LLVM"] = "1"
os.environ["CC"] = "clang"
sys.path.insert(0, currentdir)

import custom_utils as cu
from firm_kern_comp import compile_kernel, find_caller_configs
from capCheckingFunctions import capFuncs


# ── Target platform ───────────────────────────────────────────────────────────
kernel_version = "6.18"
arch = "x86_64"  # CHANGED from arm – build/analyse x86_64 kernel
os.environ["ARCH"] = arch

endianess = "little"
# ver_magicz: empty for x86_64 – platform settings come from x86_64_defconfig.
# (For ARM/MIPS firmware analysis this would contain e.g. ["ARMv7", "p2v8"])
ver_magicz: list = []

capable_caller_configs = find_caller_configs("./../../../../output/kernel_dirs/linux-6.18/", "file_ns_capable")
print(capable_caller_configs)

# ── Toolchain ─────────────────────────────────────────────────────────────────
cross_compiler = cu.get_toolchain(cu.kernel_prefix + kernel_version, arch, endianess)
_using_llvm = cu.use_llvm_for_kernel(cu.kernel_prefix + kernel_version)

print("=" * 60)
print("  Kernel    : linux-{}".format(kernel_version))
print("  Arch      : {} ({}-endian)".format(arch, endianess))
if _using_llvm:
    print(
        "  Toolchain : Clang/LLVM (LLVM=1)  [clang={}]".format(
            shutil.which("clang") or "not found"
        )
    )
else:
    gcc = shutil.which((cross_compiler or "") + "gcc") or "(not found)"
    print("  Toolchain : GCC  CROSS_COMPILE={}  [{}]".format(cross_compiler, gcc))
print("  SRCARCH   : {}".format(cu.srcarch(arch)))
print("=" * 60)

# ── Symbols / CONFIG options to enable ───────────────────────────────────────
# KCRE automatically resolves all upstream Kconfig dependencies.
conf_opts = [
    "CONFIG_LTO_CLANG_FULL",
    "CONFIG_MODULES",
    "!CONFIG_COMPILE_TEST",
] + ['CONFIG_MMU', 'CONFIG_SECCOMP', 'CONFIG_SYSFS', 'CONFIG_TIME_NS', 'CONFIG_USER_NS']  
symbolz = [] # capFuncs 

# ── Plumbing ──────────────────────────────────────────────────────────────────
image_id = "custom_build"
modulez: list = []
module_options: list = []
guard_expr: list = []
ds_options: list = []
extraversion = ""
ds_recovery = 0
single_module_dir = ""
s_config = "yes"
openwrt = False

# ── Launch ────────────────────────────────────────────────────────────────────
compile_kernel(
    image_id,
    ds_options,
    ds_recovery,
    single_module_dir,
    s_config,
    openwrt,
    kernel_version,
    extraversion,
    modulez,
    ver_magicz,
    symbolz,
    arch,
    endianess,
    cross_compiler,
    conf_opts,
    guard_expr,
    module_options,
)

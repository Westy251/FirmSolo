#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
import sys
from typing import Dict, List, Optional, Union, Tuple, Any

currentdir = os.path.dirname(os.path.realpath(__file__))
parentdir = os.path.dirname(currentdir)
sys.path.append(parentdir)
sys.path.append(currentdir)

import custom_utils as cu
import subprocess
from kcre import update_config
from fix_kconf import fix_configs
import tarfile
from hot_fixes import hot_fixes
import argparse as argp
import traceback
from firmadyne_fix import apply_fdyne_hooks
import time as tm
import re

# ── SRCARCH mapping ───────────────────────────────────────────────────────────
_SRCARCH_MAP: Dict[str, str] = {
    "x86_64": "x86",
    "i386": "x86",
    "sparc64": "sparc",
    "sparc32": "sparc",
    "sh64": "sh",
    "tilegx": "tile",
    "tilepro": "tile",
}


def _srcarch(arch: str) -> str:
    """Map ARCH to SRCARCH (mirrors kernel top-level Makefile)."""
    return _SRCARCH_MAP.get(arch, arch)


# ── Build-flag helpers ────────────────────────────────────────────────────────


def _build_flags(kernel: str, arch: str, cross: str, extraver: str) -> List[str]:
    if cu.use_llvm_for_kernel(kernel):
        return ["ARCH={}".format(arch), "LLVM=1", extraver]
    else:
        return ["ARCH={}".format(arch), "CROSS_COMPILE={}".format(cross), extraver]


def _setup_build_env(arch: str, cross: str, image_dir: str) -> None:
    srcarch = _srcarch(arch)
    os.environ["ARCH"] = arch
    os.environ["SRCARCH"] = srcarch
    real_dir = os.path.realpath(image_dir.rstrip("/"))
    os.environ["srctree"] = real_dir
    os.environ["abs_srctree"] = real_dir

    cc = cu.get_cc_for_kconfig(arch, cross)
    os.environ["CC"] = cc
    print("  build_env: CC={}  ARCH={}  SRCARCH={}".format(cc, arch, srcarch))

    for cand in ["as", "x86_64-linux-gnu-as", "llvm-as"]:
        found = shutil.which(cand)
        if found:
            os.environ.setdefault("AS", found)
            break

    if cu.is_llvm_available():
        os.environ.setdefault("LLVM", "1")
        lld = shutil.which("ld.lld")
        if lld:
            os.environ.setdefault("LD", lld)


# ── Kernel helpers ────────────────────────────────────────────────────────────


def exported_syms(kern_dir: str):
    symvers: List[str] = []
    sysmap: List[str] = []
    with open(kern_dir + "System.map", "r") as f2:
        line = f2.readline()
        while line:
            sysmap.append(line.split()[2])
            line = f2.readline()
    return symvers, sysmap


def clean_source(kernel: str, kern_dir: str) -> None:
    try:
        subprocess.run(
            ["make", "mrproper"],
            cwd=kern_dir + kernel,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        print("Cleaning kernel source")
    except Exception:
        print("Something went wrong with cleaning the kernel")


def remove_kernel_dir(ds_recovery: int, kern_dir: str) -> None:
    if not ds_recovery:
        try:
            os.system("rm -rf " + kern_dir + "/")
        except Exception:
            print("The kernel is not yet extracted...Cant remove it")


def create_directories(
    kernel,
    resultdir,
    new_kern_dir,
    kern_dir,
    tar_dir,
    tarf,
    ds_recovery,
    s_config,
) -> None:
    try:
        os.mkdir(resultdir)
    except Exception:
        print("Directory {0} already exists".format(resultdir))

    if not ds_recovery and s_config == "yes":
        try:
            os.system("rm -rf " + new_kern_dir)
            os.mkdir(new_kern_dir)
        except Exception:
            print("Directory {0} already exists".format(new_kern_dir))

    print(kernel)
    remove_kernel_dir(ds_recovery, kern_dir + kernel)

    if not ds_recovery:
        try:
            print("Opening tar file", tarf)
            untar = tarfile.open(tarf)
        except Exception as e:
            print("Kernel " + tarf + " does not exist")
            print(e)
            return
        try:
            print("Untaring file to directory", kern_dir)
            untar.extractall(kern_dir)
            untar.close()
        except Exception:
            print("Kernel " + tarf + " failed to extract")
            return


def make_tinyconfig(
    cross: str,
    arch: str,
    image_dir: str,
    kernel: str,
    logfile: str,
    errfile: str,
) -> None:
    cwd = os.getcwd()
    os.chdir(image_dir)
    print("Changed Directory to ", image_dir)
    print("Cross Compiler", cross, "Kernel", kernel)

    if cu.use_llvm_for_kernel(kernel):
        make_base = "make ARCH={} LLVM=1".format(arch)
    else:
        make_base = "make ARCH={} CROSS_COMPILE={}".format(arch, cross)

    target = "tinyconfig" if kernel >= "linux-3.18" else "allnoconfig"
    print("Using minimal config target:", target)
    cmd = "{} {}".format(make_base, target)

    try:
        tinyconfig_r = subprocess.run(
            cmd,
            shell=True,
            cwd=image_dir,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        print("Done with baseline config ({})".format(target))
    except Exception:
        print("There is an error generating tinyconfig for " + kernel)
        os.chdir(cwd)
        return

    with open(logfile, "w") as f:
        try:
            f.write("Tinyconfig logs: \n")
            f.write(tinyconfig_r.stdout.decode("utf-8"))
            f.write("\n")
        except Exception:
            print("Errors with tinyconfig logs")

    with open(errfile, "w") as f:
        try:
            f.write("Tinyconfig errors: \n")
            f.write(tinyconfig_r.stderr.decode("utf-8"))
            f.write("\n")
        except Exception:
            print("Errors with error files")

    os.chdir(cwd)
    print("Changed Directory back to ", cwd)


def do_compile(
    cross,
    arch,
    image_dir,
    extraversion,
    logfile,
    errfile,
    kernel,
    time,
    ds_recovery,
    single_module_dir,
    new_kern_dir,
) -> None:
    cwd = os.getcwd()
    os.chdir(image_dir)
    print("Changed Directory to ", image_dir)

    vers = kernel.split(".")
    EXTRAVER = (
        "EXTRAVERSION=." + vers[-1] + extraversion
        if len(vers) > 3
        else "EXTRAVERSION=" + extraversion
    )
    print("Extraversion is", EXTRAVER)

    base_flags = _build_flags(kernel, arch, cross, EXTRAVER)

    if not ds_recovery:
        try:
            comp = subprocess.run(
                ["make"] + base_flags + [f"-j{os.cpu_count() or 1}"],
                cwd=image_dir,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            
            if comp.returncode != 0:
                print("❌ MAKE BUILD FAILED WITH ERROR:")
                print(comp.stderr[-2000:])
                raise RuntimeError("Kernel build failed. See stdout/stderr above.")

            print("Done with the kernel compilation")
        except Exception as e:
            print("Error compiling {0}".format(kernel))
            print(e)

        prep_target = "prepare scripts" if kernel >= "linux-2.6.23" else "scripts"
        try:
            subprocess.check_output(
                "make {} {}".format(" ".join(base_flags), prep_target), shell=True
            )
        except Exception:
            print("Make prepare failed in", image_dir)

        mod_arg = "M=scripts/mod" if kernel >= "linux-3.0.0" else "SUBDIRS=scripts/mod"
        try:
            subprocess.check_output(
                "make {} {}".format(" ".join(base_flags), mod_arg), shell=True
            )
        except Exception:
            print("Make scripts/mod failed in", image_dir)

        mod_install = "INSTALL_MOD_PATH=" + new_kern_dir
        try:
            modz = subprocess.run(
                ["make"] + base_flags + [mod_install, "modules_install", "-j8"],
                cwd=image_dir,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            print("Done with module install")
        except Exception as e:
            print("Error with module install")
            print(e)

        with open(logfile, "a") as f:
            try:
                f.write("Compilation{0} logs: \n".format(time))
                f.write(comp.stdout.decode("utf-8"))
                f.write("\n")
                f.write("Compilation{0} module install logs: \n".format(time))
                f.write(modz.stdout.decode("utf-8"))
                f.write("\n")
            except Exception:
                print("Errors with compilation logs")

        with open(errfile, "a") as f:
            try:
                f.write("Compilation{0} errors: \n".format(time))
                f.write(comp.stderr.decode("utf-8"))
                f.write("\n")
                f.write("Compilation{0} module install errors: \n".format(time))
                f.write(modz.stderr.decode("utf-8"))
                f.write("\n")
            except Exception:
                print("Errors with compilation error files")

    else:
        print("In DS recovery mode...Building directory", single_module_dir)
        oldcfg_cmd = 'yes "" | make {} oldconfig'.format(" ".join(base_flags))
        try:
            print(subprocess.check_output(oldcfg_cmd, shell=True).decode("utf-8"))
        except Exception:
            print("Make oldconfig failed in", image_dir)

        prep_target = "prepare scripts" if kernel >= "linux-2.6.23" else "scripts"
        try:
            subprocess.check_output(
                "make {} {}".format(" ".join(base_flags), prep_target), shell=True
            )
        except Exception:
            print("Make prepare failed in", image_dir)

        mod_arg = "M=scripts/mod" if kernel >= "linux-3.0.0" else "SUBDIRS=scripts/mod"
        try:
            subprocess.check_output(
                "make {} {}".format(" ".join(base_flags), mod_arg), shell=True
            )
        except Exception:
            print("Make scripts/mod failed in", image_dir)

        t0 = tm.time()
        try:
            if kernel < "linux-3.0.0":
                subprocess.check_output(
                    "make {} -C {} SUBDIRS={} modules".format(
                        " ".join(base_flags), image_dir, single_module_dir
                    ),
                    shell=True,
                )
            else:
                subprocess.check_output(
                    "make {} -C {} M={} modules".format(
                        " ".join(base_flags), image_dir, single_module_dir
                    ),
                    shell=True,
                )
        except Exception:
            print("Make for target module failed in", image_dir)
        print("Python time for one module compilation", tm.time() - t0)

    os.chdir(cwd)
    print("Changed Directory back to ", cwd)


def parse_symbol_spec(spec: Any) -> Tuple[str, Optional[str], Optional[int]]:
    """
    Normalizes target capability inputs into (symbol_name, relative_file_path, line_number).
    Supported formats:
      - "sym_name"
      - ("sym_name", "kernel/reboot.c")
      - ("sym_name", "kernel/reboot.c", 123)
      - "kernel/reboot.c:123:sym_name" or "kernel/reboot.c:sym_name"
      - "sym_name@kernel/reboot.c:123"
      - {"symbol": "sym_name", "file": "kernel/reboot.c", "line": 123}
    """
    if isinstance(spec, dict):
        return spec.get("symbol", ""), spec.get("file"), spec.get("line")

    if isinstance(spec, (tuple, list)):
        sym = str(spec[0])
        file_p = str(spec[1]) if len(spec) > 1 and spec[1] else None
        line_n = int(spec[2]) if len(spec) > 2 and spec[2] is not None else None
        return sym, file_p, line_n

    if isinstance(spec, str):
        s = spec.strip()
        if "@" in s:
            sym, loc = s.split("@", 1)
            if ":" in loc:
                f, l = loc.split(":", 1)
                return sym.strip(), f.strip(), int(l) if l.isdigit() else None
            return sym.strip(), loc.strip(), None

        if ":" in s:
            parts = s.split(":")
            if len(parts) == 3:
                # e.g., kernel/reboot.c:123:force_store
                if parts[1].isdigit():
                    return parts[2].strip(), parts[0].strip(), int(parts[1])
                # e.g., force_store:kernel/reboot.c:123
                elif parts[2].isdigit():
                    return parts[0].strip(), parts[1].strip(), int(parts[2])
            elif len(parts) == 2:
                # e.g., kernel/reboot.c:force_store
                if parts[0].endswith((".c", ".h", ".S")):
                    return parts[1].strip(), parts[0].strip(), None
                # e.g., force_store:kernel/reboot.c
                elif parts[1].endswith((".c", ".h", ".S")):
                    return parts[0].strip(), parts[1].strip(), None

        return s, None, None

    return str(spec), None, None


def resolve_symbols_to_configs(image_dir: str, symbols: List[Any]) -> List[str]:
    """
    Queries cscope to identify exact file/line matches for input capabilities,
    then locates associated Makefile CONFIG_ flags while excluding collision paths.
    """
    found_configs = set()
    cscope_db = os.path.join(image_dir, "cscope.out")

    if not symbols:
        return []

    if not os.path.exists(cscope_db):
        print(f"  [resolve_symbols] Error: {cscope_db} not found.")
        return []

    for spec in symbols:
        print (spec)
        if not spec:
            continue

        sym, file_spec, line_spec = parse_symbol_spec(spec)
        defined_files = set()

        # Only run expensive cscope searches if sym is a valid C identifier
        if sym and C_IDENTIFIER_RE.match(sym):
            clean_sym = re.sub(r"\.(isra|constprop|part)\.\d+", "", sym).strip()
            for flag in ["-L1", "-L0"]:
                try:
                    res = subprocess.run(
                        ["cscope", "-d", "-f", cscope_db, flag, clean_sym],
                        cwd=image_dir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    for line in res.stdout.strip().splitlines():
                        parts = line.split(maxsplit=3)
                        if len(parts) >= 3:
                            abs_or_rel = parts[0]
                            rel_file = os.path.relpath(abs_or_rel, image_dir)
                            if not rel_file.endswith((".c", ".h", ".S")):
                                continue

                            if file_spec:
                                norm_spec = os.path.normpath(file_spec)
                                norm_rel = os.path.normpath(rel_file)
                                if not norm_rel.endswith(norm_spec):
                                    continue

                            if line_spec is not None:
                                try:
                                    cscope_line = int(parts[2])
                                    if abs(cscope_line - line_spec) > 50:
                                        continue
                                except ValueError:
                                    pass

                            defined_files.add(rel_file)

                    if defined_files:
                        break
                except Exception as e:
                    print(f"  [cscope] Query error for '{clean_sym}': {e}")

        # Direct file fallback: If sym was None/invalid or cscope yielded no hit,
        # jump straight to processing the Makefile for file_spec!
        if not defined_files and file_spec:
            rel_f = (
                os.path.relpath(file_spec, image_dir)
                if os.path.isabs(file_spec)
                else file_spec
            )
            if os.path.exists(os.path.join(image_dir, rel_f)):
                defined_files.add(rel_f)

      # Process Makefiles associated with matched target files by climbing parent directories
        for rel_file in defined_files:
            rel_path = os.path.normpath(rel_file)
            parts = rel_path.split(os.sep)

            if not parts:
                continue

            # Start target as the object file (e.g., 'fs.o')
            target_name = os.path.splitext(parts[-1])[0] + ".o"
            dir_parts = parts[:-1]

            # Ascend the directory tree to collect both target configs and parent folder gates
            while dir_parts:
                curr_dir_rel = os.path.join(*dir_parts)

                makefile_path = os.path.join(image_dir, curr_dir_rel, "Makefile")
                if not os.path.exists(makefile_path):
                    makefile_path = os.path.join(image_dir, curr_dir_rel, "Kbuild")

                if os.path.exists(makefile_path):
                    try:
                        with open(makefile_path, "r", errors="replace") as mf:
                            content = mf.read()

                        # Strip Makefile comments and handle continuation lines (\)
                        content = re.sub(r"#.*", "", content)
                        content = content.replace("\\\n", " ")

                        # 1. Match direct obj-$(CONFIG_FOO) += target.o OR obj-$(CONFIG_FOO) += target_dir/
                        direct_pattern = re.compile(
                            rf"obj-\$\((CONFIG_[A-Za-z0-9_]+)\)\s*\+=\s*.*?\b{re.escape(target_name)}\b"
                        )
                        for cfg in direct_pattern.findall(content):
                            found_configs.add(cfg)

                        # 2. Match composite objects (e.g. ipe-y += fs.o) and check their container gates
                        comp_matches = re.finditer(
                            rf"([a-zA-Z0-9_-]+)-(?:y|objs|\$\((CONFIG_[A-Za-z0-9_]+)\))\s*\+=\s*.*?\b{re.escape(target_name)}\b",
                            content,
                        )
                        for m in comp_matches:
                            if m.group(2):  # Guard directly on the composite line
                                found_configs.add(m.group(2))

                            # Extract container object (e.g. 'ipe.o') and search for its parent CONFIG gate
                            comp_obj = m.group(1) + ".o"
                            comp_pattern = re.compile(
                                rf"obj-\$\((CONFIG_[A-Za-z0-9_]+)\)\s*\+=\s*.*?\b{re.escape(comp_obj)}\b"
                            )
                            for cfg in comp_pattern.findall(content):
                                found_configs.add(cfg)

                    except Exception as e:
                        print(f"  [Makefile] Read error in {makefile_path}: {e}")

                # Set next target to the directory name (e.g. 'ipe/') for parent Makefile evaluation
                target_name = dir_parts[-1] + "/"
                dir_parts.pop()

    return sorted(list(found_configs))

def find_and_cscope(image_dir: str, arch: str) -> None:
    cscope_db = os.path.join(image_dir, "cscope.out")
    force_rebuild = False
    if os.path.exists(cscope_db) and not force_rebuild:
        print("  find_and_cscope: Reusing existing cscope database.")
        return

    cwd = os.getcwd()
    sa = _srcarch(arch)

    try:
        os.chdir(image_dir)

        for db_file in ["cscope.out", "cscope.in.out", "cscope.po.out", "cscope.files"]:
            if os.path.exists(db_file):
                try:
                    os.remove(db_file)
                except OSError:
                    pass

        kconfig_real = os.path.join(image_dir.rstrip("/"), "Kconfig")
        kconfig_backup = kconfig_real + ".firmsolo_bak"

        if os.path.exists(kconfig_real):
            shutil.copy2(kconfig_real, kconfig_backup)

        arch_kconfig = "arch/{}/Kconfig".format(sa)
        if os.path.exists(arch_kconfig):
            os.system("rm -f Kconfig")
            os.system("cp {} Kconfig".format(arch_kconfig))

        print("  find_and_cscope: indexing C source tree via tags.sh (ARCH={})...".format(sa))
        env = os.environ.copy()
        env["ARCH"] = sa

        subprocess.run(
            ["./scripts/tags.sh", "cscope"],
            cwd=image_dir,
            env=env,
            check=False,
            stderr=subprocess.DEVNULL
        )

        if os.path.exists("cscope.files"):
            with open("cscope.files", "a") as f:
                for root, _, files in os.walk("."):
                    for file in files:
                        if file.startswith("Makefile") or file.startswith("Kconfig"):
                            rel_p = os.path.relpath(os.path.join(root, file), ".")
                            f.write(rel_p + "\n")

            subprocess.run(
                ["cscope", "-b", "-q", "-k", "-i", "cscope.files"],
                cwd=image_dir,
                check=True,
                stderr=subprocess.DEVNULL
            )

        print("  find_and_cscope: cscope index built successfully.")

        if os.path.exists(kconfig_backup):
            shutil.move(kconfig_backup, kconfig_real)

    except Exception as e:
        print("Cscope failed:", e)
    finally:
        os.chdir(cwd)


def copy_files(
    image_dir: str, new_kern_dir: str, s_config: str, arch: str = "arm"
) -> None:
    print(
        "Copy files from directory {0} to directory {1}".format(image_dir, new_kern_dir)
    )
    os.system("cp " + image_dir + "vmlinux " + new_kern_dir)

    if s_config == "yes":
        os.system("cp " + image_dir + ".config " + new_kern_dir)
        os.system("cp " + image_dir + "Module.symvers " + new_kern_dir)
        os.system("cp " + image_dir + "System.map " + new_kern_dir)
        os.system("cp " + image_dir + "cscope.files " + new_kern_dir)

    sa = _srcarch(arch)
    _boot_images: Dict[str, str] = {
        "arm": "arch/arm/boot/zImage",
        "arm64": "arch/arm64/boot/Image",
        "x86": "arch/x86/boot/bzImage",
        "mips": "arch/mips/boot/vmlinux.bin",
    }
    boot_img = _boot_images.get(sa)
    if boot_img:
        os.system("cp " + image_dir + boot_img + " " + new_kern_dir)


def save_sym_data(
    image,
    image_dir,
    outfile,
    symbolz,
    time,
    kernel,
    kern_dir,
    new_kern_dir,
    ds_recovery,
):
    unknown: List[str] = []
    try:
        if time == "2":
            symvers, sysmap = exported_syms(image_dir)
        for spec in symbolz:
            sym, _, _ = parse_symbol_spec(spec)
            if time == "2":
                if sym not in symvers and sym not in sysmap:
                    unknown.append(sym)
            else:
                unknown.append(sym)

        mode = "w" if time == "1" else "a"
        line = (
            " Undefined Symbols: \n" if time == "1" else " Final Undefined Symbols: \n"
        )

        if not ds_recovery:
            with open(outfile, mode) as f:
                f.write(str(len(unknown)) + line)
                for ln in unknown:
                    f.write(ln + "\n")
                f.write("\n")
    except Exception:
        print("The kernel did not compile and symvers is not there")
        sys.exit(1)

    return unknown


def apply_patch(image_dir: str, patch: str) -> None:
    print("Applying patch", patch, "to kernel", image_dir)
    cwd = os.getcwd()
    os.chdir(image_dir)
    try:
        subprocess.run(
            "cat {} | patch -p1 -E -d .".format(patch),
            shell=True,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
    except Exception:
        print(traceback.format_exc())
    os.chdir(cwd)


def patch_kernel(image_dir: str, kernel: str) -> None:
    if kernel < "linux-2.6.31":
        return
    patches = os.listdir(cu.openwrt_patch_dir)
    kern_tokens = kernel.split(".")
    kern = ".".join(kern_tokens[:2])
    kern_plus = ".".join(kern_tokens[:3])
    which_patch = None
    for patch in sorted(patches):
        if kernel in patch:
            which_patch = cu.openwrt_patch_dir + patch
            break
    if not which_patch:
        for patch in sorted(patches):
            if kern_plus in patch:
                which_patch = cu.openwrt_patch_dir + patch
                break
        if not which_patch:
            for patch in sorted(patches):
                if kern in patch:
                    which_patch = cu.openwrt_patch_dir + patch
                    break
    if which_patch:
        apply_patch(image_dir, which_patch)


def compile_kernel(
    image,
    ds_options,
    ds_recovery,
    single_module_dir,
    s_config,
    openwrt,
    kernel,
    extraversion,
    modulez,
    ver_magicz,
    symbolz,
    arch,
    endianess,
    cross,
    conf_opts,
    guard_expr,
    module_options,
) -> int:
    kernel = cu.kernel_prefix + kernel
    resultdir = cu.result_dir_path + image + "/"
    new_kern_dir = resultdir + kernel + "/"
    tarf = cu.tar_dir + kernel + ".tar.gz"
    image_dir = cu.kern_dir + kernel + "/"

    cross = cu.get_toolchain(kernel, arch, endianess)

    create_directories(
        kernel,
        resultdir,
        new_kern_dir,
        cu.kern_dir,
        cu.tar_dir,
        tarf,
        ds_recovery,
        s_config,
    )

    if openwrt:
        patch_kernel(image_dir, kernel)

    print("Image_dir = " + image_dir)

    outfile = resultdir + "results.out"
    logfile = resultdir + "logs.out"
    errfile = resultdir + "errors.out"

    if not ds_recovery:
        print("Running Firmsolo in normal mode")

        _setup_build_env(arch, cross, image_dir)

        hot_fixes(image_dir, kernel)
        fix_configs(image_dir, kernel)

        make_tinyconfig(cross, arch, image_dir, kernel, logfile, errfile)

        unknown = save_sym_data(
            image,
            image_dir,
            outfile,
            symbolz,
            "1",
            kernel,
            cu.kern_dir,
            new_kern_dir,
            ds_recovery,
        )

        find_and_cscope(image_dir, arch)

        # Perform symbol-to-config resolution using cscope line-number disambiguation
        resolved_configs = resolve_symbols_to_configs(image_dir, symbolz)
        for cfg in resolved_configs:
            if cfg not in ds_options:
                ds_options.append(cfg)

        try:
            update_config(
                image,
                kernel,
                image_dir,
                resultdir,
                unknown,
                ver_magicz,
                endianess,
                arch,
                modulez,
                conf_opts,
                guard_expr,
                module_options,
                ds_options,
            )
            print("DEBUG ds_options from symbolz translation:", ds_options)
        except Exception:
            print(traceback.format_exc())

    print("Compiling kernel for image", image)
    do_compile(
        cross,
        arch,
        image_dir,
        extraversion,
        logfile,
        errfile,
        kernel,
        "2",
        ds_recovery,
        single_module_dir,
        new_kern_dir,
    )

    if not ds_recovery:
        copy_files(image_dir, new_kern_dir, s_config, arch)
        unknown = save_sym_data(
            image,
            image_dir,
            outfile,
            symbolz,
            "2",
            kernel,
            cu.kern_dir,
            new_kern_dir,
            ds_recovery,
        )

    return 0


def modify_the_vermagic(vermagic, ds_options):
    for option in list(ds_options):
        if option == "CONFIG_SMP":
            if "SMP" not in vermagic:
                vermagic.append("SMP")
            ds_options.remove(option)
        if option == "CONFIG_MODULE_UNLOAD":
            if "mod_unload" not in vermagic:
                vermagic.append("mod_unload")
            ds_options.remove(option)
        if option == "!CONFIG_SMP":
            if "SMP" in vermagic:
                vermagic.remove("SMP")
            ds_options.remove(option)
        if option == "!CONFIG_MODULE_UNLOAD":
            if "mod_unload" in vermagic:
                vermagic.remove("mod_unload")
            ds_options.remove(option)
    return vermagic, ds_options


def run_the_compilation(
    image,
    ds_opt_fl,
    ds_opt_list,
    ds_recovery,
    s_mod_dir,
    s_config,
    override_vermagic,
    openwrt,
    firmadyne,
) -> None:
    if ds_recovery < 0:
        ds_recovery = 0
    if ds_recovery > 1:
        ds_recovery = 1

    ds_options: List[str] = []
    if ds_opt_fl is not None:
        ds_options = cu.read_file(ds_opt_fl)
    elif ds_opt_list:
        ds_options = ds_opt_list

    which_info = [
        "kernel",
        "extraversion",
        "modules",
        "vermagic",
        "symbols",
        "arch",
        "endian",
        "cross",
        "options",
        "guards",
        "module_options",
    ]
    info = cu.get_image_info(image, which_info)

    try:
        dslc = cu.get_image_info(image, ["dslc"])[0] or []
    except Exception:
        dslc = []
    ds_options += dslc

    if firmadyne:
        try:
            ds_options += cu.get_image_info(image, ["fdyne_dslc"])[0] or []
        except Exception:
            print(
                "The image does not have any DSLC solutions for Firmadyne experiments"
            )

    print("Vermagic", info[3])
    if override_vermagic:
        info[3], ds_options = modify_the_vermagic(info[3], ds_options)

    print(info[0], info[1], info[5], info[3], info[7])
    compile_kernel(image, ds_options, ds_recovery, s_mod_dir, s_config, openwrt, *info)

# Valid C identifier regex: starts with a letter/underscore, followed by letters/digits/underscores
C_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def find_caller_configs(image_dir: str, target_symbol: str) -> List[str]:
    """Finds functions/files calling or referencing `target_symbol` using cscope.
    Tries -L -3 (strict calling functions) first, falling back to -L -0 (symbol references)
    if no direct call sites are found (e.g. macro wrappers, inline headers).
    """
    cscope_db = os.path.join(image_dir, "cscope.out")
    if not os.path.exists(cscope_db):
        print(f"Error: {cscope_db} not found. Run find_and_cscope first.")
        return []

    caller_specs = []
    C_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

    def _parse_cscope_output(
        raw_stdout: str, image_dir: str
    ) -> List[Tuple[Optional[str], str, Optional[int]]]:
        """Parses raw cscope output into (func, rel_file, line_num) tuples."""
        results = []
        if not raw_stdout:
            return results

        real_image_dir = os.path.realpath(image_dir)

        for line in raw_stdout.strip().splitlines():
            parts = line.split(maxsplit=3)
            if len(parts) < 3:
                continue

            abs_file = os.path.realpath(
                os.path.join(real_image_dir, parts[0])
                if not os.path.isabs(parts[0])
                else parts[0]
            )
            rel_file = os.path.relpath(abs_file, real_image_dir)

            if not rel_file.endswith((".c", ".h", ".S")):
                continue

            func_name = parts[1].strip("`'\"")
            line_num = int(parts[2]) if parts[2].isdigit() else None

            results.append((func_name, rel_file, line_num))

        return results

    try:
        # Step 1: Match your working terminal command (no -f flag, let cwd handle database location)
        res = subprocess.run(
            ["cscope", "-d", "-L3", target_symbol],
            cwd=image_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if res.stderr.strip():
            print(f"  [cscope stderr (-3)]: {res.stderr.strip()}")

        caller_specs = _parse_cscope_output(res.stdout, image_dir)

        # Step 2: Fallback to -L0 if -L3 returned 0 results
        if not caller_specs:
            print(f"  [-L3 returned 0 hits for '{target_symbol}'] Falling back to -L0 (symbol references)...")
            res_l0 = subprocess.run(
                ["cscope", "-d", "-L0", target_symbol],
                cwd=image_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if res_l0.stderr.strip():
                print(f"  [cscope stderr (-0)]: {res_l0.stderr.strip()}")

            caller_specs = _parse_cscope_output(res_l0.stdout, image_dir)

    except Exception as e:
        print(f"Error searching references for '{target_symbol}': {e}")
        return []

    print(
        f"Found {len(caller_specs)} sites referencing '{target_symbol}'. Resolving configs..."
    )
    return resolve_symbols_to_configs(image_dir, caller_specs)

if __name__ == "__main__":
    parser = argp.ArgumentParser(description="Compile the FS kernel for an image")
    parser.add_argument("image")
    parser.add_argument("-f", "--ds_opt_fl", default=None)
    parser.add_argument("-l", "--ds_opt_list", nargs="*", default=[])
    parser.add_argument("-d", "--ds_recovery", type=int, default=0)
    parser.add_argument("-m", "--s_mod_dir", default="")
    parser.add_argument("-s", "--s_config", default="yes")
    parser.add_argument("-o", "--override_vermagic", action="store_true")
    parser.add_argument("-w", "--openwrt", action="store_true")
    parser.add_argument("-e", "--firmadyne", action="store_true")

    res = parser.parse_args()
    run_the_compilation(
        res.image,
        res.ds_opt_fl,
        res.ds_opt_list,
        res.ds_recovery,
        res.s_mod_dir,
        res.s_config,
        res.override_vermagic,
        res.openwrt,
        res.firmadyne,
    )

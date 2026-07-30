#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
import sys
from typing import Dict, List, Optional

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
    """
    Return the make-variable list for this kernel.
    >= linux-5.0 with clang+lld → LLVM=1 (no CROSS_COMPILE).
    Older or GCC-only → CROSS_COMPILE=<prefix>.
    """
    if cu.use_llvm_for_kernel(kernel):
        return ["ARCH={}".format(arch), "LLVM=1", extraver]
    else:
        return ["ARCH={}".format(arch), "CROSS_COMPILE={}".format(cross), extraver]


def _setup_build_env(arch: str, cross: str, image_dir: str) -> None:
    """
    Set env vars needed by scripts/Kconfig.include and kconfiglib before
    any make or kconfiglib invocation.
    """
    srcarch = _srcarch(arch)
    os.environ["ARCH"] = arch
    os.environ["SRCARCH"] = srcarch
    real_dir = os.path.realpath(image_dir.rstrip("/"))
    os.environ["srctree"] = real_dir
    os.environ["abs_srctree"] = real_dir

    cc = cu.get_cc_for_kconfig(arch, cross)
    os.environ["CC"] = cc
    print("  build_env: CC={}  ARCH={}  SRCARCH={}".format(cc, arch, srcarch))

    # AS for $(as-version) probes – prefer GNU binutils
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
    """
    Apply a tinyconfig (or allnoconfig fallback for <3.18) to the kernel source directory.
    """
    cwd = os.getcwd()
    os.chdir(image_dir)
    print("Changed Directory to ", image_dir)
    print("Cross Compiler", cross, "Kernel", kernel)

    if cu.use_llvm_for_kernel(kernel):
        make_base = "make ARCH={} LLVM=1".format(arch)
    else:
        make_base = "make ARCH={} CROSS_COMPILE={}".format(arch, cross)

    # tinyconfig target was added in Linux ~3.18; fallback to allnoconfig for older versions
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
                print(comp.stderr[-2000:])  # Print the last 2000 characters of the error log
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


def resolve_symbols_to_configs(image_dir: str, symbols: List[str]) -> List[str]:
    found_configs = set()
    cscope_db = os.path.join(image_dir, "cscope.out")

    if not symbols:
        return []

    if not os.path.exists(cscope_db):
        print(f"  [resolve_symbols] Error: {cscope_db} not found.")
        return []

    for sym in symbols:
        if not sym:
            continue
            
        clean_sym = re.sub(r"\.(isra|constprop|part)\.\d+", "", sym).strip()
        defined_files = set()

        # Query cscope using -L1 (definitions) and -L0 (symbol references)
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
                    parts = line.split()
                    if parts:
                        abs_or_rel = parts[0]
                        # Convert absolute paths to relative paths against image_dir
                        rel_file = os.path.relpath(abs_or_rel, image_dir)
                        if rel_file.endswith((".c", ".h", ".S")):
                            defined_files.add(rel_file)
                if defined_files:
                    break
            except Exception as e:
                print(f"  [cscope] Query error for '{clean_sym}': {e}")

        for rel_file in defined_files:
            dirname = os.path.dirname(rel_file)
            filename = os.path.basename(rel_file)
            obj_name = os.path.splitext(filename)[0] + ".o"

            makefile_path = os.path.join(image_dir, dirname, "Makefile")
            if not os.path.exists(makefile_path):
                continue

            try:
                with open(makefile_path, "r", errors="replace") as mf:
                    makefile_content = mf.read()

                # Case A: Direct hit (obj-$(CONFIG_FOO) += file.o)
                for line in makefile_content.splitlines():
                    if obj_name in line:
                        cfgs = re.findall(r"CONFIG_[A-Za-z0-9_]+", line)
                        for cfg in cfgs:
                            found_configs.add(cfg)

                # Updated Case B in firm_kern_comp.py:
                composite_vars = re.findall(r"([A-Za-z0-9_-]+)-(?:y|objs)\s*[:\+]?=", makefile_content)
                for var_prefix in composite_vars:
                    pattern = rf"{var_prefix}-(?:y|objs)\s*[:\+]?=.*?\b{re.escape(obj_name)}\b"
                    if re.search(pattern, makefile_content, re.DOTALL):
                        parent_obj = var_prefix + ".o"
                        for line in makefile_content.splitlines():
                            if parent_obj in line:
                                for cfg in re.findall(r"CONFIG_[A-Za-z0-9_]+", line):
                                    found_configs.add(cfg)

            except Exception as e:
                print(f"  Error parsing Makefile in {dirname}: {e}")

    return sorted(list(found_configs))

def find_and_cscope(image_dir: str, arch: str) -> None:
    
    cscope_db = os.path.join(image_dir, "cscope.out")
    force_rebuild = False
    # If database already exists and rebuild isn't forced, skip!
    if os.path.exists(cscope_db) and not force_rebuild:
        print("  find_and_cscope: Reusing existing cscope database.")
        return

    """Build a clean, reliable cscope index including source files and Makefiles."""
    cwd = os.getcwd()
    sa = _srcarch(arch)

    try:
        os.chdir(image_dir)

        # 1. Clean up stale database files
        for db_file in ["cscope.out", "cscope.in.out", "cscope.po.out", "cscope.files"]:
            if os.path.exists(db_file):
                try:
                    os.remove(db_file)
                except OSError:
                    pass

        # 2. Preserve Kconfig backup logic
        kconfig_real = os.path.join(image_dir.rstrip("/"), "Kconfig")
        kconfig_backup = kconfig_real + ".firmsolo_bak"

        if os.path.exists(kconfig_real):
            shutil.copy2(kconfig_real, kconfig_backup)

        arch_kconfig = "arch/{}/Kconfig".format(sa)
        if os.path.exists(arch_kconfig):
            os.system("rm -f Kconfig")
            os.system("cp {} Kconfig".format(arch_kconfig))

        # 3. Generate base source file list via tags.sh
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

        # 4. Append Makefiles and Kconfigs to cscope.files and generate inverted index
        if os.path.exists("cscope.files"):
            with open("cscope.files", "a") as f:
                for root, _, files in os.walk("."):
                    for file in files:
                        if file.startswith("Makefile") or file.startswith("Kconfig"):
                            rel_p = os.path.relpath(os.path.join(root, file), ".")
                            f.write(rel_p + "\n")

            # Build fast inverted cscope database (-b -q -k)
            subprocess.run(
                ["cscope", "-b", "-q", "-k", "-i", "cscope.files"],
                cwd=image_dir,
                check=True,
                stderr=subprocess.DEVNULL
            )

        print("  find_and_cscope: cscope index built successfully.")

        # 5. Restore original Kconfig
        if os.path.exists(kconfig_backup):
            shutil.move(kconfig_backup, kconfig_real)

    except Exception as e:
        print("Cscope failed:", e)
    finally:
        os.chdir(cwd)

def copy_files(
    image_dir: str, new_kern_dir: str, s_config: str, arch: str = "arm"
) -> None:
    """Copy build artefacts to the results directory."""
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
        for sym in symbolz:
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
    print(outfile)
    logfile = resultdir + "logs.out"
    print(logfile)
    errfile = resultdir + "errors.out"
    print(errfile)

    if not ds_recovery:
        print("Running Firmsolo in normal mode")

        # Set CC / ARCH / SRCARCH / AS in env before any Kconfig work
        _setup_build_env(arch, cross, image_dir)

        hot_fixes(image_dir, kernel)
        fix_configs(image_dir, kernel)

        # Initialize from tinyconfig (minimal config baseline) instead of a vendor defconfig
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

        # Translate symbols to CONFIG_ options and append them to ds_options
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

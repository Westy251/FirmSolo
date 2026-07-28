#!/usr/bin/env python3

from __future__ import annotations

import os
import re
import shutil
import sys
import subprocess
from typing import Dict, List, Optional
from kconfiglib import (
    Kconfig,
    Symbol,
    Choice,
    MENU,
    COMMENT,
    TRI_TO_STR,
    STRING,
    BOOL,
    TRISTATE,
    HEX,
    AND,
    OR,
    NOT,
    EQUAL,
    UNEQUAL,
    standard_kconfig,
    expr_str,
    expr_items,
    expr_value,
    KconfigError,
)
from pathlib import Path
import pickle


########## Manually Set Config Options #################
ver_tokens = [
    "mod_unload",
    "MIPS32_R1",
    "32BIT",
    "MIPS32_R2",
    "preempt",
    "modversions",
    "64KB",
    "ARMv5",
    "ARMv6",
    "ARMv7",
    "p2v8",
]
vermagic_opts = {
    "mod_unload": "CONFIG_MODULE_UNLOAD",
    "MIPS32_R1": "CONFIG_CPU_MIPS32_R1",
    "MIPS32_R2": "CONFIG_CPU_MIPS32_R2",
    "preempt": "CONFIG_PREEMPT",
    "modversions": "CONFIG_MODVERSIONS",
    "64KB": "CONFIG_PAGE_SIZE_64KB",
    "ARMv5": "CONFIG_ARCH_VERSATILE CONFIG_CPU_ARM926T",
    "ARMv6": "CONFIG_ARCH_REALVIEW "
    "CONFIG_REALVIEW_EB_ARM11MP "
    "CONFIG_MACH_REALVIEW_PB11MP "
    "CONFIG_REALVIEW_EB_ARM11MP_REVB CONFIG_CPU_V6",
    "ARMv7": "CONFIG_ARCH_REALVIEW CONFIG_MACH_REALVIEW_EB "
    "CONFIG_REALVIEW_EB_A9MP "
    "CONFIG_MACH_REALVIEW_PBA8 "
    "CONFIG_MACH_REALVIEW_PBX CONFIG_CPU_V7",
    "p2v8": "CONFIG_ARM_PATCH_PHYS_VIRT",
    "SMP": "CONFIG_SMP",
}

preempt = ["CONFIG_PREEMPT_NONE", "CONFIG_PREEMPT_VOLUNTARY", "CONFIG_PREEMPT"]
arch_spec = ["SMP", "MT_SMTC"]
arch_spec_match = ["MT_SMP", "MT_SMTC"]

core_nf_modules = [
    "x_tables.ko",
    "nf_conntrack.ko",
    "ip_tables.ko",
    "iptable_filter.ko",
    "iptable_nat.ko",
]
core_nf_options = [
    "CONFIG_NETFILTER_XTABLES",
    "CONFIG_NF_CONNTRACK",
    "CONFIG_IP_NF_IPTABLES",
    "CONFIG_IP_NF_FILTER",
    "CONFIG_NF_NAT",
]


# ── SRCARCH mapping (mirrors kernel Makefile) ─────────────────────────────────
_SRCARCH_MAP: Dict[str, str] = {
    "x86_64": "x86",
    "i386": "x86",
    "sparc64": "sparc",
    "sparc32": "sparc",
    "sh64": "sh",
    "tilegx": "tile",
    "tilepro": "tile",
}


# ── Build-macro sets (same logic as fix_kconf.py) ─────────────────────────────
_VERSION_MACROS = frozenset(
    {
        "as-version",
        "as-major-version",
        "cc-version",
        "cc-major-version",
        "ld-version",
        "ld-major-version",
        "binutils-version",
        "rustc-version",
        "clang-version",
    }
)

_OPTION_MACROS = frozenset(
    {
        "cc-option",
        "cc-disable-warning",
        "cc-option-yn",
        "cc-option-align",
        "cc-ifversion",
        "cc-ldoption",
        "ld-option",
        "ld-ifversion",
        "as-option",
        "as-instr",
        "success",
        "if-success",
        "if-failure",
    }
)

_KNOWN_BUILD_MACROS = _VERSION_MACROS | _OPTION_MACROS


def _replace_build_macros(text: str) -> str:
    """
    Replace kernel Kconfig build-tool macros:
      $(xxx-version)      → '0'  (integer default context)
      $(cc-option,...) etc→ 'n'  (boolean expression context)

    Heuristic: any $(name) where name contains a hyphen is a build macro.
    kconfiglib built-ins (shell, info, warning, filename, …) have no hyphens.
    """
    result = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "$" and i + 1 < n and text[i + 1] == "(":
            j = i + 2
            while j < n and (text[j].isalnum() or text[j] == "-"):
                j += 1
            macro_name = text[i + 2 : j]

            is_build = macro_name in _KNOWN_BUILD_MACROS or (
                "-" in macro_name and len(macro_name) > 1
            )

            if is_build and j < n and text[j] in (",", ")"):
                depth, k = 1, j
                while k < n and depth:
                    if text[k] == "(":
                        depth += 1
                    elif text[k] == ")":
                        depth -= 1
                    k += 1

                if (
                    macro_name in _VERSION_MACROS
                    or macro_name.endswith("-version")
                    or macro_name.endswith("-major-version")
                ):
                    result.append("0")
                else:
                    result.append("n")
                i = k
                continue

        result.append(text[i])
        i += 1
    return "".join(result)


def _preprocess_kconfig_macros(kern_dir: str) -> None:
    """
    Fallback: walk every Kconfig file and rewrite build macros in-place.

    IMPORTANT: Kconfig.include files are handled by
    _patch_kconfig_include_file_kcre — they must NOT be processed by
    the generic _replace_build_macros which would turn top-level macro
    calls into bare 'n' (a kconfiglib syntax error).
    """
    count = 0
    inc_count = 0
    for dirpath, _, filenames in os.walk(kern_dir):
        for fn in filenames:
            if fn != "Kconfig" and not fn.startswith("Kconfig."):
                continue
            fpath = os.path.join(dirpath, fn)

            # Patch Kconfig.include files with targeted stubs
            if fn == "Kconfig.include":
                try:
                    if _patch_kconfig_include_file_kcre(fpath):
                        inc_count += 1
                except OSError:
                    pass
                continue

            # Generic macro replacement for all other Kconfig files
            try:
                with open(fpath, "r", errors="replace") as f:
                    original = f.read()
                replaced = _replace_build_macros(original)
                if replaced != original:
                    with open(fpath, "w") as f:
                        f.write(replaced)
                    count += 1
            except OSError:
                pass

    if inc_count:
        print(
            "[kcre] patched {} Kconfig.include file(s) with safe macro stubs".format(
                inc_count
            )
        )
    if count:
        print("[kcre] preprocessed build macros in {} Kconfig file(s)".format(count))


def _setup_kconfig_env(arch: str, cross: str, kern_dir: str) -> None:
    """
    Set environment variables that scripts/Kconfig.include requires for
    $(cc-option,...) and $(as-version) probes to work.

    Key fixes for x86_64:
      * SRCARCH is mapped correctly (x86_64 → x86)
      * AS is set so $(as-version) can call the assembler
    """
    # FIXED: use correct SRCARCH for kconfiglib to find arch/<srcarch>/Kconfig
    srcarch = _SRCARCH_MAP.get(arch, arch)
    os.environ["ARCH"] = arch
    os.environ["SRCARCH"] = srcarch

    real_dir = os.path.realpath(kern_dir.rstrip("/"))
    os.environ["srctree"] = real_dir
    os.environ["abs_srctree"] = real_dir

    # CC for $(cc-option,...) probes
    cc_candidates = []
    if cross:
        cc_candidates.append(cross + "gcc")
    cc_candidates += ["clang", "gcc", "cc"]
    for cand in cc_candidates:
        found = shutil.which(cand)
        if found:
            os.environ["CC"] = found
            print("[kcre] kconfiglib CC = {}".format(found))
            break

    # AS for $(as-version) probes — prefer GNU binutils 'as' since the
    # version-string parsing in scripts/Kconfig.include expects its format
    for cand in ["as", "x86_64-linux-gnu-as", "arm-linux-gnueabi-as", "llvm-as"]:
        found = shutil.which(cand)
        if found:
            os.environ.setdefault("AS", found)
            break

    # LD / LLVM flags
    lld = shutil.which("ld.lld")
    if lld:
        os.environ.setdefault("LD", lld)
        os.environ.setdefault("LLVM", "1")


# ── Image class ───────────────────────────────────────────────────────────────


class Image:
    def __init__(self, kconf, image, module_configs, arch):
        self.kconf = kconf
        self.image = image
        self.module_configs = module_configs
        self.arch = arch

    def split_expr(self, expr, op):
        res = []

        def rec(subexpr):
            if subexpr.__class__ is tuple and subexpr[0] is op:
                if subexpr[0] is NOT:
                    if subexpr[1].__class__ is tuple:
                        rec(subexpr[1])
                    else:
                        res.append(subexpr[1])
                else:
                    rec(subexpr[1])
                if (
                    subexpr[0] is not NOT
                    and subexpr[0] is not EQUAL
                    and subexpr[0] is not UNEQUAL
                ):
                    rec(subexpr[2])
            else:
                if op is not NOT:
                    res.append(subexpr)

        rec(expr)
        return res

    def tuple_case(self, target, term):
        print("\t The dependency {0} is a tuple".format(expr_str(term)))
        if term[0] == NOT:
            return
        if term[0] != EQUAL and term[0] != UNEQUAL:
            self._split_expr_info(target, term)
            return
        if "!=" in expr_str(term):
            print("\tOperator UNEQUAL in expression", expr_str(term))
            if '"n"' in expr_str(term):
                self.set_option_value(term[1].name, 2)
            elif '"y"' in expr_str(term):
                self.set_option_value(term[1].name, 0)
            else:
                if term[2].str_value == "y":
                    self.set_option_value(term[1].name, 0)
                else:
                    self.set_option_value(term[1].name, 2)
            return
        if "=" in expr_str(term):
            print("\tCase of EQUAL for expr", expr_str(term))
            if '"n"' in expr_str(term):
                self.set_option_value(term[1].name, 0)
            elif '"y"' in expr_str(term):
                self.set_option_value(term[1].name, 2)
            else:
                if term[2].str_value == "y":
                    self.set_option_value(term[1].name, 2)
                else:
                    self.set_option_value(term[1].name, 0)
            return

    def get_operators(self, expr):
        if len(self.split_expr(expr, AND)) > 1:
            return AND, "&&"
        if len(self.split_expr(expr, NOT)) != 0:
            return NOT, "!"
        return OR, "||"

    def val_setting(self, term, op_str):
        if op_str == "!":
            print(
                "Operant == NOT...Setting to n",
                term.name,
                "Visibility",
                term.visibility,
                "Assignable",
                term.assignable,
                "Dependencies",
                term.direct_dep,
            )
            if term.tri_value == 0:
                pass
            else:
                print("Value equals to 2...setting to 0")
                self.set_option_value(term.name, 0)
                if term.tri_value != 0:
                    print("Trying to force the value to 0")
                    self.set_undefined_option(term, 0)

    def _split_expr_info(self, target, expr):
        split_op, op_str = self.get_operators(expr)

        for _i, term in enumerate(self.split_expr(expr, split_op)):
            if isinstance(target, tuple):
                if expr_value(target) > 0:
                    print(
                        "We made target expression {0} true...returning!".format(
                            expr_str(target)
                        )
                    )
                    return
            else:
                if target.visibility > 0:
                    print("We made target {0} visible".format(target.name))
                    if not isinstance(term, tuple) and term.tri_value == 0:
                        self.set_option(term.name, 2)
                    return

            if isinstance(term, tuple):
                if expr_value(term) > 0 and term[0] != NOT:
                    continue
                if term[0] == NOT and len(term) == 2:
                    if isinstance(term[1], tuple):
                        continue
                    else:
                        self.val_setting(term[1], "!")
                        continue
                self.tuple_case(target, term)
            else:
                if isinstance(term, str):
                    print(
                        "Simple Symbol/String dep {} not breaking further down".format(
                            term
                        )
                    )
                    self.set_option(term, 2)
                else:
                    if term.name is None:
                        return
                    print(
                        "Simple dep {} with value {}".format(term.name, term.tri_value)
                    )
                    if op_str == "!":
                        self.val_setting(term, op_str)
                    else:
                        if term != self.kconf.y and term.tri_value == 0:
                            self.set_option(term.name, 2)
        return

    # ── version magic ─────────────────────────────────────────────────────────

    def set_ver_magic(self, ver_magic):
        print("VERMAGIC", ver_magic)
        ARCH = ""
        release = ""
        for token in ver_magic:
            if token in vermagic_opts:
                if "MIPS" in token:
                    release = token
                    ARCH = "MIPS"
                elif "ARM" in token:
                    release = token
                    ARCH = "ARM"

        print("RELEASE", release)
        try:
            option = vermagic_opts[release]
        except Exception:
            option = ""

        for opt in option.split():
            self.set_option(opt, 2)

        if ARCH == "ARM" and release >= "ARMv5":
            if release > "ARMv5":
                self.set_option("CONFIG_CPU_ARM926T", 0)
                self.set_option("CONFIG_CPU_32v5", 0)
            if release == "ARMv6":
                self.set_option("REALVIEW_EB_A9MP", 0)
                self.set_option("MACH_REALVIEW_PBA8", 0)
                self.set_option("CONFIG_CPU_V7", 0)
                if "p2v8" not in ver_magic:
                    self.set_option("CONFIG_REALVIEW_HIGH_PHYS_OFFSET", 0)
                    self.set_option("CONFIG_ARM_PATCH_PHYS_VIRT", 0)
            if release == "ARMv7":
                self.set_option("CONFIG_MACH_REALVIEW_PB1176", 0)
                self.set_option("CONFIG_MACH_REALVIEW_PB11MP", 0)
                self.set_option("CONFIG_REALVIEW_EB_ARM11MP", 0)
                self.set_option("CONFIG_CPU_V6", 0)
                self.set_option("CONFIG_CPU_V6K", 0)
                if "p2v8" not in ver_magic:
                    self.set_option("CONFIG_REALVIEW_HIGH_PHYS_OFFSET", 0)

        for token in ver_magic:
            if token in vermagic_opts:
                options = vermagic_opts[token]
                for opt in options.split():
                    print("VER_TOKEN", option)
                    self.set_option(opt, 2)
            if token in arch_spec:
                self.set_option(token, 2)
                indx = arch_spec.index(token)
                option = ARCH + "_" + arch_spec_match[indx]
                self.set_option(option, 2)

        if ARCH == "MIPS":
            self.set_option("MIPS_MALTA", 2)

        for token in ver_tokens:
            if token not in ver_magic:
                if token == "preempt":
                    self.set_option("CONFIG_PREEMPT_NONE", 2)
                    continue
                if token == "modversions":
                    self.set_option("CONFIG_MODVERSIONS", 0)
                    continue
                if token == "mod_unload":
                    self.set_option("CONFIG_MODULE_UNLOAD", 0)
                    continue

        flag = any(t in ver_magic for t in arch_spec)
        if not flag:
            if ARCH == "MIPS":
                self.set_option("CONFIG_MIPS_MT_DISABLED", 2)
                self.set_option("CONFIG_SYS_SUPPORTS_MULTITHREADING", 0)
            self.set_option("CONFIG_SMP", 0)

        # Second pass
        if ARCH == "ARM" and release >= "ARMv5":
            if release > "ARMv5":
                self.set_option("CONFIG_CPU_ARM926T", 0)
                self.set_option("CONFIG_CPU_32v5", 0)
            if release == "ARMv6":
                self.set_option("REALVIEW_EB_A9MP", 0)
                self.set_option("MACH_REALVIEW_PBA8", 0)
                self.set_option("CONFIG_CPU_V7", 0)
                if "p2v8" not in ver_magic:
                    self.set_option("CONFIG_REALVIEW_HIGH_PHYS_OFFSET", 0)
                    self.set_option("CONFIG_ARM_PATCH_PHYS_VIRT", 0)
            if release == "ARMv7":
                self.set_option("CONFIG_MACH_REALVIEW_PB1176", 0)
                self.set_option("CONFIG_MACH_REALVIEW_PB11MP", 0)
                self.set_option("CONFIG_REALVIEW_EB_ARM11MP", 0)
                self.set_option("CONFIG_CPU_V6", 0)
                self.set_option("CONFIG_CPU_V6K", 0)
                if "p2v8" not in ver_magic:
                    self.set_option("CONFIG_REALVIEW_HIGH_PHYS_OFFSET", 0)

    def get_min_value(self, select_list):
        min_value = 2
        for select in select_list:
            if isinstance(select, tuple):
                value = expr_value(select[0])
            else:
                value = select.tri_value
            if value < min_value:
                min_value = value
        return min_value

    # ── core value setters ────────────────────────────────────────────────────

    def set_option_value(self, option, value, overwrite=False):
        if option not in self.kconf.syms.keys():
            print("Option", option, "does not exist")
            return
        if self.arch == "arm" and option == "GPIOLIB":
            return
        if isinstance(value, str):
            self.kconf.syms[option].set_value(value)
            return
        if not overwrite:
            if (
                "CONFIG_" + option in self.module_configs
                and self.kconf.syms[option].tri_value == 1
                and value > 1
            ):
                print("Trying to set a module_config option")
                return
        print("In set_option_value...setting sym option", option, " to value", value)
        self.kconf.syms[option].set_value(value)
        print("New value of option", option, " is", self.kconf.syms[option].tri_value)

    def set_option(self, conf_opt, value, overwrite=False):
        conf_opt = conf_opt.replace("subst m,y,$(", "").strip("+=")
        option = (
            conf_opt.replace("CONFIG_", "") if conf_opt != "IKCONFIG_PROC" else conf_opt
        )

        if option == "XFRM" and value > 0:
            option = "INET_XFRM_MODE_TUNNEL"

        if (
            option == "NF_CONNTRACK"
            and value > 0
            and "NF_CONNTRACK_ENABLED" in self.kconf.syms
        ):
            self.set_option_value("NF_CONNTRACK_ENABLED", 2, overwrite)

        print("Setting Option", option, conf_opt, "to value", value)

        try:
            sym = self.kconf.syms[option]
        except Exception:
            print("Conf option", option, "does not exist in the tree")
            return

        if isinstance(sym, (Symbol, Choice)):
            print(
                "Symbol:",
                sym.name,
                "Is assignable:",
                sym.assignable,
                "Is visible:",
                sym.visibility,
                "Dependency:",
                expr_str(sym.direct_dep),
            )
            if sym.tri_value == value:
                print("SYMBOL", sym.name, "already has value", value)
                return
            if value == 0:
                self.set_option_value(sym.name, 0)
                return
            if value not in range(0, 4):
                self.set_option_value(sym.name, value)
                return
            if sym.type == BOOL and value == 1:
                print("Type of symbol", sym.name, "is bool, cannot be set to 1")
                return

            if value in sym.assignable:
                print("The value {} can be set directly for {}".format(value, option))
                self.set_option_value(sym.name, value, overwrite)
                print(
                    "Config string for {} is {} visibility {}".format(
                        sym.name, sym.config_string, sym.visibility
                    )
                )
                return

            self._split_expr_info(sym, sym.direct_dep)
            print(
                "Dependencies of {} have value {}".format(
                    sym.name, expr_value(sym.direct_dep)
                )
            )

            if sym.visibility == 0:
                print("Still not visible...trying reverse deps for", sym.name)
                self.set_undefined_option(sym, value, overwrite)
            else:
                self.set_option_value(sym.name, min(value, sym.visibility), overwrite)

            print(
                "Config string for {} is {} visibility {} assignable {}".format(
                    sym.name, sym.config_string, sym.visibility, sym.assignable
                )
            )

            if sym.choice:
                parent = sym.choice
                for s in parent.syms:
                    if s is not parent.user_selection and s.visibility:
                        self.set_option_value(s.name, 0, overwrite)

    def set_undefined_option(self, opt, value, overwrite=False):
        if (opt.type != TRISTATE and value == 1) or opt.name not in self.kconf.syms:
            print("Option", opt.name, "is bool and cannot be set to 1")
            return
        rev_deps = opt.rev_dep
        print("Reverse dependencies of symbol", opt.name, "are", expr_str(rev_deps))
        or_break = self.split_expr(rev_deps, OR)
        for subexpr in or_break:
            print("Checking REV dep", expr_str(subexpr))
            if expr_value(subexpr) == 0 and value == 0:
                continue
            and_break = self.split_expr(subexpr, AND)
            for term in and_break:
                if isinstance(term, tuple):
                    if expr_value(term) == 0:
                        break
                    else:
                        continue
                elif isinstance(term, (Symbol, Choice)):
                    print(
                        "Looking at rev dep of",
                        opt.name,
                        ":",
                        term.name,
                        "with_value",
                        term.tri_value,
                    )
                    if (
                        term.tri_value > 0
                        and value > 0
                        and value != term.tri_value
                        and opt.tri_value != value
                    ):
                        if not term.name:
                            continue
                        self.set_option_value(term.name, value, overwrite)
                        if term.tri_value != value:
                            self.set_undefined_option(term, value, overwrite)
                        if expr_value(subexpr) > 0:
                            break
                    elif (
                        expr_value(term.direct_dep) >= 0
                        and term.tri_value == 0
                        and value == 1
                    ):
                        if not term.name:
                            continue
                        self.set_option_value(term.name, value, overwrite)
                        if term.tri_value != value:
                            self.set_undefined_option(term, value, overwrite)
                        if expr_value(subexpr) > 0:
                            break
                    elif (
                        expr_value(term.direct_dep) > 0
                        and term.tri_value == 0
                        and value > 0
                    ):
                        if value in term.assignable:
                            self.set_option_value(term.name, value, overwrite)
                        else:
                            self.set_option_value(
                                term.name, expr_value(term.direct_dep)
                            )
                    elif (
                        expr_value(term.direct_dep) > 0
                        and term.tri_value > 0
                        and value == 0
                    ):
                        if term.name and term.name in ("NET", "INET"):
                            continue
                        if value in term.assignable:
                            term.set_value(value)
                            if value in opt.assignable:
                                self.set_option_value(opt.name, value, True)
                                return
                        if term.tri_value != value:
                            self.set_undefined_option(term, value, True)

                if opt.tri_value == value:
                    print("Value of", opt.name, "is", opt.tri_value, "...Returning")
                    return

        if opt.visibility > 0:
            self.set_option_value(opt.name, value, overwrite)

    def set_upstream_modules(self, module_configs):
        print("Setting options related to custom modules to M")
        for opt in module_configs:
            option = opt.replace("CONFIG_", "")
            try:
                sym = self.kconf.syms[option]
            except Exception:
                print("Conf option", option, "does not exist in the tree")
                continue
            if isinstance(sym, (Symbol, Choice)):
                if sym.tri_value != 1:
                    self.set_option(sym.name, 1)
                    if sym.tri_value != 1:
                        self.set_option_value(sym.name, 1)


# ── Module-option discovery ───────────────────────────────────────────────────


def find_custom_module_options(modulez):
    print("Finding config options for upstream counterpart modules")
    module_options = []
    for mod in modulez:
        module = mod.split("/")[-1].replace(".ko", ".o")
        try:
            option = subprocess.check_output(
                'cscope -d -L6"{}"'.format(module), shell=True
            ).decode("utf-8")
        except Exception:
            print("The module", module, "does not exist in the upstream kernel source")
            continue

        opt = ""
        for line in option.split("\n"):
            if line.split(" ")[-1] == module:
                opt = line
                break

        if opt:
            if "subst" in opt:
                opt_temp = opt.replace("obj-$(subst y,", "").replace("$(subst m,y,", "")
                for m in re.findall(r"\$\(.*?\)", opt_temp):
                    conf_opt = m.strip("$()")
                    if conf_opt not in module_options:
                        module_options.append(conf_opt)
            else:
                conf_opt = opt.split("-")[1].split(")")[0].strip("$(")
                if conf_opt not in module_options:
                    module_options.append(conf_opt)

    print("Module options", module_options)
    return module_options


# ── Core configuration logic ──────────────────────────────────────────────────


def _patch_kconfig_include_file_kcre(fpath: str) -> bool:
    """
    Same logic as fix_kconf._patch_kconfig_include_file but self-contained
    so kcre.py has no import dependency on fix_kconf.
    Called only from the fallback path in update_config.
    """
    _V = frozenset(
        {
            "as-version",
            "as-major-version",
            "cc-version",
            "cc-major-version",
            "ld-version",
            "ld-major-version",
            "binutils-version",
            "rustc-version",
            "clang-version",
        }
    )
    _O = frozenset(
        {
            "cc-option",
            "cc-disable-warning",
            "cc-option-yn",
            "cc-option-align",
            "cc-ifversion",
            "cc-ldoption",
            "ld-option",
            "ld-ifversion",
            "as-option",
            "as-instr",
            "success",
            "failure",
            "if-success",
            "if-failure",
            "path-exists",
            "error-if",
        }
    )

    with open(fpath, "r", errors="replace") as f:
        lines = f.readlines()

    new_lines: list = []
    changed = False

    for line in lines:
        stripped = line.rstrip()
        lstripped = stripped.lstrip()

        if not lstripped or lstripped.startswith("#"):
            new_lines.append(line)
            continue

        m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s*(:?=)\s*(.*)$", stripped)
        if m:
            name, op, rhs = m.group(1), m.group(2), m.group(3)
            if (
                name in _V
                or name.endswith("-version")
                or name.endswith("-major-version")
            ):
                new_lines.append("{} {} 0\n".format(name, op))
                changed = True
                continue
            if name in _O:
                new_lines.append("{} {} n\n".format(name, op))
                changed = True
                continue
            if "$(shell" in rhs:
                new_lines.append("{} {} n\n".format(name, op))
                changed = True
                continue

        if lstripped.startswith("$("):
            new_lines.append("# [firmsolo-patched] {}\n".format(stripped))
            changed = True
            continue

        new_lines.append(line)

    if changed:
        with open(fpath, "w") as f:
            f.writelines(new_lines)
    return changed


def def_and_set(
    kconf,
    image,
    kernel,
    ver_magicz,
    unknown,
    endianess,
    arch,
    modulez,
    resultdir,
    seen_opt,
    module_configs,
    guard_options,
    ds_options,
):
    print("Version Magic", ver_magicz)

    img_inst = Image(kconf, image, module_configs, arch)
    module_names = [x.split("/")[-1] for x in modulez]

    # ── Netfilter core ────────────────────────────────────────────────────────
    flag = any(mod in module_names for mod in core_nf_modules)
    for indx, mod in enumerate(core_nf_modules):
        img_inst.set_option(core_nf_options[indx], 1 if flag else 2)

    img_inst.set_upstream_modules(module_configs)

    for opt in seen_opt:
        if "CONFIG_" not in opt:
            continue
        img_inst.set_option(opt, 2)

    # ── Firmadyne / FirmSolo markers ──────────────────────────────────────────
    img_inst.set_option("CONFIG_FIRMADYNE", 2)
    img_inst.set_option("CONFIG_MODULE_SRCVERSION_ALL", 0)
    img_inst.set_option("CONFIG_LOCALVERSION_AUTO", 0)

    # ── MODULES – must be on or scripts/mod and modules_install fail ──────────
    img_inst.set_option("CONFIG_MODULES", 2)
    img_inst.set_option("CONFIG_MODULE_UNLOAD", 2)

    if kernel > "linux-3.10.0":
        img_inst.set_option("CONFIG_GPIOLIB", 0)

    img_inst.set_option("CONFIG_DEBUG_INFO", 2)
    img_inst.set_option("CONFIG_MTD", 2)
    img_inst.set_option("CONFIG_MTD_PARTITIONS", 2)
    img_inst.set_option("CONFIG_MTD_NAND", 2)
    img_inst.set_option("CONFIG_MTD_NAND_CORE", 2)
    img_inst.set_option("CONFIG_MTD_NAND_NANDSIM", 2)
    img_inst.set_option("CONFIG_SERIAL_8250_EXTENDED", 2)
    img_inst.set_option("CONFIG_SERIAL_8250_PCI", 2)
    img_inst.set_option("CONFIG_SERIAL_8250_CONSOLE", 2)
    img_inst.set_option("CONFIG_SERIAL_8250_MANY_PORTS", 2)
    img_inst.set_option("CONFIG_SERIAL_8250_RSA", 2)
    img_inst.set_option("CONFIG_BLK_DEV_INITRD", 2)
    img_inst.set_option("CONFIG_BLK_DEV_RAM", 2)
    img_inst.set_option("CONFIG_PROC_FS", 2)
    img_inst.set_option("CONFIG_SYSFS", 2)
    img_inst.set_option("CONFIG_SHMEM", 2)
    img_inst.set_option("CONFIG_TMPFS", 2)
    img_inst.set_option("CONFIG_ZONE_DMA", 0)
    img_inst.set_option("CONFIG_SOUND", 0)
    img_inst.set_option("CONFIG_UEVENT_HELPER_PATH", "")
    img_inst.set_option("CONFIG_CMDLINE", "")
    img_inst.set_option("CONFIG_CMDLINE_OVERRIDE", 0)
    img_inst.set_option("CONFIG_EMBEDDED", 2)

    if arch == "arm":
        try:
            if img_inst.kconf.syms["NEW_LEDS"].tri_value == 2:
                img_inst.set_option("CONFIG_LEDS_CLASS", 2, True)
        except Exception:
            pass

    # ── Guard expressions ─────────────────────────────────────────────────────
    for guard in guard_options:
        img_inst.kconf._tokens = img_inst.kconf._tokenize(
            "if " + guard.replace("CONFIG_", "")
        )
        img_inst.kconf._line = guard.replace("CONFIG_", "")
        img_inst.kconf._tokens_i = 1
        expression = img_inst.kconf._expect_expr_and_eol()
        img_inst._split_expr_info(expression, expression)

    # ── DS options ────────────────────────────────────────────────────────────
    for opt in ds_options:
        img_inst.kconf._tokens = img_inst.kconf._tokenize(
            "if " + opt.replace("CONFIG_", "")
        )
        img_inst.kconf._line = opt.replace("CONFIG_", "")
        img_inst.kconf._tokens_i = 1
        expression = img_inst.kconf._expect_expr_and_eol()
        img_inst._split_expr_info(expression, expression)

    img_inst.set_option("CONFIG_VGA_CONSOLE", 0)
    img_inst.set_option("CONFIG_VIDEO_IVTV", 0)
    img_inst.set_option("IDE", 2, "override")
    img_inst.set_option("CONFIG_BLK_DEV_IDEDISK", 2, True)
    img_inst.set_option("CONFIG_IDE_GENERIC", 2, True)
    img_inst.set_option("CONFIG_IDE_GD", 2, True)
    img_inst.set_option("BLK_DEV_PIIX", 2, True)
    img_inst.set_option("EXT2_FS", 2, True)
    img_inst.set_option("CONFIG_MODULE_FORCE_LOAD", 2, True)
    img_inst.set_option("CONFIG_EXT2_FS_POSIX_ACL", 0, True)
    img_inst.set_option("CONFIG_DEBUG_PREEMPT", 0, True)

    # ── Architecture-specific settings ───────────────────────────────────────
    # FIXED: gate vermagic and HZ behind arm/mips only.
    # For x86_64 the defconfig already sets appropriate platform values;
    # calling set_ver_magic([]) would incorrectly disable SMP and MODULE_UNLOAD.
    if arch in ("arm", "mips"):
        img_inst.set_ver_magic(ver_magicz)
        img_inst.set_option("CONFIG_HZ_250", 2)

    if arch == "arm":
        if "ARMv5" in ver_magicz:
            img_inst.set_option("CONFIG_SCSI_SYM53C8XX", 2, True)
            img_inst.set_option("CONFIG_SCSI_SYM53C8XX_2", 2, True)
        if (
            "CONFIG_ARM_UNWIND" not in seen_opt
            and "CONFIG_ARM_UNWIND" not in guard_options
            and kernel < "linux-3.10"
        ):
            img_inst.set_option("CONFIG_ARM_UNWIND", 0)
        img_inst.set_option("CONFIG_SCSI", 2, True)
        img_inst.set_option("CONFIG_BLK_DEV_SD", 2, True)
        img_inst.set_option("CONFIG_VT", 0)
        img_inst.set_option("CONFIG_VIDEO_DEV", 0)
        img_inst.set_option("CONFIG_MMC", 2, True)
        img_inst.set_option("CONFIG_MMC_BLOCK", 2, True)
        img_inst.set_option("CONFIG_MMC_ARMMCI", 2, True)
        img_inst.set_option("CONFIG_SMC91X", 0, True)
        img_inst.set_option("CONFIG_MMU", 2, True)

    if arch == "mips" and kernel < "linux-4.0":
        img_inst.set_option("CONFIG_CC_STACKPROTECTOR_REGULAR", 0)
        img_inst.set_option("CONFIG_CC_STACKPROTECTOR_NONE", 2)
        if endianess == "little endian":
            img_inst.set_option("CPU_LITTLE_ENDIAN", 2)
        else:
            img_inst.set_option("CPU_BIG_ENDIAN", 2)

    print("OPTIONS\n", seen_opt)
    print("GUARDS\n", guard_options)
    print("DS OPTIONS\n", ds_options)
    print("Module Config", img_inst.module_configs)

    try:
        img_inst.kconf.write_config()
    except Exception:
        print("Kconfig write failed")

    print("KERNEL is", kernel)


# ── Public interface ──────────────────────────────────────────────────────────


def update_config(
    image,
    kernel,
    kern_dir,
    resultdir,
    unknown,
    ver_magicz,
    endianess,
    arch,
    modulez,
    seen_opt,
    guard_options,
    module_configs,
    ds_options,
):
    import custom_utils as cu

    cwd = os.getcwd()
    os.chdir(kern_dir)

    cross = cu.get_toolchain(kernel, arch, endianess)
    _setup_kconfig_env(arch, cross, kern_dir)

    # fix_kconf.fix_configs has already patched the Kconfig tree before
    # this function is called.  We try loading directly; if a macro still
    # slips through (e.g. if fix_configs was bypassed) we fall back to
    # in-place preprocessing — which now correctly handles Kconfig.include.
    try:
        kconf = Kconfig("Kconfig", warn=False, warn_to_stderr=False)
    except KconfigError as exc:
        if "macro expanded to blank string" in str(exc) or "syntax error" in str(exc):
            print("[kcre] kconfiglib error: {}".format(exc))
            print("[kcre] Running in-place Kconfig preprocessing fallback...")
            _preprocess_kconfig_macros(kern_dir)
            kconf = Kconfig("Kconfig", warn=False, warn_to_stderr=False)
        else:
            raise

    kconf.load_config(filename=kern_dir + ".config")

    not_mod_options = [o for o in ds_options if o not in module_configs]

    def_and_set(
        kconf,
        image,
        kernel,
        ver_magicz,
        unknown,
        endianess,
        arch,
        modulez,
        resultdir,
        seen_opt,
        module_configs,
        guard_options,
        not_mod_options,
    )

    os.chdir(cwd)

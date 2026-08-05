#!/usr/bin/env python3
"""
firm_kern_comp.py — Kernel build orchestration and CONFIG symbol resolution.

Key stages in compile_kernel():
  1. Extract the kernel source tarball.
  2. Apply hot-fixes and patch Kconfig files (hot_fixes / fix_configs).
  3. Produce a minimal .config baseline with ``make tinyconfig``.
  4. Index the source tree with cscope (find_and_cscope).
  5. Resolve function symbols → Makefile CONFIG gates
     (resolve_symbols_to_configs).
  6. Apply those CONFIG options via kconfiglib (kcre.update_config).
  7. Compile the kernel.

CONFIG resolution avoids regex in the matching hot-path by parsing each
Makefile into a list of MakefileAssignment objects whose RHS is already
split into individual tokens.  Target lookup then uses plain ``in``
membership tests, which correctly handles directory targets such as
``core/`` without any word-boundary trickery.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time as tm
import traceback
import argparse as argp
from typing import Any, Dict, List, Optional, Set, Tuple

currentdir = os.path.dirname(os.path.realpath(__file__))
parentdir  = os.path.dirname(currentdir)
sys.path.append(parentdir)
sys.path.append(currentdir)

import custom_utils as cu
from kcre        import update_config
from fix_kconf   import fix_configs
from hot_fixes   import hot_fixes
from firmadyne_fix import apply_fdyne_hooks


# ── Architecture helpers ──────────────────────────────────────────────────────

_SRCARCH_MAP: Dict[str, str] = {
    "x86_64":  "x86",
    "i386":    "x86",
    "sparc64": "sparc",
    "sparc32": "sparc",
    "sh64":    "sh",
    "tilegx":  "tile",
    "tilepro": "tile",
}


def _srcarch(arch: str) -> str:
    """Map ARCH → SRCARCH (mirrors the kernel top-level Makefile)."""
    return _SRCARCH_MAP.get(arch, arch)


def _build_flags(kernel: str, arch: str, cross: str, extraver: str) -> List[str]:
    """Return the ARCH / LLVM / CROSS_COMPILE flags for this kernel."""
    if cu.use_llvm_for_kernel(kernel):
        return [f"ARCH={arch}", "LLVM=1", extraver]
    return [f"ARCH={arch}", f"CROSS_COMPILE={cross}", extraver]


def _setup_build_env(arch: str, cross: str, image_dir: str) -> None:
    """Populate env vars expected by Kconfig and the kernel build system."""
    srcarch  = _srcarch(arch)
    real_dir = os.path.realpath(image_dir.rstrip("/"))

    os.environ.update({
        "ARCH":        arch,
        "SRCARCH":     srcarch,
        "srctree":     real_dir,
        "abs_srctree": real_dir,
        "CC":          cu.get_cc_for_kconfig(arch, cross),
    })
    print(f"  build_env: CC={os.environ['CC']}  ARCH={arch}  SRCARCH={srcarch}")

    for cand in ("as", "x86_64-linux-gnu-as", "llvm-as"):
        found = shutil.which(cand)
        if found:
            os.environ.setdefault("AS", found)
            break

    if cu.is_llvm_available():
        os.environ.setdefault("LLVM", "1")
        lld = shutil.which("ld.lld")
        if lld:
            os.environ.setdefault("LD", lld)


# ── Makefile parsing ──────────────────────────────────────────────────────────
#
# Strategy: parse Makefiles into structured objects first, then query those
# objects using plain Python.  All regex lives here in the parsing layer;
# the matching layer (below) uses only ``in`` and attribute access.


@dataclasses.dataclass
class MakefileAssignment:
    """One parsed variable assignment from a kernel Makefile or Kbuild file.

    Examples::

        # Raw line                              variable              op  targets
        obj-$(CONFIG_NET)  += core/         →  'obj-$(CONFIG_NET)'  '+=' ['core/']
        net-y              += sock.o        →  'net-y'              '+=' ['sock.o']
        ipe-y              := lsm.o fs.o    →  'ipe-y'              ':=' ['lsm.o', 'fs.o']
    """
    variable: str        # Full LHS text
    operator: str        # ':=' or '+='
    targets:  List[str]  # Whitespace-split RHS tokens


# Four named, single-purpose patterns — compiled once, used in one place each.
_ASSIGNMENT_RE    = re.compile(r"^([\w$()/._-]+)\s*([:+]=)\s*(.*)")
_DIRECT_GATE_RE = re.compile(r"^[a-zA-Z0-9_-]*\$\((CONFIG_[A-Za-z0-9_]+)\)$")
_COMPOSITE_VAR_RE = re.compile(
    r"^([a-zA-Z0-9_][a-zA-Z0-9_-]*)-(?:y|objs|\$\(CONFIG_[A-Za-z0-9_]+\))$"
)
_GCC_CLONE_RE     = re.compile(r"\.(isra|constprop|part)\.\d+$")
C_IDENTIFIER_RE   = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _is_build_obj_var(variable: str) -> bool:
    """True for variables that control which objects/dirs get compiled.

    Keeps:  obj-$(CONFIG_X), obj-y, obj-m, net-y, safesetid-y, ipe-objs …
    Drops:  CFLAGS_foo.o, ccflags-y, EXTRA_CFLAGS, shell assignments …
    """
    return (
        variable.startswith("obj-")
        or variable.endswith("-y")
        or variable.endswith("-objs")
        or "-$(CONFIG_" in variable
    )


# ── Makefile querying (zero regex) ───────────────────────────────────────────

def _find_configs_via_cscope_text(image_dir: str, rel_file: str) -> Set[str]:
    """Queries cscope strictly for the file stem, restricted to ancestor paths."""
    configs: Set[str] = set()
    cscope_db = os.path.join(image_dir, "cscope.out")
    if not os.path.exists(cscope_db):
        return configs

    real_image = os.path.realpath(image_dir)
    target_dir = os.path.normpath(os.path.dirname(rel_file))
    file_stem = os.path.splitext(os.path.basename(rel_file))[0]

    valid_dirs = set()
    curr = target_dir
    while curr and curr != ".":
        valid_dirs.add(os.path.normpath(curr))
        curr = os.path.dirname(curr)
    valid_dirs.add(".")

    try:
        res = subprocess.run(
            ["cscope", "-d", "-f", cscope_db, "-L6", file_stem],
            cwd=image_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for line in res.stdout.splitlines():
            parts = line.split(maxsplit=3)
            if len(parts) < 4:
                continue

            raw_file, _, _, line_text = parts
            absp = raw_file if os.path.isabs(raw_file) else os.path.realpath(os.path.join(real_image, raw_file))
            rel_path = os.path.relpath(absp, real_image)

            if not rel_path.endswith(("Makefile", "Kbuild")):
                continue

            makefile_dir = os.path.normpath(os.path.dirname(rel_path))
            if makefile_dir in valid_dirs:
                for cfg in re.findall(r"CONFIG_[A-Za-z0-9_]+", line_text):
                    configs.add(cfg)

    except Exception:
        pass

    return configs

def _get_clean_lines(filepath: str) -> List[str]:
    """Read a Makefile, strip comments, and join backslash continuations."""
    if not os.path.exists(filepath):
        return []
    
    with open(filepath, "r", errors="replace") as fh:
        raw_text = fh.read()

    # Join backslash continuations
    raw_text = raw_text.replace('\\\n', ' ')
    
    clean_lines = []
    for line in raw_text.splitlines():
        line = line.split('#')[0].strip()
        if line:
            clean_lines.append(line)
            
    return clean_lines

def _extract_configs_from_string(text: str) -> Set[str]:
    """Isolate CONFIG_ symbols from variable assignments or targets."""
    configs = set()
    # Normalize Makefile syntax like $(CONFIG_FOO) or CONFIG_FOO=y
    clean_text = text.replace('$(', ' ').replace(')', ' ').replace('=', ' ')
    for token in clean_text.split():
        if 'CONFIG_' in token:
            start = token.find('CONFIG_')
            sub = token[start:]
            cfg_chars = []
            for char in sub:
                if char.isalnum() or char == '_':
                    cfg_chars.append(char)
                else:
                    break
            if cfg_chars:
                configs.add("".join(cfg_chars))
    return configs

def _parse_assignment(line: str) -> Tuple[str, List[str]]:
    """Split a Makefile assignment into (LHS_variable, [RHS_tokens])."""
    # Handle :=, +=, ?=, =
    for op in (':=', '+=', '?=', '='):
        if op in line:
            parts = line.split(op, 1)
            lhs = parts[0].strip()
            rhs_tokens = parts[1].strip().split()
            return lhs, rhs_tokens
    return "", []
################################################
# PARSING INSIDE FILE IFDEFS

def _find_c_source_configs(
    image_dir: str,
    rel_file: str,
    line_no: int,
    sym_name: str = "",
) -> Set[str]:
    configs: Set[str] = set()
    abs_src = os.path.join(image_dir, rel_file)
    if not os.path.exists(abs_src):
        return configs

    try:
        with open(abs_src, "r", errors="ignore") as fh:
            raw_text = fh.read().replace('\\\n', ' ')
            lines = raw_text.splitlines()
    except OSError:
        return configs

    # Locate actual function/symbol definition line if line_no is invalid or pointing to a call-site
    if sym_name:
        def_pattern = re.compile(rf"\b{re.escape(sym_name)}\b\s*\(")
        for idx, text in enumerate(lines, 1):
            if def_pattern.search(text):
                line_no = idx
                break

    if line_no <= 0:
        return configs

    if_stack: List[Set[str]] = []

    for raw in lines[:line_no]:
        s = raw.strip()
        if not s.startswith("#"):
            continue
        tokens = s[1:].split()
        if not tokens:
            continue
        directive = tokens[0]

        if directive in ("ifdef", "if"):
            if_stack.append(set(re.findall(r"CONFIG_[A-Za-z0-9_]+", s)))
        elif directive == "ifndef":
            if_stack.append(set())
        elif directive == "elif":
            if if_stack:
                if_stack.pop()
            if_stack.append(set(re.findall(r"CONFIG_[A-Za-z0-9_]+", s)))
        elif directive == "else":
            if if_stack:
                if_stack.pop()
            if_stack.append(set())
        elif directive == "endif":
            if if_stack:
                if_stack.pop()

    for frame in if_stack:
        configs.update(frame)

    return configs

def _find_makefile_configs(image_dir: str, rel_file: str) -> set[str]:
    """Traverses local & parent Makefiles with composite object mapping and conditional stacking."""
    configs: set[str] = set()
    rel_dir = os.path.dirname(rel_file)
    stem = os.path.splitext(os.path.basename(rel_file))[0]
    target_obj = f"{stem}.o"

    local_mk = os.path.join(image_dir, rel_dir, "Makefile")
    if not os.path.exists(local_mk):
        local_mk = os.path.join(image_dir, rel_dir, "Kbuild")

    if os.path.exists(local_mk):
        try:
            with open(local_mk, "r", errors="ignore") as f:
                content = re.sub(r'\\\s*\n', ' ', f.read().replace('\r\n', '\n'))

            # Pass 1: Identify composite parent objects (e.g., md.o -> md-mod.o, sd.o -> sd_mod.o)
            targets_to_check = {target_obj, stem}
            added = True
            while added:
                added = False
                for line in content.splitlines():
                    line_clean = line.split('#')[0].strip()
                    if '=' not in line_clean:
                        continue
                    op_pos = min(line_clean.find(op) for op in (':=', '+=', '?=', '=') if op in line_clean)
                    lhs = line_clean[:op_pos].strip()

                    # Ignore obj- targets during composite parent mapping
                    if lhs.startswith("obj-"):
                        continue

                    rhs_tokens = {
                        t.strip('"\',()')
                        for t in line_clean[op_pos:].strip().split()
                        if t not in (':=', '+=', '?=', '=')
                    }

                    if any(t in rhs_tokens for t in targets_to_check) and '-' in lhs:
                        # FIX: rsplit at the LAST dash so "md-mod-y" -> "md-mod", "y"
                        p_stem, suffix = lhs.rsplit('-', 1)
                        p_stem = p_stem.strip()
                        suffix = suffix.strip()

                        if (
                            suffix in ('y', 'objs', 'm')
                            or suffix.startswith('y')
                            or suffix.startswith('m')
                            or suffix.startswith('$(')
                        ):
                            parent_obj = f"{p_stem}.o"
                            if parent_obj not in targets_to_check:
                                targets_to_check.add(parent_obj)
                                targets_to_check.add(p_stem)
                                added = True

            # Pass 2: Extract CONFIG_ gates for target or composite parent
            mk_if_stack = []
            for line in content.splitlines():
                line_clean = line.split('#')[0].strip()
                if not line_clean:
                    continue

                if line_clean.startswith(('ifdef', 'ifeq', 'ifndef', 'ifneq')):
                    mk_if_stack.append(set(re.findall(r'CONFIG_[A-Za-z0-9_]+', line_clean)))
                    continue
                elif line_clean.startswith('endif'):
                    if mk_if_stack:
                        mk_if_stack.pop()
                    continue

                if '=' not in line_clean:
                    continue

                op_pos = min(line_clean.find(op) for op in (':=', '+=', '?=', '=') if op in line_clean)
                lhs = line_clean[:op_pos].strip()
                rhs_tokens = {
                    t.strip('"\',()')
                    for t in line_clean[op_pos:].strip().split()
                    if t not in (':=', '+=', '?=', '=')
                }

                if any(t in rhs_tokens for t in targets_to_check):
                    configs.update(re.findall(r'CONFIG_[A-Za-z0-9_]+', lhs))
                    for block_set in mk_if_stack:
                        configs.update(block_set)
        except Exception:
            pass

    # Parent Directory Climber (Resolves drivers/Makefile -> obj-$(CONFIG_MD) += md/)
    curr_dir = rel_dir
    while curr_dir and curr_dir != ".":
        parent_dir = os.path.dirname(curr_dir)
        folder_name = os.path.basename(curr_dir)
        folder_tokens = {f"{folder_name}/", folder_name, f"{folder_name}.o"}

        parent_mk = os.path.join(image_dir, parent_dir, "Makefile")
        if not os.path.exists(parent_mk):
            parent_mk = os.path.join(image_dir, parent_dir, "Kbuild")

        if os.path.exists(parent_mk):
            try:
                with open(parent_mk, "r", errors="ignore") as f:
                    p_content = re.sub(r'\\\s*\n', ' ', f.read().replace('\r\n', '\n'))

                for line in p_content.splitlines():
                    line_clean = line.split('#')[0].strip()
                    if '=' in line_clean:
                        op_pos = min(line_clean.find(op) for op in (':=', '+=', '?=', '=') if op in line_clean)
                        lhs = line_clean[:op_pos].strip()
                        rhs_tokens = {
                            t.strip('"\',()')
                            for t in line_clean[op_pos:].strip().split()
                            if t not in (':=', '+=', '?=', '=')
                        }
                        if any(ft in rhs_tokens for ft in folder_tokens):
                            configs.update(re.findall(r'CONFIG_[A-Za-z0-9_]+', lhs))
            except Exception:
                pass

        curr_dir = parent_dir

    return configs
def resolve_file_requirements(
    image_dir: str,
    rel_file: str,
    line_no: int = 0,
    sym_name: str = "",          # ← new: forwarded to _find_c_source_configs
) -> Set[str]:
    """Merges C preprocessor requirements (#ifdef) with Makefile build requirements."""
    c_configs  = _find_c_source_configs(image_dir, rel_file, line_no, sym_name)
    mk_configs = _find_makefile_configs(image_dir, rel_file)
    return c_configs | mk_configs


################################################

def _find_configs_for_file(image_dir: str, rel_file: str) -> Set[str]:
    """
    Deterministically resolves Kbuild configs by matching RHS target assignments
    and tracking enclosing Makefile conditional blocks (ifdef/ifeq).
    """
    configs: Set[str] = set()
    rel_dir = os.path.dirname(rel_file)
    stem = os.path.splitext(os.path.basename(rel_file))[0]
    target_obj = f"{stem}.o"

    # -------------------------------------------------------------
    # 1. Local Makefile Parsing with Conditional Block Stacking
    # -------------------------------------------------------------
    local_mk = os.path.join(image_dir, rel_dir, "Makefile")
    if not os.path.exists(local_mk):
        local_mk = os.path.join(image_dir, rel_dir, "Kbuild")

    if os.path.exists(local_mk):
        try:
            with open(local_mk, "r", errors="ignore") as f:
                content = f.read()

            content = content.replace('\r\n', '\n')
            content = re.sub(r'\\\s*\n', ' ', content)  # Join multiline continuations

            active_if_stack = []  # Stack to track nested ifdef/ifeq configs

            for line in content.splitlines():
                line_clean = line.split('#')[0].strip()
                if not line_clean:
                    continue

                # Handle conditional block directives
                if line_clean.startswith(('ifdef', 'ifeq', 'ifndef', 'ifneq')):
                    block_cfgs = set(re.findall(r'CONFIG_[A-Za-z0-9_]+', line_clean))
                    active_if_stack.append(block_cfgs)
                    continue
                elif line_clean.startswith('endif'):
                    if active_if_stack:
                        active_if_stack.pop()
                    continue

                if '=' not in line_clean:
                    continue

                # Separate LHS variable from RHS tokens
                op_pos = min(line_clean.find(op) for op in (':=', '+=', '?=', '=') if op in line_clean)
                lhs = line_clean[:op_pos].strip()
                rhs_tokens = set(line_clean[op_pos:].strip().split())

                # Target file match on RHS
                if target_obj in rhs_tokens or stem in rhs_tokens:
                    # 1. Extract config from the line itself (e.g. CONFIG_SYSFS)
                    configs.update(re.findall(r'CONFIG_[A-Za-z0-9_]+', lhs))
                    # 2. Extract configs from enclosing conditional blocks (e.g. CONFIG_PCI)
                    for block_set in active_if_stack:
                        configs.update(block_set)

                # Composite Object Tracing (e.g. proc-y := base.o generic.o)
                if '-' in lhs:
                    parts = lhs.split('-', 1)
                    parent_stem, suffix = parts[0].strip(), parts[1].strip()
                    if suffix in ('y', 'objs', 'm') and (target_obj in rhs_tokens or stem in rhs_tokens):
                        parent_obj = f"{parent_stem}.o"
                        for block_set in active_if_stack:
                            configs.update(block_set)

                        # Resolve parent object assignment
                        for subline in content.splitlines():
                            sub_clean = subline.split('#')[0].strip()
                            if '=' in sub_clean:
                                sub_op = min(sub_clean.find(op) for op in (':=', '+=', '?=', '=') if op in sub_clean)
                                sub_lhs = sub_clean[:sub_op].strip()
                                sub_rhs = set(sub_clean[sub_op:].strip().split())
                                if parent_obj in sub_rhs:
                                    configs.update(re.findall(r'CONFIG_[A-Za-z0-9_]+', sub_lhs))

        except Exception:
            pass

    # -------------------------------------------------------------
    # 2. Parent Directory Climber (Hierarchy Folder Gates)
    # -------------------------------------------------------------
    curr_dir = rel_dir
    while curr_dir and curr_dir != ".":
        parent_dir = os.path.dirname(curr_dir)
        folder_name = os.path.basename(curr_dir)
        folder_tokens = {f"{folder_name}/", folder_name, f"{folder_name}.o"}

        parent_mk = os.path.join(image_dir, parent_dir, "Makefile")
        if not os.path.exists(parent_mk):
            parent_mk = os.path.join(image_dir, parent_dir, "Kbuild")

        if os.path.exists(parent_mk):
            try:
                with open(parent_mk, "r", errors="ignore") as f:
                    p_content = f.read()

                p_content = p_content.replace('\r\n', '\n')
                p_content = re.sub(r'\\\s*\n', ' ', p_content)

                p_active_stack = []

                for line in p_content.splitlines():
                    line_clean = line.split('#')[0].strip()
                    if not line_clean:
                        continue

                    if line_clean.startswith(('ifdef', 'ifeq', 'ifndef', 'ifneq')):
                        p_active_stack.append(set(re.findall(r'CONFIG_[A-Za-z0-9_]+', line_clean)))
                        continue
                    elif line_clean.startswith('endif'):
                        if p_active_stack:
                            p_active_stack.pop()
                        continue

                    if '=' not in line_clean:
                        continue

                    op_pos = min(line_clean.find(op) for op in (':=', '+=', '?=', '=') if op in line_clean)
                    lhs = line_clean[:op_pos].strip()
                    rhs_tokens = set(line_clean[op_pos:].strip().split())

                    if any(ft in rhs_tokens for ft in folder_tokens):
                        configs.update(re.findall(r'CONFIG_[A-Za-z0-9_]+', lhs))
                        for block_set in p_active_stack:
                            configs.update(block_set)
            except Exception:
                pass

        curr_dir = parent_dir

    return configs

# ── cscope helpers ────────────────────────────────────────────────────────────


def _cscope_find_definition_files(
    image_dir: str,
    cscope_db: str,
    sym:       str,
    file_hint: Optional[str],
    line_hint: Optional[int],
) -> Set[str]:
    """Return relative paths of source files where cscope finds *sym* defined.

    Tries ``-L1`` (global definitions) first; falls back to ``-L0``
    (all references) if nothing is found.

    *file_hint* and *line_hint* narrow the results — but line filtering is
    only applied on this slow/bare-symbol path.  Callers that already know
    the file (fast path) skip this function entirely and avoid the mismatch
    that would arise from comparing a call-site line against a definition line.
    """
    clean_sym  = _GCC_CLONE_RE.sub("", sym).strip()
    real_image = os.path.realpath(image_dir)
    found: Set[str] = set()

    for flag in ("-L1", "-L0"):
        try:
            res = subprocess.run(
                ["cscope", "-d", "-f", cscope_db, flag, clean_sym],
                cwd=image_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            print(f"  [cscope] Cannot run cscope: {exc}")
            return found

        for line in res.stdout.splitlines():
            parts = line.split(maxsplit=3)
            if len(parts) < 3:
                continue

            raw  = parts[0]
            absp = raw if os.path.isabs(raw) else os.path.realpath(os.path.join(real_image, raw))
            rel  = os.path.relpath(absp, real_image)

            if not rel.endswith((".c", ".h", ".S")):
                continue
            if file_hint and not os.path.normpath(rel).endswith(os.path.normpath(file_hint)):
                continue
            if line_hint is not None:
                try:
                    if abs(int(parts[2]) - line_hint) > 50:
                        continue
                except ValueError:
                    pass

            found.add(rel)

        if found:
            break

    return found


# ── Symbol spec normalisation ─────────────────────────────────────────────────

SymbolSpec = Any   # str | tuple | list | dict


def parse_symbol_spec(spec: SymbolSpec) -> Tuple[str, Optional[str], Optional[int]]:
    """Normalise *spec* to ``(symbol_name, file_path, line_number)``.

    Accepted forms
    --------------
    ``"func_name"``
        Plain symbol string — file and line will be ``None``.
    ``("func_name", "net/core/sock.c")``
        Symbol + source-file hint.
    ``("func_name", "net/core/sock.c", 174)``
        Symbol + source-file + approximate line (e.g. from cscope -L3 output).
    ``{"symbol": …, "file": …, "line": …}``
        Dictionary form; any key may be absent.
    """
    if isinstance(spec, dict):
        return (
            str(spec.get("symbol", "")),
            spec.get("file") or None,
            spec.get("line") or None,
        )
    if isinstance(spec, (tuple, list)):
        sym    = str(spec[0]) if spec else ""
        file_p = str(spec[1]) if len(spec) > 1 and spec[1] else None
        line_n = int(spec[2]) if len(spec) > 2 and spec[2] is not None else None
        return sym, file_p, line_n
    # Plain string — the whole value is the symbol name
    return str(spec).strip(), None, None


# ── CONFIG resolution ─────────────────────────────────────────────────────────


def resolve_symbols_to_configs(
    image_dir: str,
    symbols: List[Any],
    exclude_dirs: List[str],
) -> List[str]:
    found_configs: Set[str] = set()
    cscope_db  = os.path.join(image_dir, "cscope.out")
    real_image = os.path.realpath(image_dir)

    if not symbols:
        return []

    # Normalize exclude_dirs with trailing slashes
    norm_exclude = []
    if exclude_dirs:
        for d in exclude_dirs:
            d_clean = d.strip()
            if d_clean and not d_clean.endswith(os.sep):
                d_clean += os.sep
            norm_exclude.append(d_clean)

    for item in symbols:
        if not item:
            continue

        if isinstance(item, (tuple, list)):
            sym_name      = item[0]
            provided_file = item[1] if len(item) > 1 else None
            provided_line = item[2] if len(item) > 2 else 0
        else:
            sym_name      = item
            provided_file = None
            provided_line = 0

        clean_sym = re.sub(r"\.(isra|constprop|part)\.\d+", "", str(sym_name)).strip()
        defined_targets: Set[Tuple[str, int]] = set()

        if provided_file:
            absp = os.path.realpath(provided_file if os.path.isabs(provided_file) else os.path.join(real_image, provided_file))
            defined_targets.add((os.path.relpath(absp, real_image), provided_line))

        elif os.path.exists(cscope_db):
            raw_cscope_hits: Set[Tuple[str, int]] = set()

            for flag in ("-L1", "-L0"):
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
                        if len(parts) < 3:
                            continue
                        raw_path, line_str = parts[0], parts[2]

                        absp = os.path.realpath(raw_path if os.path.isabs(raw_path) else os.path.join(real_image, raw_path))
                        rel_file = os.path.relpath(absp, real_image)

                        if rel_file.endswith((".c", ".h", ".S")):
                            try:
                                line_no = int(line_str)
                            except ValueError:
                                line_no = 0
                            raw_cscope_hits.add((rel_file, line_no))

                    # Fix: Only stop cscope loop if we hit actual .c or .S implementation files
                    if any(f.endswith((".c", ".S")) for f, _ in raw_cscope_hits):
                        break

                except Exception as exc:
                    print(f"  [cscope] Query error for '{clean_sym}': {exc}")

            # Prioritize implementation source files
            c_s_targets = {(f, l) for f, l in raw_cscope_hits if f.endswith((".c", ".S"))}
            if c_s_targets:
                defined_targets.update(c_s_targets)
            else:
                # Header fallback: map stem.h -> stem.c in source tree
                for h_file, _ in {(f, l) for f, l in raw_cscope_hits if f.endswith(".h")}:
                    stem = os.path.splitext(os.path.basename(h_file))[0]
                    for root, _, files in os.walk(real_image):
                        if f"{stem}.c" in files:
                            matched_abs = os.path.join(root, f"{stem}.c")
                            defined_targets.add((os.path.relpath(matched_abs, real_image), 0))

        # Resolve Makefile & CPP requirements
        for rel_file, line_no in defined_targets:
            if norm_exclude and any(rel_file.startswith(ex) for ex in norm_exclude):
                continue
            reqs = resolve_file_requirements(image_dir, rel_file, line_no, clean_sym)
            found_configs.update(reqs)

    return sorted(found_configs)

def find_caller_configs(image_dir: str, target_symbol: str) -> List[str]:
    """Return CONFIG options needed to compile every caller of *target_symbol*.

    Tries cscope ``-L3`` (functions that call the symbol) first; falls back
    to ``-L0`` (all references) when no direct callers are found — which
    happens when the symbol is only used through a macro wrapper or is
    defined in a header as an inline function.
    """
    cscope_db = os.path.join(image_dir, "cscope.out")
    if not os.path.exists(cscope_db):
        print("  [find_caller_configs] cscope.out missing; run find_and_cscope first.")
        return []

    def _parse_cscope_output(stdout: str) -> List[Tuple[str, str, Optional[int]]]:
        real_image = os.path.realpath(image_dir)
        results    = []
        for line in stdout.strip().splitlines():
            parts = line.split(maxsplit=3)
            if len(parts) < 3:
                continue
            raw       = parts[0]
            func_name = parts[1].strip("`'\"")
            line_num  = int(parts[2]) if parts[2].isdigit() else None
            absp      = raw if os.path.isabs(raw) else os.path.realpath(
                os.path.join(real_image, raw)
            )
            rel = os.path.relpath(absp, real_image)
            if rel.endswith((".c", ".h", ".S")):
                results.append((func_name, rel, line_num))
        return results

    caller_specs: List[Tuple[str, str, Optional[int]]] = []

    for description, flag in (("callers (-L3)", "-L3"), ("references (-L0)", "-L0")):
        try:
            res = subprocess.run(
                ["cscope", "-d", flag, target_symbol],
                cwd=image_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            print(f"  [cscope] {exc}")
            return []

        if res.stderr.strip():
            print(f"  [cscope {description}]: {res.stderr.strip()}")

        caller_specs = _parse_cscope_output(res.stdout)
        if caller_specs:
            print(f"  {len(caller_specs)} {description} hits for '{target_symbol}'")
            break
        print(f"  0 {description} hits for '{target_symbol}'; trying next mode…")

    if not caller_specs:
        print(f"  [find_caller_configs] No call sites found for '{target_symbol}'")
        return []

    print(f"Found {len(caller_specs)} sites referencing '{target_symbol}'. Resolving configs…")
    for spec in caller_specs:
        print(spec)
    return resolve_symbols_to_configs(image_dir, caller_specs)


# ── Kernel source / build helpers ─────────────────────────────────────────────


def exported_syms(kern_dir: str) -> Tuple[List[str], List[str]]:
    symvers: List[str] = []
    sysmap:  List[str] = []
    with open(kern_dir + "System.map") as fh:
        for line in fh:
            sysmap.append(line.split()[2])
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
            print("The kernel is not yet extracted — can't remove it")


def create_directories(
    kernel:      str,
    resultdir:   str,
    new_kern_dir: str,
    kern_dir:    str,
    tar_dir:     str,
    tarf:        str,
    ds_recovery: int,
    s_config:    str,
) -> None:
    try:
        os.mkdir(resultdir)
    except FileExistsError:
        print(f"Directory {resultdir} already exists")

    if not ds_recovery and s_config == "yes":
        try:
            os.system("rm -rf " + new_kern_dir)
            os.mkdir(new_kern_dir)
        except Exception:
            print(f"Directory {new_kern_dir} already exists")

    print(kernel)
    remove_kernel_dir(ds_recovery, kern_dir + kernel)

    if not ds_recovery:
        try:
            print("Opening tar file", tarf)
            untar = tarfile.open(tarf)
        except Exception as exc:
            print(f"Kernel {tarf} does not exist: {exc}")
            return
        try:
            print("Untarring to", kern_dir)
            untar.extractall(kern_dir)
            untar.close()
        except Exception:
            print(f"Kernel {tarf} failed to extract")


def make_tinyconfig(
    cross:     str,
    arch:      str,
    image_dir: str,
    kernel:    str,
    logfile:   str,
    errfile:   str,
) -> None:
    cwd = os.getcwd()
    os.chdir(image_dir)
    flags  = _build_flags(kernel, arch, cross, "")
    target = "tinyconfig" if kernel >= "linux-3.18" else "allnoconfig"
    try:
        result = subprocess.run(
            ["make"] + flags + [target],
            cwd=image_dir,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        print(f"Minimal config complete ({target})")
    except Exception:
        print(f"Error generating {target} for {kernel}")
        os.chdir(cwd)
        return

    with open(logfile, "w") as fh:
        fh.write(f"Tinyconfig log:\n{result.stdout.decode('utf-8', errors='replace')}\n")
    with open(errfile, "w") as fh:
        fh.write(f"Tinyconfig errors:\n{result.stderr.decode('utf-8', errors='replace')}\n")
    os.chdir(cwd)


def do_compile(
    cross:             str,
    arch:              str,
    image_dir:         str,
    extraversion:      str,
    logfile:           str,
    errfile:           str,
    kernel:            str,
    time:              str,
    ds_recovery:       int,
    single_module_dir: str,
    new_kern_dir:      str,
) -> None:
    cwd = os.getcwd()
    os.chdir(image_dir)

    vers    = kernel.split(".")
    extraver = (
        "EXTRAVERSION=." + vers[-1] + extraversion
        if len(vers) > 3 else
        "EXTRAVERSION=" + extraversion
    )
    base_flags = _build_flags(kernel, arch, cross, extraver)
    jobs       = f"-j{os.cpu_count() or 1}"

    if not ds_recovery:
        try:
            comp = subprocess.run(
                ["make"] + base_flags + [jobs],
                cwd=image_dir,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            if comp.returncode != 0:
                print("❌ Kernel build failed:")
                print(comp.stderr[-3000:].decode("utf-8", errors="replace"))
                raise RuntimeError("Kernel build failed")
            print("Kernel compilation done")
        except Exception as exc:
            print(f"Error compiling {kernel}: {exc}")

        prep = "prepare scripts" if kernel >= "linux-2.6.23" else "scripts"
        mod  = "M=scripts/mod"   if kernel >= "linux-3.0.0"  else "SUBDIRS=scripts/mod"
        for step in (prep, mod):
            try:
                subprocess.check_output(
                    f"make {' '.join(base_flags)} {step}", shell=True
                )
            except Exception:
                print(f"  make {step} failed in {image_dir}")

        mod_install = "INSTALL_MOD_PATH=" + new_kern_dir
        try:
            modz = subprocess.run(
                ["make"] + base_flags + [mod_install, "modules_install", "-j8"],
                cwd=image_dir,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            print("Module install done")
        except Exception as exc:
            print(f"Module install failed: {exc}")

        with open(logfile, "a") as fh:
            try:
                fh.write(f"Compilation{time} log:\n"
                         f"{comp.stdout.decode('utf-8', errors='replace')}\n"
                         f"Module install{time} log:\n"
                         f"{modz.stdout.decode('utf-8', errors='replace')}\n")
            except Exception:
                print("Error writing compilation logs")
        with open(errfile, "a") as fh:
            try:
                fh.write(f"Compilation{time} errors:\n"
                         f"{comp.stderr.decode('utf-8', errors='replace')}\n"
                         f"Module install{time} errors:\n"
                         f"{modz.stderr.decode('utf-8', errors='replace')}\n")
            except Exception:
                print("Error writing compilation error files")

    else:
        print(f"DS recovery: building module {single_module_dir}")
        try:
            out = subprocess.check_output(
                f'yes "" | make {" ".join(base_flags)} oldconfig', shell=True
            )
            print(out.decode("utf-8", errors="replace"))
        except Exception:
            print(f"make oldconfig failed in {image_dir}")

        prep = "prepare scripts" if kernel >= "linux-2.6.23" else "scripts"
        mod  = "M=scripts/mod"   if kernel >= "linux-3.0.0"  else "SUBDIRS=scripts/mod"
        for step in (prep, mod):
            try:
                subprocess.check_output(
                    f"make {' '.join(base_flags)} {step}", shell=True
                )
            except Exception:
                print(f"  make {step} failed in {image_dir}")

        mod_switch = "M" if kernel >= "linux-3.0.0" else "SUBDIRS"
        t0 = tm.time()
        try:
            subprocess.check_output(
                f"make {' '.join(base_flags)} -C {image_dir}"
                f" {mod_switch}={single_module_dir} modules",
                shell=True,
            )
        except Exception:
            print(f"make module failed in {image_dir}")
        print(f"Module compile time: {tm.time() - t0:.1f}s")

    os.chdir(cwd)


def find_and_cscope(image_dir: str, arch: str) -> None:
    """Index the kernel source tree with cscope; reuses an existing database."""
    cscope_db = os.path.join(image_dir, "cscope.out")
    if os.path.exists(cscope_db):
        print("  find_and_cscope: reusing existing database.")
        return

    cwd = os.getcwd()
    sa  = _srcarch(arch)
    try:
        os.chdir(image_dir)

        for db_file in ("cscope.out", "cscope.in.out", "cscope.po.out", "cscope.files"):
            try:
                os.remove(db_file)
            except FileNotFoundError:
                pass

        # Temporarily swap in the arch Kconfig as the root Kconfig
        kconfig_real   = "Kconfig"
        kconfig_backup = "Kconfig.bak"
        if os.path.exists(kconfig_real):
            shutil.copy2(kconfig_real, kconfig_backup)
        arch_kconfig = f"arch/{sa}/Kconfig"
        if os.path.exists(arch_kconfig):
            shutil.copy2(arch_kconfig, kconfig_real)

        print(f"  find_and_cscope: indexing via tags.sh (ARCH={sa})…")
        subprocess.run(
            ["./scripts/tags.sh", "cscope"],
            cwd=image_dir,
            env={**os.environ, "ARCH": sa},
            check=False,
            stderr=subprocess.DEVNULL,
        )

        if os.path.exists("cscope.files"):
            # Append Makefile and Kconfig paths (needed for CONFIG resolution)
            with open("cscope.files", "a") as fh:
                for root, _, files in os.walk("."):
                    for fname in files:
                        if fname.startswith(("Makefile", "Kconfig")):
                            fh.write(
                                os.path.relpath(os.path.join(root, fname), ".") + "\n"
                            )

            subprocess.run(
                ["cscope", "-b", "-q", "-k", "-i", "cscope.files"],
                cwd=image_dir,
                check=True,
                stderr=subprocess.DEVNULL,
            )

        print("  find_and_cscope: index built successfully.")

        if os.path.exists(kconfig_backup):
            shutil.move(kconfig_backup, kconfig_real)

    except Exception as exc:
        print(f"cscope indexing failed: {exc}")
    finally:
        os.chdir(cwd)


def copy_files(
    image_dir:   str,
    new_kern_dir: str,
    s_config:    str,
    arch:        str = "arm",
) -> None:
    print(f"Copying build artefacts: {image_dir} → {new_kern_dir}")
    os.system(f"cp {image_dir}vmlinux {new_kern_dir}")
    if s_config == "yes":
        for fname in (".config", "Module.symvers", "System.map", "cscope.files"):
            os.system(f"cp {image_dir}{fname} {new_kern_dir}")
    boot_images = {
        "arm":   "arch/arm/boot/zImage",
        "arm64": "arch/arm64/boot/Image",
        "x86":   "arch/x86/boot/bzImage",
        "mips":  "arch/mips/boot/vmlinux.bin",
    }
    img = boot_images.get(_srcarch(arch))
    if img:
        os.system(f"cp {image_dir}{img} {new_kern_dir}")


def save_sym_data(
    image:       str,
    image_dir:   str,
    outfile:     str,
    symbolz:     List[SymbolSpec],
    time:        str,
    kernel:      str,
    kern_dir:    str,
    new_kern_dir: str,
    ds_recovery: int,
) -> List[str]:
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

        mode  = "w"                  if time == "1" else "a"
        label = "Undefined Symbols"  if time == "1" else "Final Undefined Symbols"

        if not ds_recovery:
            with open(outfile, mode) as fh:
                fh.write(f"{len(unknown)} {label}:\n")
                for sym in unknown:
                    fh.write(sym + "\n")
                fh.write("\n")
    except Exception:
        print("Kernel did not compile; System.map unavailable")
        sys.exit(1)
    return unknown


def apply_patch(image_dir: str, patch: str) -> None:
    print(f"Applying patch {patch}")
    cwd = os.getcwd()
    os.chdir(image_dir)
    try:
        subprocess.run(
            f"cat {patch} | patch -p1 -E -d .",
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
    patches  = os.listdir(cu.openwrt_patch_dir)
    tokens   = kernel.split(".")
    # Most-specific match first
    prefixes = [kernel, ".".join(tokens[:3]), ".".join(tokens[:2])]
    for prefix in prefixes:
        for patch in sorted(patches):
            if prefix in patch:
                apply_patch(image_dir, cu.openwrt_patch_dir + patch)
                return


# ── Main orchestration ────────────────────────────────────────────────────────


def compile_kernel(
    image:             str,
    ds_options:        List[str],
    ds_recovery:       int,
    single_module_dir: str,
    s_config:          str,
    openwrt:           bool,
    kernel:            str,
    extraversion:      str,
    modulez:           List[str],
    ver_magicz:        List[str],
    symbolz:           List[SymbolSpec],
    arch:              str,
    endianess:         str,
    cross:             str,
    conf_opts:         List[str],
    guard_expr:        List[str],
    module_options:    List[str],
    exclude_dirs:      List[str],
) -> int:
    kernel       = cu.kernel_prefix + kernel
    resultdir    = cu.result_dir_path + image + "/"
    new_kern_dir = resultdir + kernel + "/"
    tarf         = cu.tar_dir + kernel + ".tar.gz"
    image_dir    = cu.kern_dir + kernel + "/"
    cross        = cu.get_toolchain(kernel, arch, endianess)

    create_directories(
        kernel, resultdir, new_kern_dir, cu.kern_dir,
        cu.tar_dir, tarf, ds_recovery, s_config,
    )

    if openwrt:
        patch_kernel(image_dir, kernel)

    print(f"image_dir = {image_dir}")
    outfile = resultdir + "results.out"
    logfile = resultdir + "logs.out"
    errfile = resultdir + "errors.out"

    if not ds_recovery:
        print("Running FirmSolo in normal mode")
        _setup_build_env(arch, cross, image_dir)
        hot_fixes(image_dir, kernel)
        fix_configs(image_dir, kernel)

        make_tinyconfig(cross, arch, image_dir, kernel, logfile, errfile)

        unknown = save_sym_data(
            image, image_dir, outfile, symbolz, "1",
            kernel, cu.kern_dir, new_kern_dir, ds_recovery,
        )

        find_and_cscope(image_dir, arch)

        resolved = resolve_symbols_to_configs(image_dir, symbolz, exclude_dirs)
        for cfg in resolved:
            if cfg not in ds_options:
                ds_options.append(cfg)

        try:
            update_config(
                image, kernel, image_dir, resultdir, unknown, ver_magicz,
                endianess, arch, modulez, conf_opts, guard_expr,
                module_options, ds_options,
            )
            print("DEBUG ds_options from symbolz translation:", ds_options)
        except Exception:
            print(traceback.format_exc())

    print(f"Compiling kernel for image {image}")
    do_compile(
        cross, arch, image_dir, extraversion, logfile, errfile,
        kernel, "2", ds_recovery, single_module_dir, new_kern_dir,
    )

    if not ds_recovery:
        copy_files(image_dir, new_kern_dir, s_config, arch)
        save_sym_data(
            image, image_dir, outfile, symbolz, "2",
            kernel, cu.kern_dir, new_kern_dir, ds_recovery,
        )

    return 0


def modify_the_vermagic(
    vermagic:   List[str],
    ds_options: List[str],
) -> Tuple[List[str], List[str]]:
    """Translate CONFIG_SMP / CONFIG_MODULE_UNLOAD options into vermagic tokens."""
    _MAP = {
        "CONFIG_SMP":            ("SMP",        True),
        "CONFIG_MODULE_UNLOAD":  ("mod_unload", True),
        "!CONFIG_SMP":           ("SMP",        False),
        "!CONFIG_MODULE_UNLOAD": ("mod_unload", False),
    }
    for option in list(ds_options):
        if option in _MAP:
            token, add = _MAP[option]
            if add and token not in vermagic:
                vermagic.append(token)
            elif not add and token in vermagic:
                vermagic.remove(token)
            ds_options.remove(option)
    return vermagic, ds_options


def run_the_compilation(
    image:             str,
    ds_opt_fl:         Optional[str],
    ds_opt_list:       List[str],
    ds_recovery:       int,
    s_mod_dir:         str,
    s_config:          str,
    override_vermagic: bool,
    openwrt:           bool,
    firmadyne:         bool,
) -> None:
    ds_recovery = max(0, min(1, ds_recovery))

    ds_options: List[str] = []
    if ds_opt_fl is not None:
        ds_options = cu.read_file(ds_opt_fl)
    elif ds_opt_list:
        ds_options = list(ds_opt_list)

    which_info = [
        "kernel", "extraversion", "modules", "vermagic",
        "symbols", "arch", "endian", "cross",
        "options", "guards", "module_options",
    ]
    info = cu.get_image_info(image, which_info)

    try:
        ds_options += cu.get_image_info(image, ["dslc"])[0] or []
    except Exception:
        pass

    if firmadyne:
        try:
            ds_options += cu.get_image_info(image, ["fdyne_dslc"])[0] or []
        except Exception:
            print("No Firmadyne DSLC options available for this image")

    print("Vermagic", info[3])
    if override_vermagic:
        info[3], ds_options = modify_the_vermagic(info[3], ds_options)

    print(info[0], info[1], info[5], info[3], info[7])
    compile_kernel(image, ds_options, ds_recovery, s_mod_dir, s_config, openwrt, *info)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argp.ArgumentParser(description="Compile the FirmSolo kernel for an image")
    parser.add_argument("image")
    parser.add_argument("-f", "--ds_opt_fl",        default=None)
    parser.add_argument("-l", "--ds_opt_list",       nargs="*", default=[])
    parser.add_argument("-d", "--ds_recovery",       type=int,  default=0)
    parser.add_argument("-m", "--s_mod_dir",         default="")
    parser.add_argument("-s", "--s_config",          default="yes")
    parser.add_argument("-o", "--override_vermagic", action="store_true")
    parser.add_argument("-w", "--openwrt",           action="store_true")
    parser.add_argument("-e", "--firmadyne",         action="store_true")
    res = parser.parse_args()

    run_the_compilation(
        res.image, res.ds_opt_fl, res.ds_opt_list,
        res.ds_recovery, res.s_mod_dir, res.s_config,
        res.override_vermagic, res.openwrt, res.firmadyne,
    )

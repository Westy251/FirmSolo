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


def _parse_makefile(path: str) -> List[MakefileAssignment]:
    """Parse *path* into a flat list of build-object assignments.

    Pre-processing:
    * Comments (``#…``) are stripped.
    * Backslash–newline continuations are joined.
    * Non-build lines (CFLAGS, rule bodies, …) are discarded.
    """
    try:
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"  [Makefile] Cannot open {path}: {exc}")
        return []

    text = re.sub(r"#[^\n]*", "", text)   # strip comments
    text = text.replace("\\\n", " ")       # join continuations

    results: List[MakefileAssignment] = []
    for line in text.splitlines():
        m = _ASSIGNMENT_RE.match(line.strip())
        if not m:
            continue
        variable, operator, rhs = m.group(1), m.group(2), m.group(3)
        if _is_build_obj_var(variable):
            results.append(MakefileAssignment(variable, operator, rhs.split()))
    return results


def _config_from_direct_gate(variable: str) -> Optional[str]:
    """Return ``CONFIG_X`` when *variable* is ``obj-$(CONFIG_X)``, else None.

    >>> _config_from_direct_gate("obj-$(CONFIG_NET)")
    'CONFIG_NET'
    >>> _config_from_direct_gate("net-y")
    None
    """
    m = _DIRECT_GATE_RE.match(variable)
    return m.group(1) if m else None


def _composite_stem(variable: str) -> Optional[str]:
    """Return the object stem when *variable* is a composite-list variable.

    ``net-y``, ``ipe-objs``, ``foo-$(CONFIG_X)`` → ``'net'``, ``'ipe'``, ``'foo'``.
    ``obj-y`` and ``obj-$(CONFIG_X)`` → ``None`` (handled by direct-gate path).

    >>> _composite_stem("net-y")
    'net'
    >>> _composite_stem("obj-$(CONFIG_NET)")  # not a composite
    None
    """
    m = _COMPOSITE_VAR_RE.match(variable)
    if not m:
        return None
    stem = m.group(1)
    return None if stem == "obj" else stem


# ── Makefile querying (zero regex) ───────────────────────────────────────────


def _query_configs_for_target(
    target_name: str,
    assignments: List[MakefileAssignment],
) -> Set[str]:
    configs: Set[str] = set()

    for assignment in assignments:
        if target_name not in assignment.targets:
            continue

        # Extract CONFIG_ symbol if present on LHS (e.g., obj-$(CONFIG_X) or pci-$(CONFIG_X))
        cfg = _config_from_direct_gate(assignment.variable)
        if cfg:
            configs.add(cfg)

        # Pattern 2 — composite: target_name lives inside foo.o
        stem = _composite_stem(assignment.variable)
        if stem:
            composite_obj = stem + ".o"
            for a2 in assignments:
                if composite_obj in a2.targets:
                    cfg2 = _config_from_direct_gate(a2.variable)
                    if cfg2:
                        configs.add(cfg2)

    return configs



def _find_configs_via_cscope_text(image_dir: str, rel_file: str) -> Set[str]:
    """Use cscope text search (-L6) to find Makefile/Kbuild lines matching object stem."""
    configs: Set[str] = set()
    cscope_db = os.path.join(image_dir, "cscope.out")
    if not os.path.exists(cscope_db):
        return configs

    # 'drivers/pci/pci-sysfs.c' -> 'pci-sysfs'
    stem = os.path.splitext(os.path.basename(rel_file))[0]

    try:
        res = subprocess.run(
            ["cscope", "-d", "-f", cscope_db, "-L6", stem],
            cwd=image_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for line in res.stdout.splitlines():
            # Only extract CONFIG_ tokens from Makefile/Kbuild lines
            if "Makefile" in line or "Kbuild" in line:
                for cfg in re.findall(r"CONFIG_[A-Za-z0-9_]+", line):
                    configs.add(cfg)
    except OSError as exc:
        print(f"  [cscope -L6] Failed: {exc}")

    return configs

def _resolve_target_in_assignments(assignments: List[Tuple[str, List[str]]], target_name: str) -> Tuple[Set[str], Set[str]]:
    """Find all configs and composite parent targets (e.g. 'proc' for 'base.o') associated with target_name."""
    configs = set()
    parent_targets = set()

    for lhs, rhs in assignments:
        if target_name in rhs:
            # Extract any CONFIG_ embedded in the LHS variable name (e.g. pci-$(CONFIG_SYSFS))
            for cfg in re.findall(r"CONFIG_[A-Za-z0-9_]+", lhs):
                configs.add(cfg)

            # Extract any CONFIG_ passed as RHS tokens (e.g. obj-$(CONFIG_PCI))
            for token in rhs:
                if token.startswith("CONFIG_"):
                    configs.add(token)

            # Check if this target is part of a composite parent (e.g. 'proc-y' or 'ipe-objs')
            comp_match = re.match(r"^([A-Za-z0-9_-]+)-(?:y|objs|\$\(CONFIG_[A-Za-z0-9_]+\))$", lhs)
            if comp_match:
                parent_stem = comp_match.group(1)
                if parent_stem != "obj":
                    parent_targets.add(f"{parent_stem}.o")

    return configs, parent_targets

def _parse_makefile_assignments(filepath: str) -> List[Tuple[str, List[str]]]:
    """Parse a Makefile into a list of (LHS, [RHS_tokens]) assignments, handling '\\' continuations."""
    if not os.path.exists(filepath):
        return []

    # 1. Join line continuations ('\\') and strip comments
    raw_text = ""
    with open(filepath, "r", errors="replace") as fh:
        for line in fh:
            line = line.split("#")[0]  # Strip inline comments
            if line.endswith("\\\n") or line.endswith("\\"):
                raw_text += line.rstrip("\\\n").rstrip("\\") + " "
            else:
                raw_text += line + "\n"

    # 2. Tokenize assignments (+=, :=, =)
    assignments = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        parts = re.split(r"\+?=|:=", line, maxsplit=1)
        if len(parts) == 2:
            lhs = parts[0].strip()
            rhs_tokens = parts[1].strip().split()
            assignments.append((lhs, rhs_tokens))

    return assignments

def _extract_configs(text: str) -> Set[str]:
    """Extract 'CONFIG_FOO' symbols using plain string parsing."""
    configs = set()
    # Normalize Makefile syntax like $(CONFIG_FOO) into clean tokens
    clean_text = text.replace('$(', ' ').replace(')', ' ').replace('=', ' ')
    for token in clean_text.split():
        if 'CONFIG_' in token:
            # Isolate the CONFIG_ symbol name
            start = token.find('CONFIG_')
            sub = token[start:]
            # Grab characters until non-alphanumeric/underscore
            cfg_name = []
            for char in sub:
                if char.isalnum() or char == '_':
                    cfg_name.append(char)
                else:
                    break
            configs.add("".join(cfg_name))
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

def _find_configs_for_file(image_dir: str, rel_file: str) -> Set[str]:
    """Deterministically resolve Kbuild CONFIG_ gates scoped strictly to the file's path."""
    configs: Set[str] = set()

    rel_dir = os.path.dirname(rel_file)                     # e.g., 'fs/proc' or 'security/safesetid'
    stem = os.path.splitext(os.path.basename(rel_file))[0]  # e.g., 'base' or 'securityfs'
    target_obj = f"{stem}.o"

    # -------------------------------------------------------------
    # 1. Scoped Local Makefile Graph Resolution
    # -------------------------------------------------------------
    local_makefile = os.path.join(image_dir, rel_dir, "Makefile")
    if not os.path.exists(local_makefile):
        local_makefile = os.path.join(image_dir, rel_dir, "Kbuild")

    if os.path.exists(local_makefile):
        lines = _get_clean_lines(local_makefile)
        targets_to_resolve = {target_obj}
        visited_targets = set()

        while targets_to_resolve:
            curr_target = targets_to_resolve.pop()
            visited_targets.add(curr_target)

            for line in lines:
                lhs, rhs_tokens = _parse_assignment(line)
                if not lhs:
                    continue

                if curr_target in rhs_tokens:
                    # Extract CONFIG_ from both LHS (e.g. pci-$(CONFIG_SYSFS)) and RHS
                    configs.update(_extract_configs_from_string(line))

                    # Check if assigned to a composite parent (e.g. proc-y += base.o or safesetid-y := securityfs.o)
                    if '-' in lhs:
                        parent_stem = lhs.split('-')[0].strip()
                        if parent_stem not in ("obj", "subdir", "core", "drivers", "libs"):
                            p_obj = f"{parent_stem}.o"
                            if p_obj not in visited_targets:
                                targets_to_resolve.add(p_obj)

    # -------------------------------------------------------------
    # 2. Parent Directory Walk (Strict Path Traversal)
    # -------------------------------------------------------------
    curr_dir = rel_dir
    while curr_dir and curr_dir != ".":
        parent_dir = os.path.dirname(curr_dir)
        folder_name = os.path.basename(curr_dir)
        folder_tokens = {f"{folder_name}/", folder_name, f"{folder_name}.o"}

        parent_makefile = os.path.join(image_dir, parent_dir, "Makefile")
        if not os.path.exists(parent_makefile):
            parent_makefile = os.path.join(image_dir, parent_dir, "Kbuild")

        if os.path.exists(parent_makefile):
            lines = _get_clean_lines(parent_makefile)
            for line in lines:
                lhs, rhs_tokens = _parse_assignment(line)
                # Ensure folder match is exact within RHS tokens
                if any(ft in rhs_tokens for ft in folder_tokens):
                    configs.update(_extract_configs_from_string(line))

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
    symbols:   List[SymbolSpec],
) -> List[str]:
    """Map a list of symbol specs to the Makefile CONFIG gates that compile them.

    For each spec there are two paths:

    **Fast path** — the spec already carries a source-file path (e.g. tuples
    from :func:`find_caller_configs`).  The file is used directly; cscope is
    not consulted.  This avoids the line-number mismatch that would occur
    if we passed a call-site line number to a cscope definition lookup.

    **Slow path** — bare symbol name only.  cscope locates the definition file.

    In both cases :func:`_find_configs_for_file` then climbs the directory
    tree collecting CONFIG gates from each Makefile it encounters.
    """
    found_configs: Set[str] = set()
    cscope_db = os.path.join(image_dir, "cscope.out")

    if not symbols:
        return []

    if not os.path.exists(cscope_db):
        print(f"  [resolve_symbols] cscope.out not found: {cscope_db}")
        return []

    for spec in symbols:
        if not spec:
            continue

        sym, file_spec, line_spec = parse_symbol_spec(spec)
        defined_files: Set[str] = set()

        # Fast path: trust the caller's file information
        if file_spec:
            rel = (
                os.path.relpath(file_spec, image_dir)
                if os.path.isabs(file_spec) else file_spec
            )
            if os.path.exists(os.path.join(image_dir, rel)):
                defined_files.add(rel)

        # Slow path: ask cscope to locate the definition
        if not defined_files and sym and C_IDENTIFIER_RE.match(sym):
            defined_files = _cscope_find_definition_files(
                image_dir, cscope_db, sym, file_spec, line_spec
            )

        for rel_file in defined_files:
            # 1. Standard Makefile directory-climbing walk
            thisFunc = _find_configs_for_file(image_dir, rel_file)

            # 2. Fast cscope -L6 search in indexed Makefiles (catches complex Kbuild syntax)
            thisFunc.update(_find_configs_via_cscope_text(image_dir, rel_file))



            print (spec, ":", thisFunc)
            
            found_configs.update(thisFunc)

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

        resolved = resolve_symbols_to_configs(image_dir, symbolz)
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

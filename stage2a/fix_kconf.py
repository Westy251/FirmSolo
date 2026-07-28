#!/usr/bin/env python3
"""
fix_kconf.py – patch Kconfig files before kconfiglib runs.

Key fixes in this version
─────────────────────────
1. _replace_build_macros: now handles $(call cc-option,…) / $(call as-option,…)
   in addition to direct $(cc-option,…) calls.
2. _replace_build_macros: preserves line count when a multi-line macro call is
   collapsed, preventing downstream line-number shifts.
3. _patch_kconfig_include_file: stubs RHS expressions containing $(call …) not
   just $(shell …).  Continuation lines after a patched definition are now
   skipped so orphaned text cannot reach kconfiglib.
4. _sanitize_kconfig_file: post-processing safety net that comments out any
   unindented line that is not a recognised Kconfig keyword, assignment, or
   macro call.  Catches whatever the earlier passes miss.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Optional

# ── Kconfig subsystem relocation table ───────────────────────────────────────
KCONFIG_RELOCATED: dict = {
    "drivers/telephony/Kconfig": None,
    "drivers/ide/Kconfig": None,
    "sound/oss/Kconfig": None,
    "drivers/serial/Kconfig": "drivers/tty/serial/Kconfig",
    "drivers/usb/net/Kconfig": "drivers/net/usb/Kconfig",
    "drivers/staging/iio/light/Kconfig": "drivers/iio/light/Kconfig",
    "drivers/staging/iio/accel/Kconfig": "drivers/iio/accel/Kconfig",
    "drivers/staging/iio/gyro/Kconfig": "drivers/iio/gyro/Kconfig",
    "drivers/staging/iio/magnetometer/Kconfig": "drivers/iio/magnetometer/Kconfig",
}

# ── Macro classification ──────────────────────────────────────────────────────
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
        "failure",
        "if-success",
        "if-failure",
        "path-exists",
        "error-if",
    }
)

_ALL_BUILD_MACROS = _VERSION_MACROS | _OPTION_MACROS

# ── Valid top-level Kconfig line starters ─────────────────────────────────────
# Used by the sanitiser – any unindented, non-comment line that does NOT start
# with one of these (and is not an assignment or macro call) is a syntax error.
_KCONFIG_KEYWORDS = frozenset(
    {
        # block starters / enders
        "config",
        "menuconfig",
        "choice",
        "endchoice",
        "menu",
        "endmenu",
        "if",
        "endif",
        "source",
        "rsource",
        "osource",
        "orsource",
        "comment",
        "mainmenu",
        # symbol properties
        "bool",
        "int",
        "hex",
        "string",
        "tristate",
        "def_bool",
        "def_int",
        "def_hex",
        "def_string",
        "def_tristate",
        "default",
        "depends",
        "select",
        "imply",
        "range",
        "help",
        "---help---",
        "optional",
        "visible",
        "modules",
        "prompt",
        "option",
        "on",
    }
)


# ══════════════════════════════════════════════════════════════════════════════
# Core macro-replacement engine
# ══════════════════════════════════════════════════════════════════════════════


def _replace_build_macros(text: str) -> str:
    """
    Replace every kernel build-tool macro call in *text* with a safe static
    value and preserve line count.

    Handled forms
    ─────────────
    $(cc-option,…)              → 'n'
    $(as-version)               → '0'
    $(call cc-option,…)         → 'n'   ← NEW: indirect $(call …) form
    $(call as-version)          → '0'   ← NEW
    any $(hyphenated-name,…)    → 'n'   (heuristic catch-all)

    When a replacement spans N lines the output includes N-1 trailing newlines
    so that all subsequent line numbers remain identical to the original file.
    """
    result: list = []
    i, n = 0, len(text)

    while i < n:
        if text[i] == "$" and i + 1 < n and text[i + 1] == "(":
            j = i + 2
            while j < n and (text[j].isalnum() or text[j] == "-"):
                j += 1
            macro_name = text[i + 2 : j]

            # ── Case 1: $(call build-macro, args) ────────────────────────────
            # 'call' itself has no hyphen so the heuristic below misses it.
            if macro_name == "call" and j < n and text[j] == ",":
                k2 = j + 1
                while k2 < n and text[k2] == " ":
                    k2 += 1
                j2 = k2
                while j2 < n and (text[j2].isalnum() or text[j2] in "-_"):
                    j2 += 1
                called = text[k2:j2]

                if called in _ALL_BUILD_MACROS or ("-" in called and len(called) > 1):
                    depth, k = 1, j  # j points to first ',' of outer $(
                    while k < n and depth:
                        if text[k] == "(":
                            depth += 1
                        elif text[k] == ")":
                            depth -= 1
                        k += 1
                    span = text[i:k]
                    newlines = span.count("\n")
                    is_ver = (
                        called in _VERSION_MACROS
                        or called.endswith("-version")
                        or called.endswith("-major-version")
                    )
                    result.append("0" if is_ver else "n")
                    if newlines:
                        result.append("\n" * newlines)
                    i = k
                    continue

            # ── Case 2: direct $(cc-option,…) / $(as-version) / etc. ─────────
            is_build = macro_name in _ALL_BUILD_MACROS or (
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
                span = text[i:k]
                newlines = span.count("\n")
                is_ver = (
                    macro_name in _VERSION_MACROS
                    or macro_name.endswith("-version")
                    or macro_name.endswith("-major-version")
                )
                result.append("0" if is_ver else "n")
                if newlines:
                    result.append("\n" * newlines)
                i = k
                continue

        result.append(text[i])
        i += 1

    return "".join(result)


def _neutralise_build_macros(image_dir: str) -> None:
    """
    Walk every Kconfig file (excluding Kconfig.include) and rewrite build
    macro calls in-place using _replace_build_macros.
    """
    count = 0
    for dirpath, _, filenames in os.walk(image_dir):
        for fn in filenames:
            if fn != "Kconfig" and not fn.startswith("Kconfig."):
                continue
            if fn == "Kconfig.include":  # handled separately
                continue
            fpath = os.path.join(dirpath, fn)
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
    if count:
        print(
            "  fix_kconf: neutralised build macros in {} Kconfig file(s)".format(count)
        )


# ══════════════════════════════════════════════════════════════════════════════
# Kconfig.include targeted patching
# ══════════════════════════════════════════════════════════════════════════════


def _patch_kconfig_include_file(fpath: str) -> bool:
    """
    Patch one Kconfig.include file with safe static macro stubs.

    Improvements over previous version
    ────────────────────────────────────
    • RHS containing $(call …) is now also stubbed (not just $(shell …)).
    • Continuation lines (\\ at end) after a patched definition are skipped so
      they cannot reach kconfiglib as orphaned text.
    • All tool-invocation patterns ($(CC), $(LD), $(AS), $(RUSTC)) trigger a
      stub even when not prefixed with $(shell or $(call.
    """
    with open(fpath, "r", errors="replace") as f:
        lines = f.readlines()

    new_lines: list = []
    changed = False
    skip_continuations = False  # True after patching a line ending in '\'

    for line in lines:
        stripped = line.rstrip()
        lstripped = stripped.lstrip()

        # ── Skip continuation lines from a previously patched definition ─────
        if skip_continuations:
            if stripped.endswith("\\"):
                new_lines.append("# [firmsolo-cont] {}\n".format(stripped))
            else:
                new_lines.append("# [firmsolo-cont] {}\n".format(stripped))
                skip_continuations = False
            changed = True
            continue

        # ── Preserve blank lines and comments ─────────────────────────────────
        if not lstripped or lstripped.startswith("#"):
            new_lines.append(line)
            continue

        # ── Variable / macro assignment ────────────────────────────────────────
        m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s*(:?=)\s*(.*)$", stripped)
        if m:
            name, op, rhs = m.group(1), m.group(2), m.group(3)
            has_cont = stripped.endswith("\\")

            # Version macros → integer 0
            if (
                name in _VERSION_MACROS
                or name.endswith("-version")
                or name.endswith("-major-version")
            ):
                new_lines.append("{} {} 0\n".format(name, op))
                changed = True
                if has_cont:
                    skip_continuations = True
                continue

            # Option/test macros → boolean n
            if name in _OPTION_MACROS:
                new_lines.append("{} {} y\n".format(name, op))
                changed = True
                if has_cont:
                    skip_continuations = True
                continue

            # Any RHS that invokes external tools → stub n (or 0 for versions)
            _TOOL_PATTERNS = (
                "$(shell",
                "$(call",
                "$(CC)",
                "$(LD)",
                "$(AS)",
                "$(NM)",
                "$(RUSTC)",
                "$(AR)",
            )
            if any(p in rhs for p in _TOOL_PATTERNS):
                is_ver = (
                    name in _VERSION_MACROS
                    or name.endswith("-version")
                    or name.endswith("-major-version")
                )
                new_lines.append("{} {} {}\n".format(name, op, "0" if is_ver else "n"))
                changed = True
                if has_cont:
                    skip_continuations = True
                continue

        # ── Top-level macro CALL (not an assignment) ──────────────────────────
        # e.g.  $(error-if,$(success,cmd),message)
        # After generic preprocessing these would become bare 'n' → syntax error.
        if lstripped.startswith("$("):
            new_lines.append("# [firmsolo-call] {}\n".format(stripped))
            changed = True
            if stripped.endswith("\\"):
                skip_continuations = True
            continue

        new_lines.append(line)

    if changed:
        with open(fpath, "w") as f:
            f.writelines(new_lines)
    return changed


def _patch_kconfig_include_files(image_dir: str) -> None:
    """Patch every Kconfig.include file found under image_dir."""
    count = 0
    for dirpath, _, filenames in os.walk(image_dir):
        for fn in filenames:
            if fn != "Kconfig.include":
                continue
            fpath = os.path.join(dirpath, fn)
            try:
                if _patch_kconfig_include_file(fpath):
                    count += 1
            except OSError as exc:
                print("  fix_kconf: could not patch {}: {}".format(fpath, exc))
    if count:
        print(
            "  fix_kconf: patched {} Kconfig.include file(s) "
            "with safe macro stubs".format(count)
        )


# ══════════════════════════════════════════════════════════════════════════════
# Post-processing sanitiser
# ══════════════════════════════════════════════════════════════════════════════


def _sanitize_kconfig_file(fpath: str) -> bool:
    """
    Comment out lines that would cause kconfiglib to raise a syntax error.

    A line is 'invalid' when, outside of a help block and a continuation
    sequence, it:
      – is not empty / a comment
      – does not start with a recognised Kconfig keyword
      – does not look like a make-style variable assignment (contains '=')
      – does not start with '$(' (macro call)

    This is a safety net for whatever residue earlier passes left behind.
    """
    with open(fpath, "r", errors="replace") as f:
        lines = f.readlines()

    new_lines: list = []
    changed = False
    in_help = False
    prev_cont = False  # previous physical line ended with '\'

    for line in lines:
        stripped = line.strip()
        lstripped = stripped

        # ── Continuation from previous line ───────────────────────────────────
        if prev_cont:
            prev_cont = stripped.endswith("\\")
            new_lines.append(line)
            continue

        prev_cont = stripped.endswith("\\")

        # ── Empty lines ───────────────────────────────────────────────────────
        if not stripped:
            new_lines.append(line)
            # Blank lines do NOT end a help block in Kconfig
            continue

        # ── Comments ──────────────────────────────────────────────────────────
        if stripped.startswith("#"):
            new_lines.append(line)
            continue

        # ── Inside help block: indented text is valid ─────────────────────────
        if in_help:
            if line[0] in (" ", "\t"):
                new_lines.append(line)
                continue
            else:
                in_help = False  # unindented non-blank line ends help

        # ── Check first token ─────────────────────────────────────────────────
        first = lstripped.split()[0] if lstripped.split() else ""

        if first in _KCONFIG_KEYWORDS:
            if first in ("help", "---help---"):
                in_help = True
            new_lines.append(line)
            continue

        # ── Make-style assignment ─────────────────────────────────────────────
        if "=" in stripped:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:?=", stripped):
                new_lines.append(line)
                continue

        # ── Macro call ────────────────────────────────────────────────────────
        if stripped.startswith("$("):
            new_lines.append(line)
            continue

        # ── Anything else is a kconfiglib syntax error – comment it out ───────
        new_lines.append("# [firmsolo-sanitized] {}".format(line))
        changed = True
        print(
            "  fix_kconf: sanitized '{}' in {}:{}".format(
                stripped[:60],
                os.path.relpath(fpath, os.path.commonpath([fpath])),
                sum(1 for l in new_lines),  # approximate line number
            )
        )

    if changed:
        with open(fpath, "w") as f:
            f.writelines(new_lines)
    return changed


def _sanitize_kconfig_files(image_dir: str) -> None:
    """
    Run _sanitize_kconfig_file on every Kconfig file (not Kconfig.include)
    under image_dir.  Called after all other preprocessing so it catches
    whatever earlier passes left behind.
    """
    count = 0
    for dirpath, _, filenames in os.walk(image_dir):
        for fn in filenames:
            if fn != "Kconfig" and not fn.startswith("Kconfig."):
                continue
            if fn == "Kconfig.include":
                continue
            fpath = os.path.join(dirpath, fn)
            try:
                if _sanitize_kconfig_file(fpath):
                    count += 1
            except OSError:
                pass
    if count:
        print("  fix_kconf: sanitized {} Kconfig file(s)".format(count))


# ══════════════════════════════════════════════════════════════════════════════
# Subsystem-specific patches
# ══════════════════════════════════════════════════════════════════════════════


def _comment_out_removed_sources(image_dir: str) -> None:
    targets = [
        "Kconfig",
        "arch/arm/Kconfig",
        "arch/x86/Kconfig",
        "arch/mips/Kconfig",
        "arch/arm64/Kconfig",
        "drivers/Kconfig",
    ]
    for rel in targets:
        full = os.path.join(image_dir, rel)
        if not os.path.exists(full):
            continue
        with open(full, "r", errors="replace") as f:
            lines = f.readlines()
        changed = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("source"):
                m = re.match(r'source\s+"?([^\s"]+)"?', stripped)
                if m:
                    sourced = m.group(1)
                    if (
                        KCONFIG_RELOCATED.get(sourced) is None
                        and sourced in KCONFIG_RELOCATED
                    ):
                        line = "# " + line
                        changed = True
            new_lines.append(line)
        if changed:
            with open(full, "w") as f:
                f.writelines(new_lines)
            print(
                "  fix_kconf: commented out removed source directives in '{}'".format(
                    rel
                )
            )


def _fix_hwmon(image_dir: str, kernel: str) -> None:
    full = os.path.join(image_dir, "drivers/hwmon/Kconfig")
    if not os.path.exists(full):
        return
    with open(full, "r", errors="replace") as f:
        content = f.read()
    if "F75375" in content or "F75373" in content:
        print('tristate "Fintek F75375S/SP and F75373";')
    patched = re.sub(r"\s*&&\s*EXPERIMENTAL", "", content)
    if patched != content:
        with open(full, "w") as f:
            f.write(patched)


def _inject_firmadyne_kconfig(image_dir: str) -> None:
    import custom_utils as cu

    src = os.path.join(cu.scripts_dir, "Kconfig")
    if not os.path.exists(src):
        print(
            "  fix_kconf: {} not found – skipping Firmadyne Kconfig injection".format(
                src
            )
        )
        return
    dst_dir = os.path.join(image_dir, "drivers/firmadyne")
    dst = os.path.join(dst_dir, "Kconfig")
    os.makedirs(dst_dir, exist_ok=True)
    subprocess.call("cp {} {}".format(src, dst), shell=True)
    drivers_kconf = os.path.join(image_dir, "drivers/Kconfig")
    if os.path.exists(drivers_kconf):
        with open(drivers_kconf, "r", errors="replace") as f:
            content = f.read()
        stub = '\nsource "drivers/firmadyne/Kconfig"\n'
        if stub.strip() not in content:
            with open(drivers_kconf, "a") as f:
                f.write(stub)
    print("  fix_kconf: Firmadyne Kconfig injected")


# ── Validation probes ─────────────────────────────────────────────────────────
_valid_counter = 0


def _valid(image_dir: str, rel_path: str, expected_token: str) -> None:
    global _valid_counter
    _valid_counter += 1
    tag = "Valid{}".format(_valid_counter)
    resolved = KCONFIG_RELOCATED.get(rel_path, rel_path)
    if resolved is None:
        print("File {} not found".format(rel_path))
        return
    full = os.path.join(image_dir, resolved)
    if not os.path.exists(full):
        print("File {} not found".format(rel_path))
        return
    with open(full, "r", errors="replace") as f:
        content = f.read()
    if expected_token in content:
        print(tag)


# ══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════════


def _fix_legacy_option_modules(image_dir: str) -> None:
    """
    Linux 6.x introduced a standalone 'modules' keyword in Kconfig.
    - Native C kconfig (Linux 6.18) fails if changed to 'option modules'.
    - Python kconfiglib crashes on standalone 'modules'.
    Commenting it out allows both native 'make tinyconfig' and 'kconfiglib' to pass.
    """
    for dirpath, _, filenames in os.walk(image_dir):
        for fn in filenames:
            if fn == "Kconfig" or fn.startswith("Kconfig."):
                fpath = os.path.join(dirpath, fn)
                try:
                    with open(fpath, "r", errors="replace") as f:
                        content = f.read()
                    # Comment out standalone 'modules' line
                    updated = re.sub(
                        r"(?m)^(\s*)modules\s*$", r"\1# [firmsolo-fix] modules", content
                    )
                    if updated != content:
                        with open(fpath, "w") as f:
                            f.write(updated)
                except OSError:
                    pass


# Lovely little LTO Patch
def _fix_llvm_toolchain_kconfig(image_dir: str) -> None:
    """
    FirmSolo's macro neutralization sets $(success,...) macro checks to 'n'.
    This breaks CC_IS_CLANG, LD_IS_LLD, AS_IS_LLVM, and HAS_LTO_CLANG.
    This function restores Clang/LLVM capability symbols so LTO options are selectable.
    """
    # 1. Force Clang/LLVM toolchain symbols in init/Kconfig
    init_kconfig = os.path.join(image_dir, "init/Kconfig")
    if os.path.exists(init_kconfig):
        with open(init_kconfig, "r", errors="replace") as f:
            content = f.read()

        content = re.sub(
            r"config CC_IS_CLANG\s*\n\s*def_bool\s+.*",
            "config CC_IS_CLANG\n\tdef_bool y",
            content,
        )
        content = re.sub(
            r"config LD_IS_LLD\s*\n\s*def_bool\s+.*",
            "config LD_IS_LLD\n\tdef_bool y",
            content,
        )
        content = re.sub(
            r"config AS_IS_LLVM\s*\n\s*def_bool\s+.*",
            "config AS_IS_LLVM\n\tdef_bool y",
            content,
        )

        with open(init_kconfig, "w") as f:
            f.write(content)

    # 2. Strip broken HAS_LTO_CLANG dependencies in arch/Kconfig
    arch_kconfig = os.path.join(image_dir, "arch/Kconfig")
    if os.path.exists(arch_kconfig):
        with open(arch_kconfig, "r", errors="replace") as f:
            content = f.read()

        lines = content.splitlines()
        in_has_lto = False
        new_lines = []
        for line in lines:
            if line.strip().startswith("config HAS_LTO_CLANG"):
                in_has_lto = True
                new_lines.append(line)
                continue
            if in_has_lto:
                if line.strip().startswith("config ") or line.strip().startswith(
                    "choice"
                ):
                    in_has_lto = False
                elif "NM" in line or "AR" in line or line.strip() == "depends on n":
                    # Skip the neutralized toolchain checks
                    continue
            new_lines.append(line)

        with open(arch_kconfig, "w") as f:
            f.write("\n".join(new_lines) + "\n")


def fix_configs(image_dir: str, kernel: str) -> None:
    """
    Apply all Kconfig-level fixes to the kernel tree.
    Must be called before any kconfiglib or make invocation.

    Processing order matters:
      1. Patch Kconfig.include files FIRST (macro definitions)
      2. Neutralise build macros in all other Kconfig files
      3. Sanitise remaining syntax errors
      4. Comment out removed subsystem source directives
      5. Subsystem-specific patches
      6. Inject Firmadyne stub
      7. Validation probes
    """
    global _valid_counter
    _valid_counter = 0

    if not image_dir.endswith("/"):
        image_dir += "/"

    # 1 – Kconfig.include: targeted static stubs
    _patch_kconfig_include_files(image_dir)

    _fix_legacy_option_modules(image_dir)

    # 2 – All other Kconfig files: replace $(cc-option,…) / $(as-version) etc.
    #     Now also replaces $(call cc-option,…) via the updated engine.
    _neutralise_build_macros(image_dir)
    _fix_llvm_toolchain_kconfig(image_dir)

    # 3 – Post-processing sanitiser: comment out any remaining invalid lines
    _sanitize_kconfig_files(image_dir)

    # 4 – Comment out source directives for removed subsystems
    _comment_out_removed_sources(image_dir)

    # 5 – Subsystem patches
    _fix_hwmon(image_dir, kernel)

    # 6 – Firmadyne stub
    _inject_firmadyne_kconfig(image_dir)

    # 7 – Validation probes
    _valid(image_dir, "drivers/telephony/Kconfig", "telephony")
    _valid(image_dir, "drivers/hwmon/Kconfig", "F75375")
    _valid(image_dir, "drivers/ide/Kconfig", "BLK_DEV_IDE")
    _valid(image_dir, "drivers/serial/Kconfig", "SERIAL")
    _valid(image_dir, "drivers/usb/net/Kconfig", "USB_NET")
    _valid(image_dir, "drivers/staging/iio/light/Kconfig", "IIO")

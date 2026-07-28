#!/usr/bin/env python3
"""
hot_fixes.py – source-level patches applied to the extracted kernel tree
before kconfiglib processing and compilation.

Linux 6.x compatibility added:
  * RELOCATED table maps files that moved or were removed since ~5.x.
  * resolve_path() follows that table so callers never open a missing file.
  * All individual fix_*() functions guard themselves with resolve_path().
"""

import os
import re

# ── Relocation / removal table ────────────────────────────────────────────────
# key   = path relative to kernel root as used by old scripts
# value = ordered list of candidate new paths; empty list = removed, skip silently

RELOCATED: dict[str, list[str]] = {
    # kernel/module.c was split into kernel/module/ in Linux 5.19
    "kernel/module.c": [
        "kernel/module/main.c",
        "kernel/module/core.c",
    ],
    # Pre-generated flex/bison outputs removed when dtc gained proper build rules
    "scripts/dtc/dtc-lexer.lex.c_shipped": [],
    "scripts/dtc/dtc-parser.tab.c_shipped": [],
    "scripts/dtc/dtc-parser.tab.h_shipped": [],
    # timeconst.pl dropped; HZ constants are now handled differently in-tree
    "kernel/timeconst.pl": [],
}


def resolve_path(image_dir: str, rel_path: str) -> str | None:
    """
    Return the absolute path to rel_path inside image_dir, following
    RELOCATED for files that moved or were removed between kernel versions.
    Returns None if the file cannot be found at any known location.
    """
    primary = os.path.join(image_dir, rel_path)
    if os.path.exists(primary):
        return primary

    for candidate in RELOCATED.get(rel_path, []):
        full = os.path.join(image_dir, candidate)
        if os.path.exists(full):
            print(f"  hot_fixes: '{rel_path}' relocated → '{candidate}'")
            return full

    if rel_path in RELOCATED:
        if RELOCATED[rel_path]:
            print(
                f"  hot_fixes: '{rel_path}' not found at any known location – skipping"
            )
        else:
            print(
                f"  hot_fixes: '{rel_path}' removed in this kernel version – skipping"
            )
    else:
        print(f"Error with fixing {os.path.join(image_dir, rel_path)}")
        print(
            f"[Errno 2] No such file or directory: '{os.path.join(image_dir, rel_path)}'"
        )
    return None


# ── Individual fix functions ──────────────────────────────────────────────────


def _fix_module_c(image_dir: str, kernel: str) -> None:
    """
    Patch kernel/module.c (or kernel/module/main.c for 5.19+) to inject
    FirmSolo extern declarations after the last top-level #include.
    Idempotent: skips if the marker is already present.
    """
    target = resolve_path(image_dir, "kernel/module.c")
    if target is None:
        return

    try:
        with open(target, "r", errors="replace") as f:
            content = f.read()

        if "fdyne_syscall" in content:
            return  # already patched

        inject = (
            "\n/* FirmSolo/Firmadyne extern declarations – auto-injected */\n"
            "extern unsigned int fdyne_syscall;\n"
            "extern unsigned int fdyne_execute;\n"
            "extern unsigned int fdyne_reboot;\n"
            "extern unsigned int firmsolo;\n\n"
        )
        # Insert after the last top-level #include block
        ends = [m.end() for m in re.finditer(r"^#include\s+[<\"][^\n]+", content, re.M)]
        insert_at = ends[-1] if ends else 0
        content = content[:insert_at] + inject + content[insert_at:]

        with open(target, "w") as f:
            f.write(content)

    except Exception as e:
        print(f"  hot_fixes: error patching {target}: {e}")


def _fix_dtc_shipped(image_dir: str) -> None:
    """
    Older kernels needed a strict-aliasing workaround in the pre-generated
    dtc flex output.  The files no longer exist in 6.x; skip silently.
    """
    for rel in (
        "scripts/dtc/dtc-lexer.lex.c_shipped",
        "scripts/dtc/dtc-parser.tab.c_shipped",
    ):
        target = resolve_path(image_dir, rel)
        if target is None:
            continue
        try:
            with open(target, "r", errors="replace") as f:
                content = f.read()
            patched = content.replace(
                "#define YY_DO_BEFORE_ACTION",
                '#pragma GCC diagnostic ignored "-Wstrict-aliasing"\n'
                "#define YY_DO_BEFORE_ACTION",
            )
            if patched != content:
                with open(target, "w") as f:
                    f.write(patched)
        except Exception as e:
            print(f"  hot_fixes: error patching {target}: {e}")


def _fix_timeconst(image_dir: str) -> None:
    """
    kernel/timeconst.pl was needed by older kernels; ensure it is executable
    when present.  Skips silently when absent (6.x).
    """
    target = resolve_path(image_dir, "kernel/timeconst.pl")
    if target is None:
        return
    try:
        os.chmod(target, 0o755)
    except Exception as e:
        print(f"  hot_fixes: could not chmod {target}: {e}")


def _fix_clang_compat(image_dir: str, kernel: str) -> None:
    """
    Placeholder for any clang-specific source patches.
    Linux 6.x has upstream Clang support so this is currently a no-op.
    """
    pass


# ── Public entry point ────────────────────────────────────────────────────────


def hot_fixes(image_dir: str, kernel: str) -> None:
    """
    Apply all hot-fixes to the kernel source tree at image_dir.
    image_dir should end with '/'.
    """
    if not image_dir.endswith("/"):
        image_dir += "/"

    _fix_module_c(image_dir, kernel)
    _fix_dtc_shipped(image_dir)
    _fix_timeconst(image_dir)
    _fix_clang_compat(image_dir, kernel)

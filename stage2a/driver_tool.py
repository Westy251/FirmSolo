import os
import custom_utils as cu
from firm_kern_comp import (
    compile_kernel,
)  # Or import functions directly if in same file

# 1. Define your target platform manually (bypassing cu.get_image_info)
kernel_version = "6.18"  # Version string (e.g., linux-3.10.14)
arch = "arm"  # Architecture: "arm" or "mips"
endianness = "little"  # "little" or "big"
cross_compiler = "arm-linux-gnueabi-"  # Cross-compiler prefix
ver_magicz = ["ARMv7", "p2v8"]  # Target vermagic flags

# 2. Specify the top-level CONFIG options or functions you need enabled
# KCRE will automatically resolve all parent/prerequisite dependencies for these!
conf_opts = ["CONFIG_SECURITY", "CONFIG_FS_POSIX_ACL"]
symbolz = ["generic_permission"]  # Target functions to satisfy

# 3. Dummy or empty fallback structures
image_id = "custom_build"
modulez = []  # List of .ko paths if you have any
module_options = []
guard_expr = []
ds_options = []
extraversion = ""
ds_recovery = 0
single_module_dir = ""
s_config = "yes"
openwrt = False
endianess = "little"

# 4. Invoke the compilation pipeline!
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

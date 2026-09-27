# Copyright (c) 2024 NVIDIA CORPORATION.
#   Licensed under the MIT license.

import os
import pathlib
import subprocess

from torch.utils import cpp_extension

"""
Setting this param to a list has a problem of generating different compilation commands (with diferent order of architectures) and leading to recompilation of fused kernels. 
Set it to empty stringo avoid recompilation and assign arch flags explicity in extra_cuda_cflags below
"""
os.environ["TORCH_CUDA_ARCH_LIST"] = ""


def load():
    # ============================================================
    # 快速通道：若已编译好的 .pyd 存在，直接加载（无需 MSVC/nvcc）。
    # 这样服务器每次启动都不再依赖编译环境（首次编译由 build_bigvgan.bat 完成）。
    # 若 .pyd 被删除，则回退到下方 JIT 编译（需要 nvcc + ninja + cl.exe）。
    # ============================================================
    srcpath_fast = pathlib.Path(__file__).parent.absolute()
    buildpath_fast = srcpath_fast / "build"
    pyd_path = buildpath_fast / "anti_alias_activation_cuda.pyd"
    if pyd_path.exists():
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "anti_alias_activation_cuda", str(pyd_path)
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception as _e:
            print(
                f"[BigVGAN] 预编译内核直接加载失败，回退到 JIT 编译: {_e}"
            )

    # Check if cuda 11 is installed for compute capability 8.0
    cc_flag = []
    _, bare_metal_major, _ = _get_cuda_bare_metal_version(cpp_extension.CUDA_HOME)
    if int(bare_metal_major) >= 11:
        cc_flag.append("-gencode")
        cc_flag.append("arch=compute_80,code=sm_80")

    # Build path
    srcpath = pathlib.Path(__file__).parent.absolute()
    buildpath = srcpath / "build"
    _create_build_dir(buildpath)

    # Helper function to build the kernels.
    def _cpp_extention_load_helper(name, sources, extra_cuda_flags):
        return cpp_extension.load(
            name=name,
            sources=sources,
            build_directory=buildpath,
            extra_cflags=[
                "-O3",
            ],
            extra_cuda_cflags=[
                "-O3",
                "-gencode",
                "arch=compute_70,code=sm_70",
                "--use_fast_math",
            ]
            + extra_cuda_flags
            + cc_flag,
            verbose=True,
        )

    extra_cuda_flags = [
        "-U__CUDA_NO_HALF_OPERATORS__",
        "-U__CUDA_NO_HALF_CONVERSIONS__",
        "--expt-relaxed-constexpr",
        "--expt-extended-lambda",
    ]

    sources = [
        srcpath / "anti_alias_activation.cpp",
        srcpath / "anti_alias_activation_cuda.cu",
    ]
    anti_alias_activation_cuda = _cpp_extention_load_helper(
        "anti_alias_activation_cuda", sources, extra_cuda_flags
    )

    return anti_alias_activation_cuda


def _get_cuda_bare_metal_version(cuda_dir):
    raw_output = subprocess.check_output(
        [cuda_dir + "/bin/nvcc", "-V"], universal_newlines=True
    )
    output = raw_output.split()
    release_idx = output.index("release") + 1
    release = output[release_idx].split(".")
    bare_metal_major = release[0]
    bare_metal_minor = release[1][0]

    return raw_output, bare_metal_major, bare_metal_minor


def _create_build_dir(buildpath):
    try:
        os.mkdir(buildpath)
    except OSError:
        if not os.path.isdir(buildpath):
            print(f"Creation of the build directory {buildpath} failed")

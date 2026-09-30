# Copyright (c) 2024 NVIDIA CORPORATION.
#   Licensed under the MIT license.

import os
import pathlib

from torch.utils import cpp_extension


def load():
    # 快速通道：若 build/ 下已有编译好的 anti_alias_activation_cuda.pyd，
    # 直接 import 复用，绕过 ninja/JIT 重编（本机无 cl.exe 常态不可用，免重编/联网）。
    # torch 2.5.1 的 cpp_extension.load 每次重写 build.ninja 会误判 .pyd 过期而回退重编，
    # 导致上层 try/except 只得回退纯 PyTorch（慢）。该 pyd 由同源码的 index2.0 内核复用而来，
    # 同一 torch 构建 + 同一卡(RTX 4060 Ti)，ABI 兼容。
    srcpath = pathlib.Path(__file__).parent.absolute()
    _prebuilt = srcpath / "build" / "anti_alias_activation_cuda.pyd"
    if _prebuilt.is_file():
        import torch.utils.cpp_extension as _ce
        print(f"[BigVGAN] 直接复用已编译 CUDA 内核: {_prebuilt}")
        return _ce._import_module_from_library(
            "anti_alias_activation_cuda", str(srcpath / "build"), True
        )

    # Build path
    srcpath = pathlib.Path(__file__).parent.absolute()
    buildpath = srcpath / "build"
    _create_build_dir(buildpath)

    # Helper function to build the kernels.
    def _cpp_extention_load_helper(name, sources, extra_cuda_flags):
        # PyTorch's generated -gencode flags honor TORCH_CUDA_ARCH_LIST, or
        # detect visible GPUs when the variable is not set.
        return cpp_extension.load(
            name=name,
            sources=sources,
            build_directory=buildpath,
            extra_cflags=[
                "-O3",
            ],
            extra_cuda_cflags=[
                "-O3",
                "--use_fast_math",
            ]
            + extra_cuda_flags,
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


def _create_build_dir(buildpath):
    try:
        os.mkdir(buildpath)
    except OSError:
        if not os.path.isdir(buildpath):
            print(f"Creation of the build directory {buildpath} failed")

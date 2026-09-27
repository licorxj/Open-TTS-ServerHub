# 专有依赖补丁包：IndexTTS-2.5 需要 transformers==4.52.1 / tokenizers==0.21.4 /
# huggingface_hub==0.36.2，与主环境 py312env（transformers 5.x）冲突。
# 将 packages/index25（扁平 site-packages）置于 sys.path 最前，覆盖同名包，
# 同时复用 py312env 的 torch 等大包。无论通过 API / WebUI / 测试脚本入口，
# 只要 import indextts 即自动优先调用补丁包。
import os
import sys

_PATCH_PKG = os.path.normpath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "packages", "index25")
)
if os.path.isdir(_PATCH_PKG) and _PATCH_PKG not in sys.path:
    sys.path.insert(0, _PATCH_PKG)

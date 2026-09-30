from typing import Optional, Tuple

import torch

from ..modeling_flash_attention_utils import _flash_attention_forward, flash_attn_supports_top_left_mask


_use_top_left_mask = flash_attn_supports_top_left_mask()


def flash_attention_forward(
    module: torch.nn.Module,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    attention_mask: Optional[torch.Tensor],
    dropout: float = 0.0,
    scaling: Optional[float] = None,
    sliding_window: Optional[int] = None,
    softcap: Optional[float] = None,
    **kwargs,
) -> Tuple[torch.Tensor, None]:
    # This is before the transpose
    seq_len = query.shape[2]

    # FA2 uses non-transposed inputs
    query = query.transpose(1, 2)
    key = key.transpose(1, 2)
    value = value.transpose(1, 2)
    # 记录原始 dtype：当模型整体为 fp32 时，下面会把输入临时转 fp16 跑 flash，
    # 计算完需把输出还原回原始 dtype，避免下游（如 GPT2 的 fp32 Conv1d c_proj）
    # 出现 Half/Float 不匹配。
    original_dtype = query.dtype

    # In PEFT, usually we cast the layer norms in float32 for training stability reasons
    # therefore the input hidden states gets silently casted in float32. Hence, we need
    # cast them back in the correct dtype just to be sure everything works as expected.
    # This might slowdown training & inference so it is recommended to not cast the LayerNorms
    # in fp32. (usually our RMSNorm modules handle it correctly)
    target_dtype = None
    if query.dtype == torch.float32:
        if torch.is_autocast_enabled():
            target_dtype = torch.get_autocast_gpu_dtype()
        # Handle the case where the model is quantized
        elif hasattr(module.config, "_pre_quantization_dtype"):
            target_dtype = module.config._pre_quantization_dtype
        else:
            # GPT2 等注意力层使用 nn.Conv1d（c_attn / c_proj）而非 nn.Linear，
            # 直接 next(...) 查找 Linear 会抛 StopIteration。回退到模块内任意
            # 带权重的参数 dtype（与权重实际计算 dtype 一致），仍找不到则兜底 fp16。
            try:
                target_dtype = next(
                    layer.weight.dtype
                    for layer in module.modules()
                    if isinstance(layer, torch.nn.Linear)
                )
            except StopIteration:
                # 模型整体为 fp32 且未开 autocast 时，GPT2 注意力层用 Conv1d
                # 不含 nn.Linear；flash-attn 只支持 fp16/bf16，故取权重 dtype，
                # 若仍是 fp32 则兜底为 fp16（与 HF 官方在
                # "without specifying a torch dtype" 时的预期用法一致）。
                for p in module.parameters():
                    target_dtype = p.dtype
                    break
                if target_dtype is None or target_dtype == torch.float32:
                    target_dtype = torch.float16

    # FA2 always relies on the value set in the module, so remove it if present in kwargs to avoid passing it twice
    kwargs.pop("is_causal", None)

    attn_output = _flash_attention_forward(
        query,
        key,
        value,
        attention_mask,
        query_length=seq_len,
        is_causal=module.is_causal,
        dropout=dropout,
        softmax_scale=scaling,
        sliding_window=sliding_window,
        softcap=softcap,
        use_top_left_mask=_use_top_left_mask,
        target_dtype=target_dtype,
        **kwargs,
    )

    # flash-attn 仅支持 fp16/bf16；若上面将 fp32 输入临时转 fp16 计算，这里把
    # 输出还原回原始 dtype，保证下游层（如 GPT2 的 fp32 c_proj）dtype 一致。
    if attn_output.dtype != original_dtype:
        attn_output = attn_output.to(original_dtype)

    return attn_output, None

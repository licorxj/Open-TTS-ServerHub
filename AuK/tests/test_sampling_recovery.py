"""CPU regression tests of the real sampler; no weights or provider calls."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch
from torch import nn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from auk.model.cfm_edit import CFMEdit


class CachedTransformer(nn.Module):
    dim = 2

    def __init__(self):
        super().__init__()
        self.anchor = nn.Parameter(torch.zeros(()))
        self.text_cond = None
        self.text_uncond = None
        self.fail = False
        self.reused = 0

    def clear_cache(self):
        self.text_cond = self.text_uncond = None

    def forward(self, x, text, c_mask, **kwargs):
        if self.text_cond is None:
            self.text_cond = text.clone()
            self.text_uncond = torch.zeros_like(text)
        else:
            self.reused += 1
        if self.text_cond.shape[:2] != c_mask.shape:
            raise RuntimeError("stale conditioning")
        if self.fail:
            raise RuntimeError("injected ODE failure")
        return torch.zeros_like(x).repeat(2, 1, 1)


@pytest.fixture
def model():
    encoder = nn.Linear(2, 2)
    encoder.config = SimpleNamespace(text_config=SimpleNamespace(num_hidden_layers=1))
    result = CFMEdit(CachedTransformer(), encoder, None, num_channels=2)
    result.encode_text = lambda text, device: (
        text, torch.ones(text.shape[:2], dtype=torch.bool, device=device)
    )
    return result


def sample(model, length):
    return model.sample(
        torch.zeros(1, 2, 2), torch.ones(1, length, 2),
        duration=5, steps=2, cfg_strength=2, seed=42,
    )


def test_success_failure_then_different_length_recovers(model):
    sample(model, 3)
    assert model.transformer.reused > 0  # Cache still works within a request.
    model.transformer.fail = True
    with pytest.raises(RuntimeError, match="injected ODE failure"):
        sample(model, 7)
    assert model.transformer.text_cond is None
    assert model.transformer.text_uncond is None
    model.transformer.fail = False
    sample(model, 11)
    assert model.transformer.text_cond is None


def test_entry_discards_preexisting_stale_conditioning(model):
    model.transformer.text_cond = torch.ones(1, 99, 2)
    model.transformer.text_uncond = torch.zeros(1, 99, 2)
    sample(model, 3)
    assert model.transformer.text_cond is None


def test_encoder_mask_mismatch_is_reported_before_ode(model):
    model.encode_text = lambda text, device: (text, torch.ones(1, 4, dtype=torch.bool))
    with pytest.raises(RuntimeError, match=r"before sampling: text=\(1, 3, 2\), mask=\(1, 4\)"):
        sample(model, 3)
    assert model.transformer.text_cond is None

# Copyright (c) 2023, Tri Dao.

import math

import pytest
import torch
import torch.nn.functional as F
from einops import rearrange
from flash_attn.layers.rotary import RotaryEmbedding, apply_rotary_emb_func, apply_rotary_emb_qkv_
from flash_attn.bert_padding import unpad_input, pad_input


# NeoX-style rotary embedding
@pytest.mark.parametrize("seqlen_offset", [0, 128, 711, 2047])
@pytest.mark.parametrize("rotary_emb_fraction", [0.0, 0.5, 1.0])
@pytest.mark.parametrize("rotary_type", ["gpt-neox", "llama"])
def test_rotary_neox(rotary_emb_fraction, seqlen_offset, rotary_type):
    if rotary_type == "gpt-neox":
        from transformers.models.gpt_neox.modeling_gpt_neox import (
            GPTNeoXRotaryEmbedding as RotaryEmbeddingHF,
        )
        from transformers.models.gpt_neox.modeling_gpt_neox import (
            apply_rotary_pos_emb as apply_rotary_pos_emb_hf,
        )
    elif rotary_type == "llama":
        from transformers.models.llama.modeling_llama import (
            LlamaRotaryEmbedding as RotaryEmbeddingHF,
        )
        from transformers.models.llama.modeling_llama import (
            apply_rotary_pos_emb as apply_rotary_pos_emb_hf,
        )

    device = "cuda"
    dtype = torch.float16
    rtol, atol = (1e-3, 5e-3)
    # set seed
    torch.random.manual_seed(0)
    batch_size = 8
    seqlen_total = 2048
    seqlen = seqlen_total - seqlen_offset
    position_ids = torch.arange(seqlen_offset, seqlen_total, device=device, dtype=torch.long)
    nheads = 16
    headdim = 128
    rotary_dim = int(headdim * rotary_emb_fraction)
    qkv = torch.randn(
        batch_size, seqlen, 3, nheads, headdim, device=device, dtype=dtype, requires_grad=True
    )
    qkv_og = qkv.clone().detach()  # Our implementation modifies qkv inplace
    rotary = RotaryEmbedding(rotary_dim, device=device)
    rotary_neox = RotaryEmbeddingHF(rotary_dim, seqlen_total, device=device)
    # Doesn't matter what tensor we pass in, rotary_neox only uses the device of the tensor
    cos_neox, sin_neox = rotary_neox(qkv, seq_len=seqlen_total)
    cos_neox, sin_neox = cos_neox.to(dtype=dtype), sin_neox.to(dtype=dtype)
    q_pt = (
        rearrange(qkv[:, :, 0, :, :rotary_dim], "b s h d -> b h s d")
        .detach()
        .clone()
        .requires_grad_(True)
    )
    k_pt = (
        rearrange(qkv[:, :, 1, :, :rotary_dim], "b s h d -> b h s d")
        .detach()
        .clone()
        .requires_grad_(True)
    )
    q_neox, k_neox = apply_rotary_pos_emb_hf(
        q_pt, k_pt, cos_neox, sin_neox, position_ids, unsqueeze_dim=0
    )
    out = rotary(qkv, seqlen_offset=seqlen_offset)
    assert torch.allclose(
        rotary._cos_cached, cos_neox[..., : rotary_dim // 2].to(dtype=dtype), rtol=rtol, atol=atol
    )
    assert torch.allclose(
        rotary._sin_cached, sin_neox[..., : rotary_dim // 2].to(dtype=dtype), rtol=rtol, atol=atol
    )
    assert torch.allclose(
        rearrange(q_neox, "b h s d -> b s h d"), out[:, :, 0, :, :rotary_dim], rtol=rtol, atol=atol
    )
    assert torch.allclose(
        rearrange(k_neox, "b h s d -> b s h d"), out[:, :, 1, :, :rotary_dim], rtol=rtol, atol=atol
    )
    assert torch.equal(out[:, :, 0:2, :, rotary_dim:], qkv_og[:, :, 0:2, :, rotary_dim:])
    assert torch.equal(out[:, :, 2], qkv_og[:, :, 2])

    g = torch.randn_like(out)
    g_og = g.clone().detach()  # Our implementation modifies g inplace
    out.backward(g)
    q_neox.backward(rearrange(g_og[:, :, 0, :, :rotary_dim], "b s h d -> b h s d"))
    k_neox.backward(rearrange(g_og[:, :, 1, :, :rotary_dim], "b s h d -> b h s d"))
    assert torch.allclose(
        rearrange(q_pt.grad, "b h s d -> b s h d"),
        qkv.grad[:, :, 0, :, :rotary_dim],
        rtol=rtol,
        atol=atol,
    )
    assert torch.allclose(
        rearrange(k_pt.grad, "b h s d -> b s h d"),
        qkv.grad[:, :, 1, :, :rotary_dim],
        rtol=rtol,
        atol=atol,
    )
    assert torch.equal(qkv.grad[:, :, 0:2, :, rotary_dim:], g_og[:, :, 0:2, :, rotary_dim:])
    assert torch.equal(qkv.grad[:, :, 2], g_og[:, :, 2])


# GPT-J-style rotary embedding
@pytest.mark.parametrize("seqlen_offset", [0, 711])
@pytest.mark.parametrize("rotary_emb_fraction", [0.5, 1.0])
def test_rotary_gptj_interleaved(rotary_emb_fraction, seqlen_offset):
    from transformers.models.gptj.modeling_gptj import (
        apply_rotary_pos_emb as apply_rotary_pos_emb_gptj,
    )
    from transformers.models.gptj.modeling_gptj import create_sinusoidal_positions

    device = "cuda"
    dtype = torch.float16
    rtol, atol = (1e-3, 5e-3)
    # set seed
    torch.random.manual_seed(0)
    batch_size = 8
    seqlen_total = 2048
    seqlen = seqlen_total - seqlen_offset
    nheads = 16
    headdim = 128
    rotary_dim = int(headdim * rotary_emb_fraction)
    qkv = torch.randn(
        batch_size, seqlen, 3, nheads, headdim, device=device, dtype=dtype, requires_grad=True
    )
    qkv_og = qkv.clone().detach()  # Our implementation modifies qkv inplace
    rotary = RotaryEmbedding(rotary_dim, interleaved=True, device=device)
    position_ids = torch.arange(
        seqlen_offset, seqlen_total, device=device, dtype=torch.long
    ).unsqueeze(0)
    embed_positions = (
        create_sinusoidal_positions(seqlen_total, rotary_dim)
        .repeat(position_ids.shape[0], 1, 1)
        .to(device=device, dtype=dtype)
    )
    repeated_position_ids = position_ids.unsqueeze(-1).repeat(1, 1, embed_positions.shape[-1])
    sincos = torch.gather(embed_positions, 1, repeated_position_ids)
    sin_gptj, cos_gptj = torch.split(sincos, sincos.shape[-1] // 2, dim=-1)
    q_pt = qkv[:, :, 0, :, :rotary_dim].detach().clone().requires_grad_(True)
    k_pt = qkv[:, :, 1, :, :rotary_dim].detach().clone().requires_grad_(True)
    q_gptj = apply_rotary_pos_emb_gptj(q_pt, sin_gptj, cos_gptj)
    k_gptj = apply_rotary_pos_emb_gptj(k_pt, sin_gptj, cos_gptj)

    out = rotary(qkv, seqlen_offset=seqlen_offset)
    assert torch.allclose(rotary._cos_cached[seqlen_offset:], cos_gptj, rtol=rtol, atol=atol)
    assert torch.allclose(rotary._sin_cached[seqlen_offset:], sin_gptj, rtol=rtol, atol=atol)
    assert torch.allclose(q_gptj, out[:, :, 0, :, :rotary_dim], rtol=rtol, atol=atol)
    assert torch.allclose(k_gptj, out[:, :, 1, :, :rotary_dim], rtol=rtol, atol=atol)
    assert torch.equal(out[:, :, 0:2, :, rotary_dim:], qkv_og[:, :, 0:2, :, rotary_dim:])
    assert torch.equal(out[:, :, 2], qkv_og[:, :, 2])

    g = torch.randn_like(out)
    g_og = g.clone().detach()  # Our implementation modifies g inplace
    out.backward(g)
    q_gptj.backward(g_og[:, :, 0, :, :rotary_dim])
    k_gptj.backward(g_og[:, :, 1, :, :rotary_dim])
    assert torch.allclose(q_pt.grad, qkv.grad[:, :, 0, :, :rotary_dim], rtol=rtol, atol=atol)
    assert torch.allclose(k_pt.grad, qkv.grad[:, :, 1, :, :rotary_dim], rtol=rtol, atol=atol)
    assert torch.equal(qkv.grad[:, :, 0:2, :, rotary_dim:], g_og[:, :, 0:2, :, rotary_dim:])
    assert torch.equal(qkv.grad[:, :, 2], g_og[:, :, 2])


@pytest.mark.parametrize("max_seqlen_offset", [0, 10, 811])
# @pytest.mark.parametrize("max_seqlen_offset", [0])
@pytest.mark.parametrize("max_seqlen_qkv", [10, 128, 204])
# @pytest.mark.parametrize("max_seqlen_qkv", [10])
@pytest.mark.parametrize("rotary_emb_fraction", [0.5, 1.0])
# @pytest.mark.parametrize("rotary_emb_fraction", [1.0])
@pytest.mark.parametrize("mha_type", ["mha", "gqa", "mqa"])
# @pytest.mark.parametrize("mha_type", ["mha"])
@pytest.mark.parametrize("nheads", [16, 32])
# @pytest.mark.parametrize("nheads", [16])
def test_rotary_varlen(
    rotary_emb_fraction,
    max_seqlen_qkv,
    max_seqlen_offset,
    mha_type,
    nheads,
):
    device = "cuda"
    dtype = torch.float16
    rtol, atol = (1e-3, 5e-3)
    # set seed
    torch.random.manual_seed(0)
    batch_size = 8
    headdim = 128
    rotary_dim = int(headdim * rotary_emb_fraction)
    if max_seqlen_offset > 0:
        seqlen_offset = torch.randint(
            0, max_seqlen_offset, (batch_size,), device=device, dtype=torch.long
        )
    else:
        seqlen_offset = torch.zeros(batch_size, device=device, dtype=torch.long)
    seqlens_q = torch.randint(0, max_seqlen_qkv, (batch_size,), device=device, dtype=torch.long)
    attention_mask = torch.arange(max_seqlen_qkv, device=device, dtype=torch.long).unsqueeze(
        0
    ) < seqlens_q.unsqueeze(1)
    if mha_type == "mha":
        qkv = torch.randn(
            batch_size,
            max_seqlen_qkv,
            3,
            nheads,
            headdim,
            device=device,
            dtype=dtype,
            requires_grad=True,
        )
        q, kv = qkv[:, :, 0], qkv[:, :, 1:]
    elif mha_type == "mqa":
        qkv = torch.randn(
            batch_size,
            max_seqlen_qkv,
            nheads + 2,
            headdim,
            device=device,
            dtype=dtype,
            requires_grad=True,
        )
        q, kv = qkv[:, :, :nheads], qkv[:, :, nheads:].unsqueeze(-2)
    else:
        qkv = torch.randn(
            batch_size,
            max_seqlen_qkv,
            nheads + 8,
            headdim,
            device=device,
            dtype=dtype,
            requires_grad=True,
        )
        q, kv = qkv[:, :, :nheads], qkv[:, :, nheads:]
        kv = kv.view(batch_size, max_seqlen_qkv, 2, 4, headdim)
    q_unpad, indices_q, cu_seqlens_q, max_seqlen_q = unpad_input(q, attention_mask)
    kv_unpad, *_ = unpad_input(kv, attention_mask)
    kv_unpad_original = kv_unpad.clone().detach()
    q_unpad = q_unpad.clone().detach().requires_grad_()
    kv_unpad = kv_unpad.clone().detach().requires_grad_()
    rotary = RotaryEmbedding(rotary_dim, device=device)
    qout1_unpad, kvout1_unpad = rotary(
        q_unpad,
        kv_unpad,
        seqlen_offset=seqlen_offset,
        cu_seqlens=cu_seqlens_q,
        max_seqlen=max_seqlen_q,
    )
    q2, kv2 = q.clone().detach().requires_grad_(), kv.clone().detach().requires_grad_()
    qout2, kvout2 = rotary(q2, kv2, seqlen_offset=seqlen_offset)
    qout2_unpad = unpad_input(qout2, attention_mask)[0]
    kvout2_unpad = unpad_input(kvout2, attention_mask)[0]
    assert torch.allclose(qout1_unpad, qout2_unpad)
    assert torch.allclose(kvout1_unpad, kvout2_unpad)
    assert torch.allclose(kvout2_unpad[:, 1], kv_unpad_original[:, 1])

    g = torch.randn_like(qout1_unpad)
    gg = g.clone().detach()
    qout1_unpad.backward(g)
    qout2_unpad.backward(gg)
    g2 = unpad_input(q2.grad, attention_mask)[0]
    assert torch.allclose(g2, q_unpad.grad)

    g = torch.randn_like(kvout1_unpad)
    gg = g.clone().detach()
    kvout1_unpad.backward(g)
    kvout2_unpad.backward(gg)
    g2 = unpad_input(kv2.grad, attention_mask)[0]
    assert torch.allclose(g2, kv_unpad.grad)


@pytest.mark.parametrize("seqlen_offset", [0, 128, 711, 2047])
@pytest.mark.parametrize("rotary_emb_fraction", [0.5, 1.0])
@pytest.mark.parametrize("scaling_type", ["linear", "dynamic"])
@pytest.mark.parametrize("scaling_factor", [1.0, 0.5, 2.0])
def test_rotary_scaling(rotary_emb_fraction, seqlen_offset, scaling_type, scaling_factor):
    from transformers.models.llama.modeling_llama import (
        LlamaLinearScalingRotaryEmbedding,
        LlamaDynamicNTKScalingRotaryEmbedding,
    )
    from transformers.models.llama.modeling_llama import (
        apply_rotary_pos_emb as apply_rotary_pos_emb_hf,
    )

    if scaling_type == "linear":
        RotaryEmbeddingHF = LlamaLinearScalingRotaryEmbedding
    elif scaling_type == "dynamic":
        RotaryEmbeddingHF = LlamaDynamicNTKScalingRotaryEmbedding

    device = "cuda"
    dtype = torch.float16
    rtol, atol = (1e-3, 5e-3)
    # set seed
    torch.random.manual_seed(0)
    batch_size = 8
    seqlen_total = 2048
    seqlen = seqlen_total - seqlen_offset
    position_ids = torch.arange(seqlen_offset, seqlen_total, device=device, dtype=torch.long)
    nheads = 16
    headdim = 128
    rotary_dim = int(headdim * rotary_emb_fraction)
    qkv = torch.randn(
        batch_size, seqlen, 3, nheads, headdim, device=device, dtype=dtype, requires_grad=True
    )
    qkv_og = qkv.clone().detach()  # Our implementation modifies qkv inplace
    rotary = RotaryEmbedding(
        rotary_dim, scale_factor=scaling_factor, scale_type=scaling_type, device=device
    )
    rotary_neox = RotaryEmbeddingHF(
        rotary_dim, seqlen_total, scaling_factor=scaling_factor, device=device
    )
    # Doesn't matter what tensor we pass in, rotary_neox only uses the device of the tensor
    cos_neox, sin_neox = rotary_neox(qkv, seq_len=seqlen_total)
    cos_neox, sin_neox = cos_neox.to(dtype=dtype), sin_neox.to(dtype=dtype)
    q_pt = (
        rearrange(qkv[:, :, 0, :, :rotary_dim], "b s h d -> b h s d")
        .detach()
        .clone()
        .requires_grad_(True)
    )
    k_pt = (
        rearrange(qkv[:, :, 1, :, :rotary_dim], "b s h d -> b h s d")
        .detach()
        .clone()
        .requires_grad_(True)
    )
    q_neox, k_neox = apply_rotary_pos_emb_hf(
        q_pt, k_pt, cos_neox, sin_neox, position_ids, unsqueeze_dim=0
    )
    out = rotary(qkv, seqlen_offset=seqlen_offset)
    assert torch.allclose(
        rotary._cos_cached, cos_neox[..., : rotary_dim // 2].to(dtype=dtype), rtol=rtol, atol=atol
    )
    assert torch.allclose(
        rotary._sin_cached, sin_neox[..., : rotary_dim // 2].to(dtype=dtype), rtol=rtol, atol=atol
    )
    assert torch.allclose(
        rearrange(q_neox, "b h s d -> b s h d"), out[:, :, 0, :, :rotary_dim], rtol=rtol, atol=atol
    )
    assert torch.allclose(
        rearrange(k_neox, "b h s d -> b s h d"), out[:, :, 1, :, :rotary_dim], rtol=rtol, atol=atol
    )
    assert torch.equal(out[:, :, 0:2, :, rotary_dim:], qkv_og[:, :, 0:2, :, rotary_dim:])
    assert torch.equal(out[:, :, 2], qkv_og[:, :, 2])

    g = torch.randn_like(out)
    g_og = g.clone().detach()  # Our implementation modifies g inplace
    out.backward(g)
    q_neox.backward(rearrange(g_og[:, :, 0, :, :rotary_dim], "b s h d -> b h s d"))
    k_neox.backward(rearrange(g_og[:, :, 1, :, :rotary_dim], "b s h d -> b h s d"))
    assert torch.allclose(
        rearrange(q_pt.grad, "b h s d -> b s h d"),
        qkv.grad[:, :, 0, :, :rotary_dim],
        rtol=rtol,
        atol=atol,
    )
    assert torch.allclose(
        rearrange(k_pt.grad, "b h s d -> b s h d"),
        qkv.grad[:, :, 1, :, :rotary_dim],
        rtol=rtol,
        atol=atol,
    )
    assert torch.equal(qkv.grad[:, :, 0:2, :, rotary_dim:], g_og[:, :, 0:2, :, rotary_dim:])
    assert torch.equal(qkv.grad[:, :, 2], g_og[:, :, 2])

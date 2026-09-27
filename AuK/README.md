---
domain: audio
tags:
- speech-generation
- speech-editing
- text-to-speech
models:
- Tencent-Hunyuan/AuK
- Tencent-Hunyuan/AuK-Flash
- Qwen/Qwen2.5-Omni-3B
deployspec:
  entry_file: app.py
license: MIT License
---

# AuK Studio

This ModelScope Studio follows the public
[Tencent-Hunyuan/AuK](https://github.com/Tencent-Hunyuan/AuK) source at commit
`e3828bdac1712bbf7bcb3a2a01eec6fa5168fe32`. The runtime files under `src/` and
every example WAV referenced by that revision are copied unchanged. ModelScope
adaptations remain in `app.py`.

The interface serves both official variants:

- **AuK-Flash** uses its fixed 4-step, CFG-free distilled recipe.
- **AuK Base** exposes NFE and classifier-free guidance controls.

Both variants share one Qwen2.5-Omni-3B text/audio encoder. The application also
compares the two published VAE files and configurations, sharing one VAE
instance only when both match. The interface adds task-grouped examples,
Prompt Enhancer output fields, cloud/local ASR, and automatic duration
estimation.

## Studio secrets and variables

The following names are read directly from the Studio process environment.
The application reports missing names only and redacts configured values from
user-facing errors.

| Name | Purpose | Requirement |
| --- | --- | --- |
| `MODELSCOPE_API_KEY` | Read private ModelScope model repositories | Required while AuK model repositories are private |
| `LLM_API_KEY` | Prompt Enhancer LLM credential | Required when Prompt Enhancer is enabled |
| `LLM_BASE_URL` | OpenAI-compatible LLM API base URL | Required when Prompt Enhancer is enabled |
| `LLM_MODEL_NAME` | Exact provider model name | Required when Prompt Enhancer is enabled |
| `TENCENTCLOUD_SECRET_ID` | Tencent Cloud recording ASR credential ID | Needed for cloud ASR |
| `TENCENTCLOUD_SECRET_KEY` | Tencent Cloud recording ASR credential key | Needed for cloud ASR |
| `ASR_ENGINE_MODEL_TYPE` | Tencent Cloud recognition engine | Optional; upstream default is `16k_zh_en` |

Prompt Enhancer is optional and enabled by default. With it enabled, duration
`0` requests automatic estimation and a positive value overrides that estimate.
With it disabled, a finite duration greater than `0` is required. Cloud ASR
falls back to upstream SenseVoiceSmall on CPU when needed.

Public access can consume the configured LLM and ASR quotas. Provider-side
spending limits and rate limits should be configured before making the Studio
public.

## ModelScope repositories

The Studio resolves all model assets through the ModelScope SDK:

- `Tencent-Hunyuan/AuK`
- `Tencent-Hunyuan/AuK-Flash`
- `Qwen/Qwen2.5-Omni-3B`

Snapshots are cached under `/mnt/workspace/.cache/modelscope` so restarts can
reuse downloaded files. ModelScope xGPU is attached to the whole process; both
model variants stay resident on CUDA and requests are serialized with
concurrency `1` and queue capacity `16`.

The Studio's TorchAudio runtime delegates newer I/O paths to TorchCodec.
`SpaceAudioIO` in `app.py` preserves the upstream local-file contract through
SoundFile for WAV-compatible formats, including PCM16 output, while retaining
TorchAudio transforms for resampling.

Optional deployment variables:

- `AUK_REVISION`: AuK revision, default `master`
- `AUK_FLASH_REVISION`: AuK-Flash revision, default `master`
- `AUK_MODELSCOPE_CACHE`: snapshot cache directory
- `AUK_MODELSCOPE_CREDENTIALS`: ephemeral SDK credential directory
- `MODELSCOPE_ENDPOINT`: ModelScope site endpoint, default `https://modelscope.cn`

## Clone

```bash
git clone https://www.modelscope.cn/studios/Tencent-Hunyuan/AuK.git
```

---
name: omnivoice-tts-api
description: Call the local OmniVoice and VoxCPM FastAPI TTS services for health checks, voice cloning, ultimate cloning, voice design, task polling, waiting for completion, and audio download. Use when the user mentions OmniVoice, VoxCPM, local TTS APIs, voice cloning, ultimate cloning, voice design, task status, audio download, or asks the agent to call the project's 8853 or 8854 services directly.
---

# OmniVoice TTS API

Use this skill to operate the two local project TTS services:

- `vox` -> `http://localhost:8854`
- `omni` -> `http://localhost:8853`

Assume the project root is `y:\Omnivoice`.

## Prerequisites

1. Run health checks first:

```powershell
python C:\Users\Administrator\.agents\skills\omnivoice-tts-api\scripts\invoke_tts_api.py health --service vox
python C:\Users\Administrator\.agents\skills\omnivoice-tts-api\scripts\invoke_tts_api.py health --service omni
```

2. If a service is not running, ask the user to start the matching API launcher batch file in the project root:

- the VoxCPM API launcher `.bat`
- the OmniVoice API launcher `.bat`

3. Prefer `ref_audio_path` for same-machine calls to avoid multipart upload overhead.

## Endpoint Selection

- `vox clone` -> `/api/v1/voice/clone`
- `vox ultimate-clone` -> `/api/v1/voice/ultimate_clone`
- `vox design` -> `/api/v1/voice/design`
- `omni clone` -> `/api/v1/voice/clone`
- `omni design` -> `/api/v1/voice/design`
- `dot clone` -> `/api/v1/voice/clone`
- `index clone` -> `/api/v1/voice/clone`

Do not call `ultimate-clone` for `omni`.

## Standard Workflow

1. Confirm the service reports `healthy`.
2. Submit `clone`, `ultimate-clone`, or `design`.
3. Read `task_id` from the response.
4. Poll with `wait` or `status`.
5. If the task is `completed`, prefer returning `output_path`. Use `download` only when the user needs the WAV file copied out.

## Common Commands

### VoxCPM Clone

```powershell
python C:\Users\Administrator\.agents\skills\omnivoice-tts-api\scripts\invoke_tts_api.py clone `
  --service vox `
  --text "Test text" `
  --ref-audio-path "Y:\Omnivoice\VoxCPM\examples\example.wav" `
  --language zh `
  --instruct "gentle" `
  --speed 0.75
```

### VoxCPM Ultimate Clone

```powershell
python C:\Users\Administrator\.agents\skills\omnivoice-tts-api\scripts\invoke_tts_api.py ultimate-clone `
  --service vox `
  --text "Continue the speech" `
  --ref-audio-path "Y:\Omnivoice\VoxCPM\examples\example.wav" `
  --prompt-text "Reference transcript"
```

> When `--speed` is passed to ultimate-clone, it auto-switches to clone mode with speed instruct.

### OmniVoice Clone

```powershell
python C:\Users\Administrator\.agents\skills\omnivoice-tts-api\scripts\invoke_tts_api.py clone `
  --service omni `
  --text "Test text" `
  --ref-audio-path "Y:\Omnivoice\VoxCPM\examples\example.wav" `
  --language zh `
  --speed 1.0 `
  --num-steps 32
```

### Voice Design

```powershell
python C:\Users\Administrator\.agents\skills\omnivoice-tts-api\scripts\invoke_tts_api.py design `
  --service omni `
  --text "Test text" `
  --instruct "female, young adult, moderate pitch"
```

### DotTTS Clone

```powershell
python C:\Users\Administrator\.agents\skills\omnivoice-tts-api\scripts\invoke_tts_api.py clone `
  --service dot `
  --text "Test text" `
  --ref-audio-path "Y:\Omnivoice\VoxCPM\examples\example.wav" `
  --language zh `
  --instruct "温柔的语气说"
```

### IndexTTS Clone

```powershell
python C:\Users\Administrator\.agents\skills\omnivoice-tts-api\scripts\invoke_tts_api.py clone `
  --service index `
  --text "Test text" `
  --ref-audio-path "Y:\Omnivoice\VoxCPM\examples\example.wav" `
  --instruct "害怕、紧张的语气"
```

### Wait For Completion

```powershell
python C:\Users\Administrator\.agents\skills\omnivoice-tts-api\scripts\invoke_tts_api.py wait `
  --service vox `
  --task-id "<task_id>"
```

## Parameter Differences

- `vox clone` supports `instruct`, `speed`, `cfg_value`, `inference_timesteps`, `normalize`, `denoise`
- `vox ultimate-clone` adds `prompt_text`, `speed` (speed triggers auto-switch to clone mode)
- `vox design` supports `instruct`, `speed`, `cfg_value`, `inference_timesteps`, `normalize`
- `omni clone` supports `ref_text`, `num_steps`, `guidance_scale`, `speed`, `duration`, `denoise`, `max_workers`
- `omni design` supports `num_steps`, `guidance_scale`, `speed`, `duration`, `denoise`
- `dot clone` supports `prompt_text`, `instruct`, `num_steps`, `guidance_scale`, `speaker_scale`, `normalize_text`, `max_generate_length`, `seed`
- `index clone` supports `instruct`, `emo_control_method`, `emo_alpha`, `emo_vector`, `emo_text`, `num_beams`, `temperature`, `top_p`, `top_k`, `repetition_penalty`, `length_penalty`, `max_mel_tokens`

Read `references/api_reference.md` for the parameter tables and response fields.

## Notes

- `wait` prints the final task JSON when the task exits.
- `download` is optional and should be used only when a local WAV file is needed.
- The OmniVoice API server already handles the Windows torchcodec fallback internally.

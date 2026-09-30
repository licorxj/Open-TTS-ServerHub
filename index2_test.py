import os, time, requests

URI = "http://localhost:8855"
REF = r"Y:\LcTTSHub\VoxCPM\examples\reference_speaker.wav"
TEXT = "你好，我是孔子，今天给大家讲一个古老的故事，希望你们喜欢。"

if not os.path.exists(REF):
    raise SystemExit(f"参考音频缺失: {REF}")

t0 = time.time()
r = requests.post(f"{URI}/api/v1/voice/clone", data={"text": TEXT, "spk_audio_path": REF}, timeout=30)
print("create:", r.status_code, r.text[:200])
r.raise_for_status()
tid = r.json()["task_id"]

status = None
for i in range(120):
    time.sleep(3)
    s = requests.get(f"{URI}/api/v1/tasks/{tid}", timeout=20).json()
    status = s["status"]
    print(f"[{i}] status={status} progress={s.get('progress')} msg={s.get('message')}")
    if status in ("completed", "failed"):
        break

print("总耗时 %.1fs" % (time.time() - t0))
if status == "completed":
    out = s.get("output_path")
    print("output_path:", out)
    if out and os.path.exists(out):
        import soundfile as sf
        info = sf.info(out)
        print("wav: %.2fs %dHz ch=%d" % (info.frames / info.samplerate, info.samplerate, info.channels))
    print("rtf=", s.get("rtf"), "inference_time=", s.get("inference_time"), "audio_duration=", s.get("audio_duration"))
    dl = requests.get(f"{URI}/api/v1/voice/download/{tid}", timeout=30)
    print("download:", dl.status_code, "bytes=", len(dl.content))
else:
    print("FAILED error=", s.get("error"))

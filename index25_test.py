import os, time, requests, json

URI = "http://localhost:8858"
REF = r"Y:\LcTTSHub\VoxCPM\examples\reference_speaker.wav"
TEXT = "你好，我是孔子，今天给大家讲一个古老的故事，希望你们喜欢。"
OUT = r"Y:\LcTTSHub\outputs\index25_test.wav"

if not os.path.exists(REF):
    raise SystemExit(f"参考音频缺失: {REF}")

t0 = time.time()
r = requests.post(f"{URI}/api/tts", json={
    "input_text": TEXT,
    "speaker_audio_path": REF,
    "lang": "zh",
    "output_path": OUT,
}, timeout=600)
print("create:", r.status_code)
print("resp keys:", list(r.json().keys()))
print("resp:", json.dumps(r.json(), ensure_ascii=False)[:300])
print("总耗时 %.1fs" % (time.time() - t0))
if os.path.exists(OUT):
    import soundfile as sf
    info = sf.info(OUT)
    print("wav: %.2fs %dHz ch=%d" % (info.frames / info.samplerate, info.samplerate, info.channels))
else:
    print("未生成输出文件")

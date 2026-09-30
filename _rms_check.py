import os, sys, time, requests, numpy as np, soundfile as sf

URI = "http://localhost:8858"
REF = sys.argv[1] if len(sys.argv) > 1 else r"Y:\LcTTSHub\VoxCPM\examples\reference_speaker.wav"
TEXT = sys.argv[2] if len(sys.argv) > 2 else "你好，我是孔子，今天给大家讲一个古老的故事，希望你们喜欢。"
OUT = r"Y:\LcTTSHub\outputs\_rms_check.wav"

if os.path.exists(OUT):
    os.remove(OUT)

t0 = time.time()
r = requests.post(f"{URI}/api/tts", json={
    "input_text": TEXT,
    "speaker_audio_path": REF,
    "lang": "zh",
    "output_path": OUT,
    "verbose": True,
}, timeout=900)
print("status:", r.status_code)
print("resp:", r.text[:400])
print("耗时 %.1fs" % (time.time() - t0))

if os.path.exists(OUT):
    data, sr = sf.read(OUT, dtype="float32")
    print("sr=%d frames=%d dur=%.2fs" % (sr, len(data), len(data) / sr))
    print("RMS=%.6f  peak=%.6f  nonzero_ratio=%.4f" % (
        float(np.sqrt(np.mean(data ** 2))),
        float(np.max(np.abs(data))),
        float(np.mean(np.abs(data) > 1e-5)),
    ))
else:
    print("未生成输出文件")

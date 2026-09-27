import requests, time, os

URI = "http://localhost:8854"

r = requests.post(f"{URI}/api/v1/voice/design", data={
    "text": "今天天气真好，我们一起去公园散步，享受这美好的午后时光。",
    "instruct": "A warm, gentle female narrator with a clear and calm voice",
    "language": "zh",
    "inference_timesteps": 10,
})
print("create:", r.status_code, r.text[:200])
task = r.json()
tid = task["task_id"]
print("task_id:", tid, "status:", task.get("status"))

s = task
for i in range(40):
    time.sleep(3)
    s = requests.get(f"{URI}/api/v1/tasks/{tid}").json()
    print(f"[{i}] status={s['status']} progress={s.get('progress')} msg={s.get('message')}")
    if s["status"] in ("completed", "failed"):
        break

if s["status"] == "completed":
    out = r"Y:\LcTTSHub\voxcpm_test.wav"
    audio = requests.get(f"{URI}/api/v1/voice/download/{tid}")
    with open(out, "wb") as f:
        f.write(audio.content)
    print(f"downloaded: {out} sizeKB={len(audio.content)/1024:.1f}")
else:
    print("NOT COMPLETED:", s)

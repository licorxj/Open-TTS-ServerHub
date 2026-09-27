"""并发测试：同时提交 2 个 Confucius4 克隆请求，验证 CPU提取/GPU推理 流水线重叠。
运行：py312env/python.exe _concurrent_test.py
观察：服务器端日志应出现「任务B 特征提取完成」早于「任务A 完成」，即二者重叠。
"""
import glob, time, threading
import requests

BASE = "http://localhost:8857"
# 取一个本地参考音频
cands = glob.glob(r"y:\LcTTSHub\uploads\*.wav") + glob.glob(r"y:\LcTTSHub\voice\*.wav")
ref = cands[0] if cands else None
if not ref:
    print("未找到参考音频"); raise SystemExit(1)
print(f"使用参考音频: {ref}")

TEXT = "这是一次并发流水线测试，用于验证 CPU 特征提取与 GPU 推理是否真正并行。"

def send(tag):
    t0 = time.time()
    print(f"[{tag}] 提交请求 @ {t0:.2f}")
    try:
        r = requests.post(f"{BASE}/api/v1/voice/clone",
                          data={"text": TEXT, "lang": "zh", "ref_audio_path": ref},
                          timeout=120)
        d = r.json()
        print(f"[{tag}] 已创建 task_id={d.get('task_id','?')[:12]} @ {time.time()-t0:.2f}s")
    except Exception as e:
        print(f"[{tag}] 请求失败: {e}")

threads = [threading.Thread(target=send, args=(f"REQ{i}",)) for i in range(2)]
for t in threads: t.start()
for t in threads: t.join()
print("两个请求均已提交，请观察服务器端日志中的重叠时序。")

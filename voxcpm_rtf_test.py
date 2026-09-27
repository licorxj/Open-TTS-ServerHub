import requests, time, os, statistics

URI = "http://localhost:8854"
REF = r"Y:\LcTTSHub\VoxCPM\examples\reference_speaker.wav"
OUTDIR = r"Y:\LcTTSHub\voxcpm_repeat"
TEXT = "今天天气真好，我们一起去公园散步，享受这美好的午后时光。"
INSTRUCT = "Excited and fast-paced, with high energy and a cheerful tone"
TIMESTEPS = 10
N = 3

os.makedirs(OUTDIR, exist_ok=True)


def run_once(mode, instruct=None):
    data = {
        "text": TEXT,
        "ref_audio_path": REF,
        "language": "zh",
        "inference_timesteps": TIMESTEPS,
    }
    if instruct:
        data["instruct"] = instruct
    r = requests.post(f"{URI}/api/v1/voice/clone", data=data)
    r.raise_for_status()
    tid = r.json()["task_id"]
    s = r.json()
    for _ in range(60):
        time.sleep(2)
        s = requests.get(f"{URI}/api/v1/tasks/{tid}").json()
        if s["status"] in ("completed", "failed"):
            break
    if s["status"] == "completed" and s.get("output_path"):
        audio = requests.get(f"{URI}/api/v1/voice/download/{tid}")
        ext = ".wav"
        with open(os.path.join(OUTDIR, f"{mode}_rep{tid[:8]}{ext}"), "wb") as f:
            f.write(audio.content)
    return s


def summarize(label, rows):
    rtfs = [x["rtf"] for x in rows if x.get("rtf")]
    infs = [x["inference_time"] for x in rows if x.get("inference_time")]
    durs = [x["audio_duration"] for x in rows if x.get("audio_duration")]
    print(f"\n=== {label} (n={len(rows)}) ===")
    for i, x in enumerate(rows):
        print(f"  rep{i+1}: status={x['status']} RTF={x.get('rtf')} 推理={x.get('inference_time')}s 时长={x.get('audio_duration')}s")
    if rtfs:
        print(f"  -> RTF    mean={statistics.mean(rtfs):.4f}  min={min(rtfs):.4f}  max={max(rtfs):.4f}")
        print(f"  -> 推理/s mean={statistics.mean(infs):.3f}  时长/s mean={statistics.mean(durs):.3f}")


clone_rows, instruct_rows = [], []
for rep in range(N):
    clone_rows.append(run_once("clone"))
for rep in range(N):
    instruct_rows.append(run_once("instruct", INSTRUCT))

summarize("克隆模式 (ref_audio + text, 无 instruct)", clone_rows)
summarize("指令模式 (ref_audio + text + instruct)", instruct_rows)

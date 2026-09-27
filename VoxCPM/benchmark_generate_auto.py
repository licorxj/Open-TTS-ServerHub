#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VoxCPM 透明批量推理（/api/v1/voice/generate_auto）压测脚本。

设计目标：验证「开启 --batch 后，调用方仍发单条请求、服务端自动组装批次」的加速效果。

用法：
  1) 启动批量服务：
       python api_server.py --batch --inbound-limit 15 --batch-size 3
  2) 启动非批量基线服务（另开一个端口，便于同机对比）：
       python api_server.py --port 9541
  3) 跑压测（默认对两个端口都跑一遍，打印 RTF 对比）：
       python benchmark_generate_auto.py \
           --url-batch http://127.0.0.1:9540 \
           --url-serial http://127.0.0.1:9541 \
           --n 30 --concurrency 30 --text "今天天气真好，我们一起去公园散步吧。"

说明：
  - 脚本仅向 /api/v1/voice/generate_auto 并发发送「单条」请求，不要求调用方组装批次。
  - 有效 RTF = 生成音频总时长(秒) / 墙钟总耗时(秒)，数值越大代表整体越快（>1 表示比实时更快）。
  - 同一份文本下，--batch 服务的有效 RTF 应显著高于非批量服务，即为批量带来的加速。
  - 也可用 --serial-compare 只跑一个服务：先 concurrency=1（串行）再 concurrency=N（并发），
    在批量服务上会同时体现「并发 + 批次合并」的加速；在普通服务上只是并发加速。
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, install_opener, urlopen

# 本地压测：强制不走代理，避免 urllib 将 localhost 的 POST 经代理改写后返回 405
install_opener(build_opener(ProxyHandler({})))

ENDPOINT = "/api/v1/voice/generate_auto"


def _post(url: str, payload: dict, timeout: float):
    data = json.dumps(payload).encode("utf-8")
    target = url.rstrip("/") + ENDPOINT
    req = Request(
        target,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_payload(args) -> dict:
    p = {"text": args.text}
    if args.reference_wav_path:
        p["reference_wav_path"] = args.reference_wav_path
    if args.prompt_wav_path:
        p["prompt_wav_path"] = args.prompt_wav_path
    if args.prompt_text:
        p["prompt_text"] = args.prompt_text
    if args.cfg_value is not None:
        p["cfg_value"] = args.cfg_value
    if args.inference_timesteps is not None:
        p["inference_timesteps"] = args.inference_timesteps
    if args.normalize:
        p["normalize"] = True
    if args.denoise:
        p["denoise"] = True
    return p


def _worker(idx: int, url: str, payload: dict, timeout: float) -> dict:
    t0 = time.perf_counter()
    try:
        resp = _post(url, payload, timeout)
        latency = time.perf_counter() - t0
        dur = float(resp.get("duration") or 0.0)
        return {
            "idx": idx,
            "ok": True,
            "latency": latency,
            "duration": dur,
            "batch_mode": resp.get("batch_mode"),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "idx": idx,
            "ok": False,
            "latency": time.perf_counter() - t0,
            "error": f"{type(e).__name__}: {e}",
        }


def _run_pass(url: str, args, concurrency: int) -> dict:
    payload = _build_payload(args)
    t_start = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [
            ex.submit(_worker, i, url, payload, args.timeout)
            for i in range(args.n)
        ]
        for f in as_completed(futures):
            results.append(f.result())
    wall = time.perf_counter() - t_start

    oks = [r for r in results if r["ok"]]
    fails = [r for r in results if not r["ok"]]
    total_audio = sum(r["duration"] for r in oks)
    avg_lat = (sum(r["latency"] for r in oks) / len(oks)) if oks else 0.0
    max_lat = max((r["latency"] for r in oks), default=0.0)
    rtf = (total_audio / wall) if wall > 0 else 0.0

    return {
        "url": url,
        "n": args.n,
        "concurrency": concurrency,
        "ok": len(oks),
        "fail": len(fails),
        "wall": wall,
        "total_audio": total_audio,
        "avg_lat": avg_lat,
        "max_lat": max_lat,
        "rtf": rtf,
        "batch_mode": oks[0].get("batch_mode") if oks else None,
        "fails": fails,
    }


def _print_pass(title: str, r: dict) -> None:
    print("=" * 56)
    print(title)
    print(f"  服务地址        : {r['url']}")
    print(f"  请求数/并发     : {r['n']} / {r['concurrency']}")
    print(f"  成功/失败       : {r['ok']} / {r['fail']}")
    print(f"  墙钟总耗时      : {r['wall']:.2f}s")
    print(f"  生成音频总时长  : {r['total_audio']:.2f}s")
    print(f"  平均单条延迟    : {r['avg_lat']:.2f}s")
    print(f"  最大单条延迟    : {r['max_lat']:.2f}s")
    print(f"  有效 RTF        : {r['rtf']:.3f}  (生成音频秒/墙钟秒，越大越快)")
    if r["batch_mode"] is not None:
        print(f"  服务端 batch_mode: {r['batch_mode']}")
    if r["fails"]:
        print("  失败样例:")
        for fr in r["fails"][:5]:
            print(f"    #{fr['idx']}: {fr['error']}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="VoxCPM 透明批量推理压测脚本")
    ap.add_argument("--url-batch", default="http://127.0.0.1:9540",
                    help="开启 --batch 的服务地址（默认 http://127.0.0.1:9540）")
    ap.add_argument("--url-serial", default=None,
                    help="非批量基线服务地址；提供时与批量服务做 RTF 对比")
    ap.add_argument("--serial-compare", action="store_true",
                    help="仅对单一服务先 concurrency=1 再 concurrency=N 跑两遍")
    ap.add_argument("--n", type=int, default=30, help="请求总数")
    ap.add_argument("--concurrency", type=int, default=30, help="并发数")
    ap.add_argument("--text", default="今天天气真好，我们一起去公园散步吧。", help="合成文本")
    ap.add_argument("--reference-wav-path", default=None, help="克隆模式参考音频路径（服务端可达）")
    ap.add_argument("--prompt-wav-path", default=None, help="续写模式参考音频路径（服务端可达）")
    ap.add_argument("--prompt-text", default=None, help="续写模式参考文本")
    ap.add_argument("--cfg-value", type=float, default=None, help="CFG 引导强度")
    ap.add_argument("--inference-timesteps", type=int, default=None, help="LocDiT 迭代步数")
    ap.add_argument("--normalize", action="store_true", help="启用文本规范化")
    ap.add_argument("--denoise", action="store_true", help="对参考音频降噪")
    ap.add_argument("--timeout", type=float, default=120.0, help="单条请求超时（秒）")
    args = ap.parse_args()

    if args.url_serial is None and not args.serial_compare:
        # 仅跑批量服务一轮
        print(">> 仅对 --url-batch 跑一轮（如需对比请加 --url-serial 或 --serial-compare）\n")
        r = _run_pass(args.url_batch, args, args.concurrency)
        _print_pass("批量服务压测结果", r)
        return 0

    if args.serial_compare:
        print(f">> 单服务对比：先串行(concurrency=1)再并发(concurrency={args.concurrency})\n")
        r_s = _run_pass(args.url_batch, args, 1)
        _print_pass(f"串行 (concurrency=1) @ {args.url_batch}", r_s)
        r_c = _run_pass(args.url_batch, args, args.concurrency)
        _print_pass(f"并发 (concurrency={args.concurrency}) @ {args.url_batch}", r_c)
        if r_s["wall"] > 0 and r_c["wall"] > 0:
            speedup = r_s["wall"] / r_c["wall"]
            print("=" * 56)
            print(f"并发相对串行加速比（墙钟）: {speedup:.2f}x")
            print("=" * 56)
        return 0

    # 批量 vs 非批量 对比
    print(">> 批量服务 vs 非批量基线 对比\n")
    r_b = _run_pass(args.url_batch, args, args.concurrency)
    _print_pass(f"批量服务 @ {args.url_batch}", r_b)
    r_s = _run_pass(args.url_serial, args, args.concurrency)
    _print_pass(f"非批量基线 @ {args.url_serial}", r_s)
    if r_s["rtf"] > 0:
        ratio = r_b["rtf"] / r_s["rtf"]
        print("=" * 56)
        print(f"批量相对非批量 有效 RTF 提升: {ratio:.2f}x  "
              f"({r_b['rtf']:.3f} vs {r_s['rtf']:.3f})")
        print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

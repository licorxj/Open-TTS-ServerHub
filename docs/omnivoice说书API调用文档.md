# OmniVoice 说书 API 调用文档（流式声音克隆）

本接口由 `启动_omnivoice说书api.bat` 启动，对应服务文件 `omnivoice/simple_tts_clone.py`。
接口已改造为**流式返回**：长文本会分段生成，服务端生成一段即推送一段 WAV 音频，
客户端可按 HTTP 音频流（`<audio>` / `fetch` + `Response.body`）边接收边播放。

服务默认地址：`http://localhost:8817`

---

## 1. 接口说明

| 项 | 内容 |
| --- | --- |
| 路径 | `POST /tts` |
| 请求格式 | `application/json` |
| 响应格式 | `audio/wav` 流式音频（`Transfer-Encoding: chunked`） |
| 响应头 | `X-Task-Id`（本次任务 ID，便于排查日志） |

---

## 2. 请求体字段（JSON）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | string | 是 | 要合成说书的文本（不能为空） |
| `voice_name` | string | 否 | **音色名称**。克隆时系统会到 `voice/` 目录下查找同名的 `.wav` 作为参考音频，例如 `"women"` → `voice/women.wav`；若文件名已带 `.wav` 后缀也可直接写 `women.wav` |
| `ref_audio` | string | 否 | 参考音频文件路径（绝对路径或相对项目根目录的路径）。**优先级低于 `voice_name`** |
| `ref_text` | string | 否 | 参考音频对应的文本，有助于提升克隆质量 |

### 参考音频解析优先级

1. 若提供了 `voice_name` 且 `voice/<voice_name>.wav` 存在 → 使用该文件
2. 否则若提供了 `ref_audio` 且路径存在 → 使用该路径
3. 否则回退读取 `voice/voice_path.txt` 中配置的参考音频路径
4. 若以上都不存在 → 返回 `400` 错误

> 推荐用法：把克隆参考音频按音色命名后放进 `voice/` 目录（如 `voice/women.wav`），
> 调用时只传 `voice_name` 即可，无需每次指定路径。

---

## 3. 流式返回机制

- 文本较长时，服务端开启分段生成（`audio_chunk_duration=30s`），将音频切成多段
- 每段生成完毕立即编码为独立 WAV 字节块并通过流式响应推送
- 响应 `Content-Type: audio/wav`，使用 chunked 传输，客户端可渐进播放
- 服务端日志会打印每个 chunk 的推送大小与任务 ID（`X-Task-Id`）

> 注意：由于底层 OmniVoice 的 `generate` 为同步接口，分段是在模型完整计算后逐块 flush 输出，
> HTTP 层面为真正的流式音频流；若文本较短（单段），则等价于一次性返回完整 WAV 流。

---

## 4. 调用示例

### 4.1 cURL（保存到文件）

```bash
curl -X POST http://localhost:8817/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"话说天下大势，分久必合，合久必分。","voice_name":"women"}' \
  -o shushu.wav
```

### 4.2 cURL + 流式边收边存（显示进度）

```bash
curl -N -X POST http://localhost:8817/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"这是一段很长的说书文本……","voice_name":"women","ref_text":"参考音频原文"}' \
  -o shushu_stream.wav
```

### 4.3 Python（requests，流式接收）

```python
import requests

url = "http://localhost:8817/tts"
payload = {
    "text": "话说天下大势，分久必合，合久必分。",
    "voice_name": "women",          # 对应 voice/women.wav
    # "ref_audio": "D:/refs/a.wav",  # 也可直接指定路径，优先级低于 voice_name
    # "ref_text": "参考音频原文",
}

with requests.post(url, json=payload, stream=True) as r:
    r.raise_for_status()
    print("Task-Id:", r.headers.get("X-Task-Id"))
    with open("shushu.wav", "wb") as f:
        for chunk in r.iter_content(chunk_size=4096):
            if chunk:
                f.write(chunk)
print("保存完成：shushu.wav")
```

### 4.4 Python（aiohttp，异步流式 + 边播放）

```python
import aiohttp, asyncio

async def main():
    url = "http://localhost:8817/tts"
    payload = {"text": "且听下回分解。", "voice_name": "women"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            chunks = []
            async for chunk in resp.content.iter_chunked(8192):
                chunks.append(chunk)
                # 这里可以把 chunk 直接喂给播放器实现边下边播
            with open("shushu.wav", "wb") as f:
                f.write(b"".join(chunks))

asyncio.run(main())
```

### 4.5 前端（fetch + <audio> 渐进播放）

```html
<audio id="player" controls></audio>
<script>
async function speak() {
  const resp = await fetch("http://localhost:8817/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: "欲知后事如何，且听下回分解。", voice_name: "women" })
  });
  const blob = await resp.blob();           // 也可改用 resp.body.getReader() 实现逐块播放
  document.getElementById("player").src = URL.createObjectURL(blob);
}
speak();
</script>
```

---

## 5. 错误码

| HTTP 状态 | 含义 |
| --- | --- |
| 400 | 缺少有效参考音频（voice_name 未命中、ref_audio 不存在且 voice_path.txt 也不可用） |
| 503 | 模型尚未加载完成（服务刚启动） |
| 500 | 推理过程中出现异常，错误信息见响应体 / 服务端日志 |

---

## 6. 新增音色步骤

1. 准备一段目标说话人的参考音频（建议 5–15 秒清晰语音）
2. 命名为 `<音色名>.wav` 放入 `voice/` 目录，例如 `voice/women.wav`
3. 调用时传入 `"voice_name": "women"` 即可克隆该音色，无需改动代码或配置文件

> 也可继续沿用旧方式：把默认参考音频路径写入 `voice/voice_path.txt`，
> 调用时不传 `voice_name` 与 `ref_audio` 即自动回退使用该路径。

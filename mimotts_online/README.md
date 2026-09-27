# MiMo TTS API (Online Version)

MiMo TTS API 是小米 MiMo 语音合成服务的 Python 接口，通过 OpenAI 兼容的 Chat Completions API 调用在线 TTS 服务。支持预置音色、音色设计和音色克隆三大功能，并提供多线程并发处理能力。

## 支持的模型

| 模型名称 | Model ID | 功能 | 音色 |
|---------|----------|------|------|
| MiMo-V2.5-TTS | `mimo-v2.5-tts` | 使用预置精品音色进行语音合成 | 使用预置音色列表中的精品音色 |
| MiMo-V2.5-TTS-VoiceDesign | `mimo-v2.5-tts-voicedesign` | 通过文本描述定制音色 | 通过文本描述自动生成音色 |
| MiMo-V2.5-TTS-VoiceClone | `mimo-v2.5-tts-voiceclone` | 基于音频样本复刻任意音色 | 通过音频样本精准复刻音色 |

## 预置音色列表

| 音色名 | Voice ID | 语言 | 性别 |
|--------|----------|------|------|
| MiMo-默认 | `mimo_default` | 因部署集群而异 | - |
| 冰糖 | `冰糖` | 中文 | 女性 |
| 茉莉 | `茉莉` | 中文 | 女性 |
| 苏打 | `苏打` | 中文 | 男性 |
| 白桦 | `白桦` | 中文 | 男性 |
| Mia | `Mia` | 英文 | 女性 |
| Chloe | `Chloe` | 英文 | 女性 |
| Milo | `Milo` | 英文 | 男性 |
| Dean | `Dean` | 英文 | 男性 |

## 安装依赖

```bash
pip install requests numpy
```

## 配置 API Key

### 方式 0: 配置文件（推荐）

编辑 `config.json` 文件，填入你的 API 密钥：

```json
{
  "api_key": "sk-your_api_key_here",
  "api_base": "https://api.xiaomimimo.com/v1",
  "timeout": 120,
  "max_workers": 5
}
```

### 方式 1: 环境变量

```bash
# Linux/Mac
export MIMO_API_KEY="your_api_key"

# Windows
set MIMO_API_KEY=your_api_key
```

### 方式 2: 代码中指定

```python
tts = MiMoTTS(api_key="your_api_key")
```

### 方式 3: 配置对象

```python
config = MiMoTTSConfig(api_key="your_api_key")
tts = MiMoTTS(config=config)
```

**优先级**: 代码参数 > config.json > 环境变量

## 快速开始

### 基础用法 - 预置音色

```python
from mimotts_online.mimotts_api import MiMoTTS

# 初始化
tts = MiMoTTS()

# 使用预置音色合成
result = tts.synthesize_with_preset(
    text="你好，欢迎使用小米MiMo语音合成系统！",
    voice="冰糖",
    style_description="用温柔亲切的语气，语速适中"
)

if result.success:
    tts.save_audio(result, "output.wav")

tts.shutdown()
```

### 使用上下文管理器

```python
from mimotts_online.mimotts_api import MiMoTTS

with MiMoTTS() as tts:
    result = tts.synthesize_with_preset(text="Hello, world!", voice="Mia")
    tts.save_audio(result, "output.wav")
```

### 高级 API 封装

```python
from mimotts_online.mimotts_api import MiMoTTSAPI

api = MiMoTTSAPI()

# 预置音色
result = api.synthesize("你好，世界！", voice="冰糖")

# 音色设计
result = api.design_voice("你好！", "一位温柔的女性，声音甜美")

# 音色克隆
result = api.clone_voice("你好！", "reference.wav")

api.shutdown()
```

## API 参考

### MiMoTTS 类

#### 初始化参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| api_key | str | None | API 密钥，None 时读取 MIMO_API_KEY 环境变量 |
| api_base | str | "https://api.xiaomimimo.com/v1" | API 基础 URL |
| timeout | int | 120 | 请求超时时间（秒） |
| max_workers | int | 5 | 最大并发工作线程数 |
| config | MiMoTTSConfig | None | 配置对象（覆盖其他参数） |

#### synthesize_with_preset()

使用预置音色进行语音合成（mimo-v2.5-tts 模型）。

```python
def synthesize_with_preset(
    text: str,                              # 要合成的文本
    voice: str = "冰糖",                    # 预置音色名称
    style_description: Optional[str] = None,  # 风格描述（自然语言控制）
    audio_format: str = "wav",              # 输出格式（wav, pcm16）
) -> TTSResult
```

**示例**:
```python
# 基础用法
result = tts.synthesize_with_preset(text="你好！", voice="冰糖")

# 带风格控制
result = tts.synthesize_with_preset(
    text="你好！",
    voice="冰糖",
    style_description="用温柔亲切的语气，语速适中"
)
```

#### synthesize_with_design()

通过文本描述设计音色进行语音合成（mimo-v2.5-tts-voicedesign 模型）。

```python
def synthesize_with_design(
    text: str,                    # 要合成的文本
    voice_description: str,       # 音色描述
    audio_format: str = "wav",    # 输出格式
) -> TTSResult
```

**音色描述维度**:

| 维度 | 示例 |
|------|------|
| 性别与年龄 | "young woman in her mid-20s"、"五十多岁的中年男性" |
| 音色/质感 | "deep and gravelly"、"丝滑醇厚、带着磁性" |
| 情绪/语气 | "warm and confident"、"温柔但带着一丝疲惫" |
| 语速/节奏 | "slow and deliberate"、"语速极快，像连珠炮" |
| 角色/人设 | "narrator, podcast host, 评书先生, 深夜电台DJ" |
| 说话风格 | "casual and colloquial"、"一本正经地" |
| 场景描写 | "narrating a nature documentary"、"在给投资人路演" |

**示例**:
```python
# 中文描述
result = tts.synthesize_with_design(
    text="你好，这是一段测试语音。",
    voice_description="一位温柔的年轻女性，声音甜美，语速稍慢"
)

# 英文描述
result = tts.synthesize_with_design(
    text="Hello, this is a test voice.",
    voice_description="Young female, warm and confident, speaking slowly"
)
```

#### synthesize_with_clone()

基于音频样本复刻音色进行语音合成（mimo-v2.5-tts-voiceclone 模型）。

```python
def synthesize_with_clone(
    text: str,                                    # 要合成的文本
    reference_audio_path: Optional[str] = None,   # 参考音频路径
    reference_audio_base64: Optional[str] = None, # 参考音频 Base64
    style_description: Optional[str] = None,      # 风格描述
    audio_format: str = "wav",                    # 输出格式
) -> TTSResult
```

**注意事项**:
- 参考音频支持 mp3 和 wav 格式
- Base64 编码后的字符串大小不能超过 10 MB
- 会自动添加 MIME 类型前缀：`data:{MIME_TYPE};base64,$BASE64_AUDIO`

**示例**:
```python
# 使用本地文件
result = tts.synthesize_with_clone(
    text="你好，这是使用克隆声音合成的语音。",
    reference_audio_path="reference.wav"
)

# 使用 Base64
result = tts.synthesize_with_clone(
    text="你好！",
    reference_audio_base64="UklGR..."
)
```

#### batch_synthesize()

批量合成（多线程并发）。

```python
def batch_synthesize(
    requests: List[Dict[str, Any]],  # 请求列表
    max_workers: Optional[int] = None,  # 覆盖默认并发数
) -> List[TTSResult]
```

**请求格式**:
```python
requests = [
    {
        "mode": "preset",  # 或 "design", "clone"
        "text": "第一段文本",
        "voice": "冰糖",  # preset 模式
    },
    {
        "mode": "design",
        "text": "第二段文本",
        "voice_description": "一位温柔的女性",  # design 模式
    },
    {
        "mode": "clone",
        "text": "第三段文本",
        "reference_audio_path": "ref.wav",  # clone 模式
    },
]
```

#### save_audio()

保存音频到文件。

```python
def save_audio(
    result: TTSResult,  # TTS 结果
    output_path: str,   # 输出路径
) -> bool
```

#### get_preset_voices()

获取可用的预置音色。

```python
def get_preset_voices() -> Dict[str, str]
```

#### shutdown()

释放资源，关闭连接。

### 配置类

#### MiMoTTSConfig

```python
@dataclass
class MiMoTTSConfig:
    api_key: str = ""                    # API 密钥
    api_base: str = DEFAULT_API_BASE     # API 基础 URL
    timeout: int = 120                   # 超时时间
    max_retries: int = 3                 # 最大重试次数
```

### 结果类

#### TTSResult

```python
@dataclass
class TTSResult:
    success: bool = False                 # 是否成功
    audio_data: Optional[bytes] = None    # 音频数据（字节）
    audio_base64: str = ""                # 音频数据（Base64）
    format: str = "wav"                   # 音频格式
    error_message: str = ""               # 错误信息
    metadata: Dict[str, Any] = {}         # 元数据
```

### 便捷函数

#### quick_synthesize()

快速预置音色合成并保存。

```python
from mimotts_online.mimotts_api import quick_synthesize

success = quick_synthesize(
    text="你好，世界！",
    output_path="output.wav",
    voice="冰糖",
    api_key="your_key"
)
```

#### quick_design()

快速音色设计并保存。

```python
from mimotts_online.mimotts_api import quick_design

success = quick_design(
    text="你好！",
    description="一位温柔的女性，声音甜美",
    output_path="output.wav",
    api_key="your_key"
)
```

#### quick_clone()

快速音色克隆并保存。

```python
from mimotts_online.mimotts_api import quick_clone

success = quick_clone(
    text="你好！",
    reference_audio="reference.wav",
    output_path="output.wav",
    api_key="your_key"
)
```

## 使用示例

### 示例 1: 基础预置音色合成

```python
from mimotts_online.mimotts_api import MiMoTTS

tts = MiMoTTS()

result = tts.synthesize_with_preset(
    text="你好，欢迎使用小米MiMo语音合成系统！",
    voice="冰糖",
    style_description="用温柔亲切的语气，语速适中"
)

if result.success:
    print(f"合成成功，音频大小: {len(result.audio_data)} 字节")
    tts.save_audio(result, "welcome.wav")

tts.shutdown()
```

### 示例 2: 音色设计

```python
from mimotts_online.mimotts_api import MiMoTTS

tts = MiMoTTS()

result = tts.synthesize_with_design(
    text="你好，这是一段测试语音。",
    voice_description="一位温柔的年轻女性，声音甜美，语速稍慢"
)

if result.success:
    tts.save_audio(result, "designed_voice.wav")

tts.shutdown()
```

### 示例 3: 音色克隆

```python
from mimotts_online.mimotts_api import MiMoTTS

tts = MiMoTTS()

result = tts.synthesize_with_clone(
    text="你好，这是使用克隆声音合成的语音。",
    reference_audio_path="reference.wav"
)

if result.success:
    tts.save_audio(result, "cloned_voice.wav")

tts.shutdown()
```

### 示例 4: 批量处理

```python
from mimotts_online.mimotts_api import MiMoTTS

tts = MiMoTTS(max_workers=5)

requests = [
    {"mode": "preset", "text": "第一段文本", "voice": "冰糖"},
    {"mode": "preset", "text": "第二段文本", "voice": "茉莉"},
    {"mode": "design", "text": "第三段文本", "voice_description": "一位温柔的女性"},
]

results = tts.batch_synthesize(requests)

for i, result in enumerate(results):
    if result.success:
        tts.save_audio(result, f"batch_output_{i+1}.wav")

tts.shutdown()
```

### 示例 5: 异步处理

```python
from mimotts_online.mimotts_api import MiMoTTS

tts = MiMoTTS(max_workers=5)

# 提交异步请求
futures = []
for i in range(5):
    future = tts.synthesize_with_preset_async(
        text=f"异步请求 {i}",
        voice="冰糖" if i % 2 == 0 else "茉莉"
    )
    futures.append(future)

# 等待所有结果
for i, future in enumerate(futures):
    result = future.result()
    if result.success:
        tts.save_audio(result, f"async_output_{i}.wav")

tts.shutdown()
```

### 示例 6: 使用上下文管理器

```python
from mimotts_online.mimotts_api import MiMoTTS

with MiMoTTS() as tts:
    # 预置音色
    result1 = tts.synthesize_with_preset(text="第一段", voice="冰糖")
    tts.save_audio(result1, "output1.wav")
    
    # 音色设计
    result2 = tts.synthesize_with_design(text="第二段", voice_description="温柔女声")
    tts.save_audio(result2, "output2.wav")
    
    # 音色克隆
    result3 = tts.synthesize_with_clone(text="第三段", reference_audio_path="ref.wav")
    tts.save_audio(result3, "output3.wav")
```

### 示例 7: 风格控制

#### 自然语言控制

```python
result = tts.synthesize_with_preset(
    text="Hey boss — guess what, guess what? I just got the results back and I actually passed!",
    voice="Chloe",
    style_description="Bright, bouncy, slightly sing-song tone — like you are bursting with good news you can barely hold in. Fast pace, rising pitch at the end."
)
```

#### 音频标签控制

```python
result = tts.synthesize_with_preset(
    text="(怅然)这么多年过去了，再走过那条街，心里一下子空了一块。",
    voice="冰糖"
)

result = tts.synthesize_with_preset(
    text="(东北话)哎呀妈呀，这天儿也忒冷了吧！",
    voice="苏打"
)

result = tts.synthesize_with_preset(
    text="(唱歌)原谅我这一生不羁放纵爱自由",
    voice="冰糖"
)
```

## 风格控制

### 自然语言控制

通过 user message 传入自然语言指令来控制合成语音的风格。

**示例**:
- "用轻快上扬的语调向领导报喜，语速稍快，带着查到成绩后压抑不住的激动与小骄傲"
- "用明亮活泼的青少年嗓音，带着恶作剧得逞后的得意与戏谑"
- "冰冷、慵懒却极具威压的低音御姐。发声通道非常松弛"

### 音频标签控制

通过在 assistant message 的文本中嵌入风格标签来控制语音。

**基础情绪**: 开心/悲伤/愤怒/恐惧/惊讶/兴奋/委屈/平静/冷漠

**复合情绪**: 怅然/欣慰/无奈/愧疚/释然/嫉妒/厌倦/忐忑/动情

**整体语调**: 温柔/高冷/活泼/严肃/慵懒/俏皮/深沉/干练/凌厉

**音色定位**: 磁性/醇厚/清亮/空灵/稚嫩/苍老/甜美/沙哑/醇雅

**人设腔调**: 夹子音/御姐音/正太音/大叔音/台湾腔

**方言**: 东北话/四川话/河南话/粤语

**角色扮演**: 孙悟空/林黛玉

**唱歌**: 唱歌

**示例**:
```
(怅然)这么多年过去了，再走过那条街，心里一下子空了一块。
(慵懒)再让我睡五分钟……就五分钟，真的，最后一次。
(磁性)夜已经深了，城市还在呼吸。我是今晚陪你的人。
(东北话)哎呀妈呀，这天儿也忒冷了吧！
(粤语)呢个真係好正啊！食过一次就唔会忘记！
(唱歌)原谅我这一生不羁放纵爱自由
```

## 注意事项

1. **API Key**: 需要有效的 API Key 才能使用，可从小米 MiMo 平台获取
2. **网络连接**: 需要稳定的网络连接访问 API
3. **请求限制**: API 可能有请求频率限制
4. **音频格式**: 支持 wav 和 pcm16 格式
5. **资源释放**: 使用完毕后调用 `shutdown()` 释放资源
6. **超时设置**: 根据网络情况调整 timeout 参数
7. **重试机制**: 内置自动重试，可通过 max_retries 配置
8. **流式输出**: MiMo-V2.5-TTS 系列的低延迟流式输出功能暂未上线

## 运行测试

```bash
cd mimotts_online
python test_mimotts_api.py
```

测试脚本会提示输入 API Key，或从 MIMO_API_KEY 环境变量读取。

## 许可证

本项目遵循项目根目录的许可证。

## 更新日志

### v1.0.0

- 初始版本
- 支持 MiMo-V2.5-TTS 系列三个模型
- 支持预置音色合成功能
- 支持音色设计功能
- 支持音色克隆功能
- 支持多线程并发处理
- 支持批量处理
- 支持异步处理
- 支持请求重试和错误处理
- 支持自然语言风格控制
- 支持音频标签风格控制
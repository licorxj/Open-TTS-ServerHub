# OmniVoice 示例

此目录包含用于训练、微调和评估 OmniVoice 的脚本和配置文件。

| 使用场景 | 脚本 | 描述 |
|---|---|---|
| 从头训练 | [run_emilia.sh](run_emilia.sh) | 在 Emilia 数据集上的完整流程（数据检查、分词化、训练） |
| 微调 | [run_finetune.sh](run_finetune.sh) | 使用您自己的 JSONL 数据从预训练检查点进行微调 |
| 评估 | [run_eval.sh](run_eval.sh) | 在标准测试集上评估 WER、说话人相似度和 UTMOS |

---

## 从头训练（Emilia）

[run_emilia.sh](run_emilia.sh) 分 3 个阶段运行完整流程：

| 阶段 | 作用 |
|---|---|
| 0 | 验证 Emilia 数据集和 JSONL 清单是否就位 |
| 1 | 将音频分词化为 WebDataset 分片 |
| 2 | 使用 `accelerate` 启动多 GPU 训练 |

**前提条件：**

1. 从 [OpenXLab](https://openxlab.org.cn/datasets/Amphion/Emilia) 下载 Emilia 数据集并将其放置在 `download/` 目录下：
   ```
   download/Amphion___Emilia
   └── raw
       ├── EN
       └── ZH
   ```
2. 获取 JSONL 清单并将其放置在 `data/emilia/manifests/` 目录中：
   - `emilia_en_train.jsonl`, `emilia_en_dev.jsonl`
   - `emilia_zh_train.jsonl`, `emilia_zh_dev.jsonl`

   您可以从原始数据生成这些清单，或者从 [HuggingFace](https://huggingface.co/datasets/zhu-han/Emilia-Manifests) 下载预处理过的清单。

**运行完整流程：**

```bash
bash examples/run_emilia.sh
```

或者通过在脚本顶部设置 `stage` 和 `stop_stage` 来运行单个阶段（例如 `stage=1`, `stop_stage=1` 仅进行分词化）。

> 有关配置详情、检查点恢复和 TensorBoard 监控，请参阅 [docs/training.md](../docs/training.md)。

---

## 微调

[run_finetune.sh](run_finetune.sh) 在您自己的数据上从预训练检查点进行微调。

### 步骤 1：准备数据

创建一个 JSONL 清单，其中每一行描述一个音频样本：

```jsonl
{"id": "sample_001", "audio_path": "/data/audio/001.wav", "text": "Hello world", "language_id": "en"}
{"id": "sample_002", "audio_path": "/data/audio/002.wav", "text": "你好世界", "language_id": "zh"}
```

`id`、`audio_path` 和 `text` 是必需的。`language_id` 是可选的。

> 有关完整数据格式规范，请参阅 [docs/data_preparation.md](../docs/data_preparation.md)。

### 步骤 2：配置脚本

编辑 `run_finetune.sh` 顶部的变量：

```bash
TRAIN_JSONL="data/my_data_train.jsonl"   # 训练 JSONL 文件路径
DEV_JSONL="data/my_data_dev.jsonl"       # 验证 JSONL 文件路径
GPU_IDS="0,1"                            # 要使用的 GPU
NUM_GPUS=2
OUTPUT_DIR="exp/omnivoice_finetune"      # 输出目录
```

### 步骤 3：运行

```bash
bash examples/run_finetune.sh
```

脚本将：
1. 将您的音频分词化为 WebDataset 分片
2. 使用 `accelerate` 启动微调

微调配置 ([config/train_config_finetune.json](config/train_config_finetune.json)) 与 Emilia 训练配置 ([config/train_config_emilia.json](config/train_config_emilia.json)) 的主要区别如下：

| 参数 | Emilia（从头训练） | 微调 | 原因 |
|---|---|---|---|
| `init_from_checkpoint` | `null` | `"k2-fsa/OmniVoice"` | 加载预训练权重 |
| `steps` | 300,000 | 5,000 | 微调所需步数较少，可根据您的数据/任务进行调整 |
| `learning_rate` | 1e-4 | 5e-5 | 微调使用较低的学习率，可根据您的数据/任务进行调整 |

要使用不同的预训练检查点，请修改配置文件中的 `init_from_checkpoint`。

如果您的 GPU 上遇到 `flex_attention` 问题，请改用 [config/train_config_finetune_sdpa.json](config/train_config_finetune_sdpa.json)，它使用 SDPA 注意力机制以获得更广泛的兼容性。详见 [docs/training.md](../docs/training.md#attention-implementation)。

---

## 评估

首先安装评估依赖项：

```bash
pip install omnivoice[eval]
# 或
uv sync --extra eval
```

支持的测试集：`librispeech_pc`、`seedtts_en`、`seedtts_zh`、`fleurs`、`minimax`。

```bash
bash examples/run_eval.sh
```

> 有关指标详情、测试集准备和运行单个指标，请参阅 [docs/evaluation.md](...docs/evaluation.md)。
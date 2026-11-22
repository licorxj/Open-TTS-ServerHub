<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'

const skill = ref(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    skill.value = await api.skill()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 带兜底的剪贴板写入（非安全上下文 / 权限受限时退回 execCommand） */
async function copy(text, label) {
  const value = String(text || '')
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success(`已复制${label}`)
    return
  } catch {
    /* 继续兜底 */
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = value
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    if (ok) {
      ElMessage.success(`已复制${label}`)
      return
    }
  } catch {
    /* ignore */
  }
  ElMessage.warning('复制失败，请手动选中文本复制')
}

const pathRows = computed(() => {
  const s = skill.value
  if (!s) return []
  return [
    { k: 'Skill 名称', v: s.name, copy: false },
    { k: 'Skill 目录（绝对路径）', v: s.skill_dir, copy: true },
    { k: 'SKILL.md（绝对路径）', v: s.skill_md, copy: true },
    { k: 'CLI 脚本（绝对路径）', v: s.script, copy: true },
    { k: '参考文档', v: (s.references || []).join('、') || '—', copy: false },
    { k: '项目根目录', v: s.root, copy: true },
    { k: 'Python 解释器', v: s.python, copy: true },
    { k: '本机管家端口', v: String(s.hub_port || 5190), copy: false },
    { k: '安装状态', v: s.installed_path ? `已安装：${s.installed_path}` : '未检测到安装目录', copy: false },
  ]
})

const promptText = computed(() => {
  const s = skill.value
  if (!s) return ''
  return `请安装本项目的 TTS 配音 Skill（name: ${s.name}）。

Skill 目录（绝对路径）：${s.skill_dir}
SKILL.md（绝对路径）：${s.skill_md}
CLI 脚本（绝对路径）：${s.script}
参考文档目录：${s.skill_dir}\\references
Python 解释器：${s.python}
本机管家端口：${s.hub_port}

安装步骤：
1. 把整个 Skill 目录（${s.skill_dir}）复制或以软链接方式装进智能体的技能目录，目录名必须是 ${s.name}：
   - CodeBuddy：C:\\Users\\<用户名>\\.codebuddy\\skills\\${s.name}
   - Claude Code：<项目目录>\\.claude\\skills\\${s.name}
   - 其他智能体：放到它约定的 skills 目录即可
2. 不要改动 SKILL.md 的 frontmatter（name / description），那是智能体识别并触发该技能的依据；
3. 安装后读一遍 SKILL.md 与 references\\interfaces.md，确认 scripts\\tts_agent.py 可以执行；
4. 不要修改 skill 内的脚本与文档（它们与本机端口、引擎配置绑定），需要调整时先问我。

安装完成后，请用一句话确认已安装，并告诉我如何用它配音。`
})

const wakeText = computed(() => {
  const s = skill.value
  if (!s) return ''
  return `安装完成后，直接对智能体说下面任意一句就会触发配音（它会自动调用 ${s.name} Skill → 请求本机 ${s.hub_port} 管家 → 合成 wav）：

· 把这段文字转成语音：……
· 用 VoxCPM 克隆这个音色，配音：……
· 用上次那个接口配音：……
· 把 README.md 读成语音

智能体实际执行的命令（供参考，由它自己决定）：
"${s.python}" "${s.script}" run --interface last --text "要合成的文本"

常用子命令：interfaces（列出可用接口）/ last（上次用的接口与参考音频）/ run（合成并落盘）/ status（管家与引擎进程状态）/ stop（清理进程、释放显存）。`
})
</script>

<template>
  <div>
    <div class="ctrl panel">
      <span class="hud">SKILL INSTALL</span>
      <span class="mono-sm">
        让智能体学会调用本机 TTS 管家配音 —— 复制下面的提示词发给它即可完成安装
      </span>

      <div class="spacer"></div>

      <button class="btn" :disabled="loading" @click="load">刷新</button>
    </div>

    <div class="panel">
      <div class="panel__hd">
        <div class="panel__title">Skill 本体 · 绝对路径</div>
        <span class="mono-sm" style="color: var(--text-mute)">
          路径由后端实时给出，可直接复制给智能体
        </span>
      </div>
      <div class="panel__body">
        <div v-if="!skill" class="mono-sm" style="color: var(--text-mute)">加载中…</div>
        <div v-else class="kv">
          <template v-for="row in pathRows" :key="row.k">
            <div class="kv__k">{{ row.k }}</div>
            <code class="kv__v">{{ row.v }}</code>
            <button
              v-if="row.copy"
              class="btn btn--ghost btn--xs"
              :title="`复制 ${row.v}`"
              @click="copy(row.v, row.k)"
            >
              复制
            </button>
            <div v-else class="kv__v kv__v--dash">—</div>
          </template>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel__hd">
        <div class="panel__title">① 安装提示词 · 整段复制给智能体</div>
        <button class="btn btn--primary" :disabled="!promptText" @click="copy(promptText, '安装提示词')">
          一键复制提示词
        </button>
      </div>
      <div class="panel__body">
        <pre class="code">{{ promptText }}</pre>
        <div class="note">
          提醒：请把上面的提示词完整粘贴给你的智能体（CodeBuddy / Claude Code / Cursor 等），
          让它按提示把 Skill 装进自己的技能目录；安装后智能体就能直接调用本机 5190 管家配音。
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel__hd">
        <div class="panel__title">② 装好后如何唤醒智能体配音</div>
        <button class="btn btn--ghost" :disabled="!wakeText" @click="copy(wakeText, '唤醒示例')">
          复制唤醒示例
        </button>
      </div>
      <div class="panel__body">
        <pre class="code">{{ wakeText }}</pre>
        <div class="note">
          智能体被唤醒后会自动：确认接口 → 拉起管家/引擎 → 合成 wav → 关闭进程释放显存，
          并把生成的音频绝对路径报给你。全程无需手工敲命令。
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ctrl {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 13px 16px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.panel {
  margin-bottom: 16px;
}

.kv {
  display: grid;
  grid-template-columns: 190px 1fr auto;
  align-items: center;
  gap: 10px 14px;
}

.kv__k {
  font-size: 12.5px;
  color: var(--text-dim);
}

.kv__v {
  font-family: var(--font-mono);
  font-size: 12.5px;
  color: var(--amber-2);
  word-break: break-all;
}

.kv__v--dash {
  color: var(--text-mute);
}

.btn--xs {
  height: 24px;
  padding: 0 9px;
  font-size: 11px;
}

.code {
  margin: 0;
  padding: 14px 16px;
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text);
  background: var(--panel-3);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  white-space: pre-wrap;
  word-break: break-word;
}

.note {
  margin-top: 10px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-dim);
}
</style>

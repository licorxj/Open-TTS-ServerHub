<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

const router = useRouter()

const voices = ref([])
const loading = ref(false)

/* ---------------- 默认音色（克隆兜底） ---------------- */
const defaultVoice = ref('')
const defaultSaving = ref(false)

async function loadDefault() {
  try {
    const r = await api.getDefaultVoice()
    defaultVoice.value = r?.default_voice || ''
  } catch {
    defaultVoice.value = ''
  }
}

async function onDefaultChange(id) {
  defaultSaving.value = true
  try {
    const r = await api.setDefaultVoice(id || '')
    defaultVoice.value = r?.default_voice || ''
    if (defaultVoice.value) {
      const v = voices.value.find((x) => x.id === defaultVoice.value)
      ElMessage.success(`默认音色已设为「${v?.name || defaultVoice.value}」，克隆未传参考音频时将自动使用它`)
    } else {
      ElMessage.info('已清除默认音色')
    }
  } catch (e) {
    ElMessage.error(`设置默认音色失败：${e.message}`)
    await loadDefault()
  } finally {
    defaultSaving.value = false
  }
}

/* ---------------- 试听：点击才加载音频字节 ----------------
   列表加载只取元数据（名称/文件/性别/年龄/大小），不碰音频内容；
   只有点「试听」时才请求 /audio 拉字节并缓存 blob URL。 */
const player = ref({ id: '', name: '', url: '', size: 0, loading: false, error: '' })
const playing = ref(false)
const audioCache = new Map() // id -> { url, size }

const GENDER_OPTS = [
  { v: '', l: '未标注' },
  { v: 'female', l: '女声' },
  { v: 'male', l: '男声' },
  { v: 'neutral', l: '中性' },
]
const AGE_OPTS = [
  { v: '', l: '未标注' },
  { v: 'child', l: '童声' },
  { v: 'teen', l: '少年' },
  { v: 'young', l: '青年' },
  { v: 'middle', l: '中年' },
  { v: 'old', l: '老年' },
]

function genderLabel(g) {
  return GENDER_OPTS.find((x) => x.v === (g || ''))?.l || '未标注'
}
function ageLabel(a) {
  return AGE_OPTS.find((x) => x.v === (a || ''))?.l || '未标注'
}

function fmtSize(b) {
  if (!b) return '—'
  return b > 1024 * 1024 ? `${(b / 1048576).toFixed(2)} MB` : `${(b / 1024).toFixed(1)} KB`
}

/** 复制音色 ID（外部调用克隆接口时传 voice_id） */
async function copyId(v) {
  const id = String(v?.id || '')
  if (!id) return
  try {
    await navigator.clipboard.writeText(id)
    ElMessage.success(`已复制音色 ID：${id}`)
  } catch {
    ElMessage.info(`音色 ID：${id}`)
  }
}

async function loadVoices() {
  loading.value = true
  try {
    const r = await api.voices()
    voices.value = r.voices || []
  } catch (e) {
    ElMessage.error(`音色列表加载失败：${e.message}`)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadVoices()
  await loadDefault()
})
onBeforeUnmount(() => {
  audioCache.forEach((v) => URL.revokeObjectURL(v.url))
  audioCache.clear()
})

/* ---------------- 顶部筛选 ---------------- */
const filter = reactive({ keyword: '', gender: '', age: '' })

const filtered = computed(() => {
  const kw = filter.keyword.trim().toLowerCase()
  return voices.value.filter((v) => {
    if (filter.gender && (v.gender || '') !== filter.gender) return false
    if (filter.age && (v.age || '') !== filter.age) return false
    if (kw) {
      const hay = `${v.name || ''} ${v.file || ''} ${v.id || ''}`.toLowerCase()
      if (!hay.includes(kw)) return false
    }
    return true
  })
})

const genderCount = computed(() => {
  const c = { female: 0, male: 0, neutral: 0, none: 0 }
  for (const v of voices.value) c[v.gender || 'none']++
  return c
})

function resetFilter() {
  filter.keyword = ''
  filter.gender = ''
  filter.age = ''
}

/* ---------------- 试听 ---------------- */
function isCurrent(v) {
  return player.value.id === v.id && !!player.value.url && !player.value.error
}

async function togglePlay(v) {
  if (isCurrent(v)) {
    stop()
    return
  }
  if (!v.exists) {
    ElMessage.warning('该音色的音频文件缺失，无法试听')
    return
  }
  stop()
  player.value = { id: v.id, name: v.name, url: '', size: 0, loading: true, error: '' }

  try {
    let cached = audioCache.get(v.id)
    if (!cached) {
      const resp = await fetch(api.voiceAudioUrl(v.id))
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const blob = await resp.blob()
      if (!blob.size) throw new Error('音频内容为空')
      cached = { url: URL.createObjectURL(blob), size: blob.size }
      audioCache.set(v.id, cached)
    }
    if (player.value.id !== v.id) return // 期间又点了别的音色
    player.value = { id: v.id, name: v.name, url: cached.url, size: cached.size, loading: false, error: '' }
  } catch (e) {
    if (player.value.id !== v.id) return
    player.value = { id: v.id, name: v.name, url: '', size: 0, loading: false, error: `拉取失败：${e.message}` }
    ElMessage.error(`音色「${v.name}」试听失败：${e.message}`)
  }
}

function stop() {
  player.value = { id: '', name: '', url: '', size: 0, loading: false, error: '' }
  playing.value = false
}

/* ---------------- 新增音色 ---------------- */
const addOpen = ref(false)
const addForm = reactive({
  file: null,
  name: '',
  gender: '',
  age: '',
  text: '',
  autoTranscribe: true,
  saving: false,
})

function openAdd() {
  addForm.file = null
  addForm.name = ''
  addForm.gender = ''
  addForm.age = ''
  addForm.text = ''
  addForm.autoTranscribe = true
  addOpen.value = true
}

function onPickFile(f) {
  addForm.file = f.raw
  if (!addForm.name) addForm.name = f.name.replace(/\.[^.]+$/, '')
}

async function submitAdd() {
  if (!addForm.file) {
    ElMessage.warning('请先选择音频文件')
    return
  }
  const needAsr = !addForm.text.trim() && addForm.autoTranscribe
  addForm.saving = true
  try {
    const r = await api.saveVoice({
      file: addForm.file,
      name: addForm.name,
      gender: addForm.gender,
      age: addForm.age,
      text: addForm.text,
      autoTranscribe: addForm.autoTranscribe,
    })
    voices.value = r.voices || voices.value
    addOpen.value = false
    const asr = r.asr || {}
    if (asr.ok) {
      ElMessage.success(
        `音色「${r.voice?.name || ''}」已加入音色库，并自动转写出参考文字稿（${asr.elapsed || 0}s）`,
      )
    } else if (needAsr && asr.error) {
      ElMessage.warning(
        `音色「${r.voice?.name || ''}」已加入音色库，但自动转写失败：${asr.error}（可稍后在编辑里手动转写）`,
      )
    } else {
      ElMessage.success(`音色「${r.voice?.name || ''}」已加入音色库`)
    }
  } catch (e) {
    ElMessage.error(`新增音色失败：${e.message}`)
  } finally {
    addForm.saving = false
  }
}

/* ---------------- 编辑音色 ---------------- */
const editOpen = ref(false)
const editForm = reactive({
  id: '',
  name: '',
  gender: '',
  age: '',
  text: '',
  textSource: '',
  saving: false,
  asrBusy: false,
})

function openEdit(v) {
  editForm.id = v.id
  editForm.name = v.name || ''
  editForm.gender = v.gender || ''
  editForm.age = v.age || ''
  editForm.text = v.text || ''
  editForm.textSource = v.text_source || ''
  editOpen.value = true
}

async function submitEdit() {
  editForm.saving = true
  try {
    const r = await api.updateVoice(editForm.id, {
      name: editForm.name,
      gender: editForm.gender,
      age: editForm.age,
      text: editForm.text,
    })
    voices.value = r.voices || voices.value
    if (player.value.id === editForm.id) player.value.name = r.voice?.name || player.value.name
    editOpen.value = false
    ElMessage.success('音色属性已更新')
  } catch (e) {
    ElMessage.error(`更新失败：${e.message}`)
  } finally {
    editForm.saving = false
  }
}

/** 用 ASR 重新转写该音色音频，结果直接写进参考文字稿 */
async function reTranscribe() {
  editForm.asrBusy = true
  try {
    const r = await api.transcribeVoice(editForm.id, true)
    voices.value = r.voices || voices.value
    const asr = r.asr || {}
    if (asr.ok) {
      editForm.text = asr.text || r.voice?.text || ''
      editForm.textSource = 'asr'
      ElMessage.success(`转写完成（${asr.elapsed || 0}s）`)
    } else {
      ElMessage.error(`转写失败：${asr.error || '未知错误'}`)
    }
  } catch (e) {
    ElMessage.error(`转写失败：${e.message}`)
  } finally {
    editForm.asrBusy = false
  }
}

/* ---------------- 删除音色 ---------------- */
async function remove(v) {
  try {
    await ElMessageBox.confirm(
      `将删除音色「${v.name}」${v.file ? `，并一并删除音频文件 ${v.file}` : ''}。此操作不可撤销，是否继续？`,
      '删除音色',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const r = await api.deleteVoice(v.id)
    voices.value = r.voices || voices.value
    if (player.value.id === v.id) stop()
    const cached = audioCache.get(v.id)
    if (cached) {
      URL.revokeObjectURL(cached.url)
      audioCache.delete(v.id)
    }
    ElMessage.success(`音色「${v.name}」已删除`)
  } catch (e) {
    ElMessage.error(`删除失败：${e.message}`)
  }
}

/* ---------------- 送到工作台使用 ---------------- */
function useInStudio(v) {
  router.push({ path: '/studio', query: { voice: v.id } })
}

/** 头像首字：取名称第一个字符 */
function initial(v) {
  const s = (v.name || v.file || v.id || '?').trim()
  return s.slice(0, 1).toUpperCase()
}
</script>

<template>
  <div>
    <!-- 顶部：筛选条 -->
    <div class="ctrl panel">
      <span class="hud hud--amber">VOICE LIBRARY</span>
      <el-input
        v-model="filter.keyword"
        size="small"
        class="fsearch"
        placeholder="搜索音色名称 / 文件名…"
        clearable
      />
      <el-select v-model="filter.gender" size="small" class="fsel" placeholder="性别" clearable>
        <el-option v-for="o in GENDER_OPTS" :key="o.v" :value="o.v" :label="o.l" />
      </el-select>
      <el-select v-model="filter.age" size="small" class="fsel" placeholder="年龄" clearable>
        <el-option v-for="o in AGE_OPTS" :key="o.v" :value="o.v" :label="o.l" />
      </el-select>
      <button class="btn btn--sm" @click="resetFilter">重置</button>
      <div class="defpick">
        <span class="hud defpick__label">默认音色</span>
        <el-select
          v-model="defaultVoice"
          size="small"
          class="fsel"
          placeholder="未设置（克隆时必传参考音频）"
          clearable
          filterable
          :loading="defaultSaving"
          @change="onDefaultChange"
        >
          <el-option
            v-for="v in voices"
            :key="v.id"
            :value="v.id"
            :label="v.name"
            :disabled="!v.exists"
          />
        </el-select>
      </div>
      <div class="spacer"></div>
      <span class="mono-sm">
        共 {{ voices.length }} 个 · 显示 {{ filtered.length }} 个 ·
        男 {{ genderCount.male }} / 女 {{ genderCount.female }}
      </span>
      <button class="btn btn--sm" :disabled="loading" @click="loadVoices">刷新</button>
      <button class="btn btn--sm btn--primary" @click="openAdd">＋ 新增音色</button>
    </div>

    <!-- 卡片网格：自适应行列 -->
    <div class="grid" v-loading="loading">
      <div class="vcard" v-for="v in filtered" :key="v.id" :class="[`vcard--${v.gender || 'none'}`, { 'vcard--playing': isCurrent(v), 'vcard--default': v.id === defaultVoice }]">
        <div class="vcard__top">
          <div class="avatar" :class="`avatar--${v.gender || 'none'}`">{{ initial(v) }}</div>
          <div class="vcard__meta">
            <div class="vcard__name" :title="v.name">{{ v.name }}</div>
            <div class="vcard__tags">
              <span class="tag" :class="`tag--g-${v.gender || 'none'}`">{{ genderLabel(v.gender) }}</span>
              <span class="tag" :class="`tag--a-${v.age || 'none'}`">{{ ageLabel(v.age) }}</span>
            </div>
          </div>
          <span v-if="v.id === defaultVoice" class="deftag" title="克隆模式未传参考音频时，默认使用该音色">默认</span>
          <span v-if="!v.exists" class="miss" title="索引里有记录，但音频文件缺失">缺文件</span>
        </div>

        <div class="vcard__file" :title="v.file">
          <span class="vcard__id mono-sm" title="点击复制音色 ID" @click.stop="copyId(v)">{{ v.id }}</span>
          <span class="vcard__fname">{{ v.file || '—' }}</span>
          <span class="vcard__size">{{ fmtSize(v.size) }}</span>
        </div>

        <div class="vcard__text" v-if="v.text" :title="v.text">
          <span class="tag" :class="v.text_source === 'asr' ? 'tag--asr' : 'tag--a-none'">
            {{ v.text_source === 'asr' ? 'ASR' : '文字稿' }}
          </span>
          <span class="vcard__ttext">{{ v.text }}</span>
        </div>

        <div class="vcard__ops">
          <button class="btn btn--sm btn--play" :disabled="!v.exists" @click="togglePlay(v)">
            {{ isCurrent(v) ? '停止' : '试听' }}
          </button>
          <button class="btn btn--sm" @click="openEdit(v)">编辑</button>
          <button class="btn btn--sm btn--primary" :disabled="!v.exists" @click="useInStudio(v)">
            工作台使用
          </button>
          <button class="btn btn--sm btn--danger" @click="remove(v)">删除</button>
        </div>
      </div>

      <div class="empty" v-if="!loading && !filtered.length">
        {{ voices.length ? '没有符合筛选条件的音色' : '音色库为空 —— 点击右上角「＋ 新增音色」上传一段音频' }}
      </div>
    </div>

    <!-- 试听台：按需加载，点击试听后才拉取音频并播放 -->
    <div class="panel player" v-if="player.id">
      <div class="player__hd">
        <span class="hud hud--amber">PREVIEW</span>
        <span class="player__title">{{ player.name }}</span>
        <div class="spacer"></div>
        <span class="mono-sm" v-if="player.size">{{ fmtSize(player.size) }}</span>
        <button class="btn btn--sm btn--ghost" @click="stop">关闭</button>
      </div>
      <div class="player__body">
        <div v-if="player.loading" class="mono-sm">正在加载音频…</div>
        <div v-else-if="player.error" class="player__err">{{ player.error }}</div>
        <template v-else>
          <audio
            :src="player.url"
            controls
            autoplay
            class="player__audio"
            @play="playing = true"
            @pause="playing = false"
            @ended="playing = false"
          ></audio>
          <div class="bars" :class="{ 'bars--on': playing }">
            <i v-for="n in 48" :key="n" :style="{ '--i': n }"></i>
          </div>
        </template>
      </div>
    </div>

    <!-- 新增音色 -->
    <el-dialog v-model="addOpen" title="新增音色" width="440px" append-to-body>
      <div class="dlg">
        <label class="dlg__row">
          <span class="dlg__label">音频文件</span>
          <el-upload
            :auto-upload="false"
            :limit="1"
            :on-change="onPickFile"
            accept=".wav,.mp3,.flac,.m4a,.ogg,.opus,.aac,.wma"
          >
            <button class="btn btn--sm">选择文件…</button>
            <span class="dlg__tip mono-sm" v-if="addForm.file">已选：{{ addForm.file.name }}</span>
          </el-upload>
        </label>
        <label class="dlg__row">
          <span class="dlg__label">音色名称</span>
          <el-input v-model="addForm.name" size="small" placeholder="例如：女声-新闻播报" />
        </label>
        <label class="dlg__row">
          <span class="dlg__label">性别</span>
          <el-select v-model="addForm.gender" size="small" style="width: 100%">
            <el-option v-for="o in GENDER_OPTS" :key="o.v" :value="o.v" :label="o.l" />
          </el-select>
        </label>
        <label class="dlg__row">
          <span class="dlg__label">年龄</span>
          <el-select v-model="addForm.age" size="small" style="width: 100%">
            <el-option v-for="o in AGE_OPTS" :key="o.v" :value="o.v" :label="o.l" />
          </el-select>
        </label>
        <label class="dlg__row">
          <span class="dlg__label">参考文字稿</span>
          <el-input
            v-model="addForm.text"
            type="textarea"
            :rows="2"
            size="small"
            placeholder="可留空：留空且勾选自动转写时，管家会用 ASR 转写这段音频并存进音色库"
          />
        </label>
        <label class="dlg__row">
          <span class="dlg__label">自动转写</span>
          <el-checkbox v-model="addForm.autoTranscribe" :disabled="!!addForm.text.trim()">
            未填写文字稿时，用 ASR 自动转写（转写结果入库，克隆时直接复用，省掉引擎侧重复转写）
          </el-checkbox>
        </label>
        <p class="mono-sm dlg__tip">
          保存后音频会复制到项目 voice/ 目录，并写入 voice/voice.json 索引。
          <br />
          自动转写首次需加载 Whisper 模型（约十几秒），转写文本随音色一起入库。
        </p>
      </div>
      <template #footer>
        <button class="btn btn--sm btn--ghost" @click="addOpen = false">取消</button>
        <button class="btn btn--sm btn--primary" :disabled="addForm.saving" @click="submitAdd">
          {{ addForm.saving ? '保存中…' : '保存' }}
        </button>
      </template>
    </el-dialog>

    <!-- 编辑音色属性 -->
    <el-dialog v-model="editOpen" title="编辑音色属性" width="420px" append-to-body>
      <div class="dlg">
        <label class="dlg__row">
          <span class="dlg__label">音色名称</span>
          <el-input v-model="editForm.name" size="small" />
        </label>
        <label class="dlg__row">
          <span class="dlg__label">性别</span>
          <el-select v-model="editForm.gender" size="small" style="width: 100%">
            <el-option v-for="o in GENDER_OPTS" :key="o.v" :value="o.v" :label="o.l" />
          </el-select>
        </label>
        <label class="dlg__row">
          <span class="dlg__label">年龄</span>
          <el-select v-model="editForm.age" size="small" style="width: 100%">
            <el-option v-for="o in AGE_OPTS" :key="o.v" :value="o.v" :label="o.l" />
          </el-select>
        </label>
        <label class="dlg__row">
          <span class="dlg__label">参考文字稿</span>
          <el-input
            v-model="editForm.text"
            type="textarea"
            :rows="2"
            size="small"
            placeholder="该音色音频对应的文字稿；克隆时管家会直接把它填进引擎的参考文字稿参数"
          />
          <div class="dlg__row">
            <el-button size="small" :loading="editForm.asrBusy" @click="reTranscribe">
              用 ASR 重新转写
            </el-button>
            <span class="mono-sm dlg__tip">
              来源：{{ editForm.textSource === 'asr' ? 'ASR 自动转写' : editForm.textSource === 'manual' ? '人工填写' : '暂无' }}
            </span>
          </div>
        </label>
        <p class="mono-sm dlg__tip">
          仅修改索引中的属性，不会改动音频文件。转写文本入库后，克隆时不再重复调用 ASR。
        </p>
      </div>
      <template #footer>
        <button class="btn btn--sm btn--ghost" @click="editOpen = false">取消</button>
        <button class="btn btn--sm btn--primary" :disabled="editForm.saving" @click="submitEdit">
          {{ editForm.saving ? '保存中…' : '保存' }}
        </button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ctrl {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 13px 16px;
  margin-bottom: 16px;
}

.fsearch {
  width: 220px;
}

.fsel {
  width: 118px;
}

/* ---- 卡片上的转写文字稿 ---- */
.vcard__text {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 2px 0 6px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-dim, #8b93a7);
}
.vcard__ttext {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.tag--asr {
  background: rgba(56, 189, 248, 0.14);
  color: #38bdf8;
  border: 1px solid rgba(56, 189, 248, 0.35);
}

/* ---- 默认音色选择 ---- */
.defpick {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-left: 10px;
  border-left: 1px solid var(--line);
}

.defpick__label {
  color: var(--cyan);
  white-space: nowrap;
}

.deftag {
  font-size: 10px;
  padding: 1px 5px;
  border: 1px solid var(--cyan);
  color: var(--cyan);
  border-radius: 3px;
  align-self: flex-start;
  white-space: nowrap;
}

.vcard--default {
  border-color: rgba(80, 200, 220, 0.45);
}

/* ---- 卡片网格：自适应行列 ---- */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(248px, 1fr));
  gap: 14px;
  align-items: stretch;
}

.vcard {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border: 1px solid var(--line);
  background: var(--panel-2, rgba(21, 26, 32, 0.55));
  border-radius: var(--radius);
  transition: border-color 0.18s var(--ease), transform 0.18s var(--ease);
}

.vcard:hover {
  border-color: var(--line-3);
  transform: translateY(-1px);
}

.vcard--playing {
  border-color: var(--amber-line);
  box-shadow: 0 0 0 1px var(--amber-line) inset;
}

.vcard__top {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* 头像：男蓝 / 女粉 / 中性灰 */
.avatar {
  width: 42px;
  height: 42px;
  flex: none;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-family: var(--font-display);
  font-size: 17px;
  font-weight: 700;
  color: #fff;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.35);
}

.avatar--male {
  background: linear-gradient(145deg, #2f7fd6, #1b4f96);
  box-shadow: 0 0 10px rgba(47, 127, 214, 0.45);
}

.avatar--female {
  background: linear-gradient(145deg, #e2689a, #a83a6b);
  box-shadow: 0 0 10px rgba(226, 104, 154, 0.45);
}

.avatar--neutral {
  background: linear-gradient(145deg, #6d7683, #454d57);
  box-shadow: 0 0 8px rgba(109, 118, 131, 0.35);
}

.avatar--none {
  background: linear-gradient(145deg, #4a525c, #2f353d);
  box-shadow: none;
  color: var(--text-dim);
}

.vcard__meta {
  min-width: 0;
  flex: 1;
}

.vcard__name {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.vcard__tags {
  display: flex;
  gap: 6px;
  margin-top: 4px;
}

.tag {
  font-size: 10.5px;
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid var(--line-3);
  color: var(--text-dim);
  white-space: nowrap;
}

.tag--g-male {
  color: #6aa8f0;
  border-color: rgba(47, 127, 214, 0.6);
}

.tag--g-female {
  color: #f08cb6;
  border-color: rgba(226, 104, 154, 0.6);
}

.tag--g-neutral,
.tag--g-none {
  color: var(--text-dim);
}

.tag--a-child,
.tag--a-teen {
  color: var(--cyan);
  border-color: rgba(80, 200, 220, 0.45);
}

.tag--a-young,
.tag--a-middle,
.tag--a-old {
  color: var(--amber-2);
  border-color: var(--amber-line);
}

.miss {
  font-size: 10px;
  padding: 1px 5px;
  border: 1px solid var(--red);
  color: var(--red);
  border-radius: 3px;
  align-self: flex-start;
}

.vcard__file {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-mute);
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.vcard__id {
  color: var(--cyan);
  opacity: 0.85;
  flex: none;
  cursor: copy;
}

.vcard__fname {
  overflow: hidden;
  text-overflow: ellipsis;
}

.vcard__size {
  color: var(--text-dim);
  flex: none;
}

.vcard__ops {
  margin-top: auto;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.empty {
  grid-column: 1 / -1;
  padding: 40px 12px;
  text-align: center;
  color: var(--text-dim);
  font-size: 13px;
}

/* ---- 试听台 ---- */
.player {
  margin-top: 16px;
  animation: rise 0.35s var(--ease) both;
}

.player__hd {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
}

.player__title {
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 600;
  color: var(--cyan);
}

.player__body {
  padding: 14px;
}

.player__err {
  font-size: 12.5px;
  color: #ffb0b0;
  line-height: 1.75;
}

.player__audio {
  width: 100%;
  height: 36px;
  filter: invert(0.92) hue-rotate(180deg) saturate(0.6);
}

.bars {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 34px;
  margin-top: 12px;
}

.bars i {
  flex: 1;
  height: calc(16% + var(--i) * 1.7%);
  background: var(--line-2);
  border-radius: 1px;
  transform-origin: bottom;
  transition: background 0.3s var(--ease);
}

.bars--on i {
  background: var(--amber);
  animation: eq 1s var(--ease) infinite;
  animation-delay: calc(var(--i) * -0.055s);
}

@keyframes eq {
  0%,
  100% {
    transform: scaleY(0.4);
  }
  50% {
    transform: scaleY(1);
  }
}

/* ---- 弹窗 ---- */
.dlg {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.dlg__row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.dlg__label {
  font-size: 12px;
  color: var(--text-dim);
  letter-spacing: 0.04em;
}

.dlg__tip {
  margin: 0;
  line-height: 1.6;
}
</style>

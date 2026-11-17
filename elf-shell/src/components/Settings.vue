<script setup lang="ts">
/**
 * 设置面板（docs/05 §5.2）：改名字/重捏脸/改兴趣/语音对话开关/清空重来；右键精灵唤出。
 * 语音开关 §11.28 由主界面悬浮麦克风迁入此处（用户嫌麦克风破坏画面）。
 * 捏脸选项与 onboarding 同源（SPRITE_OPTIONS）；保存走 POST /profile 全量覆盖。
 * 清数据=抹掉全部学习/档案回首次引导（保留 LLM key/SOUL.md/语音模型）。
 */
import { computed, onMounted, ref } from 'vue'
import { DEFAULT_SPRITE, SPRITE_OPTIONS, type SpriteConfig } from '../pet/renderer'
import { brainFetch } from '../brain'

export interface LlmConfigData {
  base_url: string
  api_key: string // 空=不改动；打码值（含 ****）也=不改动
  model_cheap: string
  model_strong: string
}

export interface SettingsData {
  elf_name: string
  sprite_config: SpriteConfig
  interests: { name: string; desc: string }[]
  feed_urls: string[]
  llm?: LlmConfigData
}

const props = defineProps<{ initial: SettingsData; voiceOn: boolean }>()
const emit = defineEmits<{
  save: [data: SettingsData]
  correct: [text: string] // 纠偏：直连 /correction，不改档案
  voiceToggle: [] // 语音对话开关（§11.28：主界面麦克风已删，迁入设置面板）
  reset: []
  close: []
  quit: []
}>()

const name = ref(props.initial.elf_name)
const cfg = ref<SpriteConfig>({ ...DEFAULT_SPRITE, ...props.initial.sprite_config })
// 兴趣编辑：一行一个「名字|描述（可省）」，比 onboarding 的 chip 更适合小面板
const interestText = ref(props.initial.interests.map((i) => `${i.name}|${i.desc}`).join('\n'))
// 内容源编辑：一行一个 RSS/Atom 地址，保存后 brain 立即按新源跑一轮（§3.4）
const feedText = ref(props.initial.feed_urls.join('\n'))
// §11.36 模型配置：打开面板时拉当前值（key 打码回显；留空=不改）
const llm = ref<LlmConfigData>({ base_url: '', api_key: '', model_cheap: '', model_strong: '' })
onMounted(async () => {
  try {
    const r = await brainFetch('/config/llm')
    if (!r.ok) return
    const c = await r.json()
    llm.value = {
      base_url: c.base_url ?? '',
      api_key: c.api_key ?? '',
      model_cheap: c.model_cheap ?? '',
      model_strong: c.model_strong ?? '',
    }
  } catch {
    /* 脑不在线：留空，用户可手填 */
  }
})
const confirmReset = ref(false)
const correction = ref('')

const parsedInterests = computed(() =>
  interestText.value
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      const [n, d] = l.split('|')
      return { name: n.trim(), desc: (d ?? n).trim() }
    }),
)
const parsedFeeds = computed(() =>
  feedText.value
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.startsWith('http')),
)
const canSave = computed(
  () => name.value.trim().length > 0 && parsedInterests.value.length >= 2,
)

/** 模型配置是否真的改了（全空/只留打码 key = 没改，不发请求）。 */
const llmDirty = computed(
  () =>
    !!(
      llm.value.base_url.trim() ||
      llm.value.model_cheap.trim() ||
      llm.value.model_strong.trim() ||
      (llm.value.api_key.trim() && !llm.value.api_key.includes('****'))
    ),
)

function save() {
  if (!canSave.value) return
  emit('save', {
    elf_name: name.value.trim(),
    sprite_config: { ...cfg.value },
    interests: parsedInterests.value,
    feed_urls: parsedFeeds.value, // 一行一个 RSS/Atom；清空=回默认源
    llm: llmDirty.value ? { ...llm.value } : undefined,
  })
}

/** §11.37 皮肤上传：读取图片 → 等比缩到 ≤256px → dataURL 存 cfg.skin（壳侧 localStorage 持有）。 */
function onSkinPick(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 允许重复选同一张
  if (!file) return
  const img = new Image()
  img.onload = () => {
    const MAX = 256
    const k = Math.min(1, MAX / Math.max(img.width, img.height))
    const c = document.createElement('canvas')
    c.width = Math.max(1, Math.round(img.width * k))
    c.height = Math.max(1, Math.round(img.height * k))
    c.getContext('2d')!.drawImage(img, 0, 0, c.width, c.height)
    cfg.value.skin = c.toDataURL('image/png')
    URL.revokeObjectURL(img.src)
  }
  img.src = URL.createObjectURL(file)
}

/** 纠偏：一句话告诉它「现在不一样了」，走 /correction 而不是全量保存。 */
function submitCorrection() {
  const text = correction.value.trim()
  if (!text) return
  emit('correct', text)
  correction.value = ''
}
</script>

<template>
  <div class="settings">
    <div class="s-head">
      <span class="s-title">设置</span>
      <button class="s-close" title="收起" @click="emit('close')">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round">
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
    </div>
    <div class="s-body">
    <div class="row">
      <span class="label">名字</span>
      <input v-model="name" class="text" maxlength="12" />
    </div>
    <div class="row">
      <span class="label">样子</span>
      <div class="option-row">
        <button
          v-for="c in SPRITE_OPTIONS.colors"
          :key="c.value"
          class="swatch"
          :class="{ active: cfg.color === c.value }"
          :style="{ background: c.value }"
          :title="c.name"
          @click="cfg.color = c.value"
        />
      </div>
      <div class="option-row">
        <button
          v-for="e in SPRITE_OPTIONS.eyes"
          :key="e.value"
          class="chip"
          :class="{ active: cfg.eyes === e.value }"
          @click="cfg.eyes = e.value"
        >
          {{ e.name }}
        </button>
      </div>
      <div class="option-row">
        <button
          v-for="a in SPRITE_OPTIONS.accessories"
          :key="a.value"
          class="chip"
          :class="{ active: cfg.accessory === a.value }"
          @click="cfg.accessory = a.value"
        >
          {{ a.name }}
        </button>
        <button
          v-for="s in SPRITE_OPTIONS.sizes"
          :key="s.value"
          class="chip"
          :class="{ active: cfg.size === s.value }"
          @click="cfg.size = s.value"
        >
          {{ s.name }}
        </button>
      </div>
    </div>
    <div class="row">
      <span class="label">皮肤（§11.37：上传图片替代光团形象，可随时移除）</span>
      <div class="skin-row">
        <img v-if="cfg.skin" :src="cfg.skin" class="skin-preview" alt="宠物皮肤预览" />
        <div v-else class="skin-preview skin-empty">默认光团</div>
        <label class="skin-upload">
          {{ cfg.skin ? '换一张' : '上传图片' }}
          <input type="file" accept="image/*" class="skin-input" @change="onSkinPick" />
        </label>
        <button v-if="cfg.skin" class="skin-remove" @click="cfg.skin = undefined">移除</button>
      </div>
    </div>
    <div class="row">
      <span class="label">语音对话</span>
      <div class="voice-row">
        <button
          class="voice-switch"
          :class="{ on: props.voiceOn }"
          role="switch"
          :aria-checked="props.voiceOn"
          @click="emit('voiceToggle')"
        >
          <span class="knob" />
        </button>
        <span class="voice-hint">
          {{ props.voiceOn ? '已开启：喊它的名字就能聊天' : '已关闭' }}
        </span>
      </div>
    </div>
    <div class="row">
      <span class="label">模型（它的脑子，OpenAI 兼容接口；改了保存立即生效）</span>
      <input v-model="llm.base_url" class="text mono" placeholder="服务地址，如 https://api.deepseek.com/v1" spellcheck="false" />
      <input v-model="llm.api_key" class="text mono" placeholder="API Key（留空 = 不改动）" spellcheck="false" />
      <div class="llm-row">
        <input v-model="llm.model_cheap" class="text mono" placeholder="日常模型（打标/闲聊）" spellcheck="false" />
        <input v-model="llm.model_strong" class="text mono" placeholder="思考模型（精排/长文）" spellcheck="false" />
      </div>
    </div>
    <div class="row">
      <span class="label">兴趣（一行一个，|后是可省的描述，至少 2 个）</span>
      <textarea v-model="interestText" class="area" rows="4" spellcheck="false" />
    </div>
    <div class="row">
      <span class="label">内容源（一行一个 RSS/Atom 地址，清空=回默认源）</span>
      <textarea v-model="feedText" class="area feeds" rows="5" spellcheck="false" />
    </div>
    <div class="row">
      <span class="label">纠正它（一句话：我现在更爱… / 别再推荐…）</span>
      <div class="corr-row">
        <input
          v-model="correction"
          class="text"
          placeholder="例：咖啡我现在只喝手冲"
          @keyup.enter="submitCorrection"
        />
        <button class="corr-btn" :disabled="!correction.trim()" @click="submitCorrection">
          纠正
        </button>
      </div>
    </div>
    </div>
    <div class="actions">
      <button class="primary" :disabled="!canSave" @click="save">保存</button>
      <button class="ghost" @click="emit('close')">取消</button>
      <button class="ghost" @click="emit('quit')">退出</button>
      <button v-if="!confirmReset" class="ghost danger" @click="confirmReset = true">
        清空重来…
      </button>
      <button v-else class="danger" @click="emit('reset')">确认清空精灵档案与记忆</button>
    </div>
  </div>
</template>

<style scoped>
/* 视觉令牌来自 index.html :root，与内容卡片/成长面板同一体系 */
.settings {
  display: flex;
  flex-direction: column;
  min-height: 0;
  max-height: 100%;
  font-size: 12.5px;
  color: var(--elf-ink);
}
.s-head {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px 10px;
}
.s-title {
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.s-close {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.08);
  color: var(--elf-ink-3);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.s-close:hover {
  background: rgba(255, 255, 255, 0.16);
  color: var(--elf-ink);
}
.s-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 0 16px 14px;
}
.row {
  margin-bottom: 14px;
}
.label {
  display: block;
  margin-bottom: 6px;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.03em;
  color: var(--elf-ink-3);
}
.text,
.area {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--elf-line-strong);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.05);
  color: var(--elf-ink);
  font-size: 12.5px;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.text {
  height: 34px;
  padding: 0 11px;
}
.area {
  padding: 9px 11px;
  line-height: 1.6;
  resize: none;
}
.area.feeds {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 11.5px;
}
.text.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 11.5px;
  margin-bottom: 6px;
}
.llm-row {
  display: flex;
  gap: 6px;
}
.llm-row .text {
  flex: 1;
  min-width: 0;
  margin-bottom: 0;
}
.text:focus,
.area:focus {
  border-color: rgba(255, 154, 61, 0.6);
  box-shadow: 0 0 0 3px rgba(255, 154, 61, 0.16);
}
.option-row {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-bottom: 7px;
}
.corr-row {
  display: flex;
  gap: 8px;
}
.voice-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.voice-switch {
  position: relative;
  width: 40px;
  height: 22px;
  flex: none;
  padding: 0;
  border: none;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.12);
  cursor: pointer;
  transition: background 0.2s;
}
.voice-switch .knob {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #f5efe6;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
  transition: transform 0.2s;
}
.voice-switch.on {
  background: linear-gradient(135deg, #ffc531, #ff7d1f);
}
.voice-switch.on .knob {
  transform: translateX(18px);
}
.voice-hint {
  font-size: 12px;
  color: var(--elf-ink-3);
}
.corr-row .text {
  flex: 1;
  min-width: 0;
}
/* §11.37 皮肤上传行 */
.skin-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.skin-preview {
  width: 44px;
  height: 44px;
  flex: none;
  object-fit: contain;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--elf-line-strong);
}
.skin-empty {
  display: grid;
  place-items: center;
  font-size: 10.5px;
  color: var(--elf-ink-3);
}
.skin-upload {
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid var(--elf-line-strong);
  background: rgba(255, 255, 255, 0.05);
  color: var(--elf-ink-2);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}
.skin-upload:hover {
  border-color: rgba(255, 154, 61, 0.45);
  color: var(--elf-accent);
}
.skin-input {
  display: none; /* label 代理点击，原生 input 藏起 */
}
.skin-remove {
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid var(--elf-line-strong);
  background: transparent;
  color: var(--elf-ink-3);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}
.skin-remove:hover {
  border-color: rgba(255, 107, 94, 0.5);
  color: var(--elf-danger);
}
.corr-btn {
  flex: none;
  height: 34px;
  padding: 0 14px;
  border: none;
  border-radius: 10px;
  background: rgba(255, 154, 61, 0.14);
  color: var(--elf-accent);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s;
}
.corr-btn:hover:not(:disabled) {
  background: rgba(255, 154, 61, 0.24);
}
.corr-btn:disabled {
  color: var(--elf-ink-3);
  background: rgba(255, 255, 255, 0.04);
  cursor: default;
}
.swatch {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 2px solid transparent;
  cursor: pointer;
  padding: 0;
  transition: box-shadow 0.15s, transform 0.1s;
}
.swatch:hover {
  transform: scale(1.08);
}
.swatch.active {
  border-color: transparent;
  box-shadow: 0 0 0 2px var(--elf-accent);
}
.chip {
  padding: 5px 11px;
  border-radius: 999px;
  border: 1px solid var(--elf-line-strong);
  background: rgba(255, 255, 255, 0.05);
  color: var(--elf-ink-2);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}
.chip:hover {
  border-color: rgba(255, 154, 61, 0.45);
  color: var(--elf-accent);
}
.chip.active {
  background: rgba(255, 154, 61, 0.12);
  border-color: rgba(255, 154, 61, 0.5);
  color: var(--elf-accent);
  font-weight: 600;
}
.actions {
  flex: none;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 12px 16px 14px;
  border-top: 1px solid var(--elf-line);
  background: linear-gradient(180deg, rgba(33, 26, 19, 0.3), #211a13);
}
.actions button {
  height: 34px;
  padding: 0 15px;
  border-radius: 11px;
  font-size: 12.5px;
  cursor: pointer;
  transition: filter 0.15s, background 0.15s;
}
.actions .primary {
  border: none;
  background: linear-gradient(180deg, #ffb35c, #ff7d1f);
  color: #2b1403;
  font-weight: 600;
  box-shadow: 0 6px 14px -6px rgba(255, 125, 31, 0.55);
}
.actions .primary:hover:not(:disabled) {
  filter: brightness(1.05);
}
.actions .primary:disabled {
  background: rgba(255, 255, 255, 0.06);
  color: var(--elf-ink-3);
  box-shadow: none;
  cursor: default;
}
.actions .ghost {
  border: 1px solid var(--elf-line-strong);
  background: rgba(255, 244, 230, 0.06);
  color: var(--elf-ink-2);
}
.actions .ghost:hover {
  background: rgba(255, 244, 230, 0.11);
}
.actions .ghost.danger {
  color: var(--elf-danger);
  border-color: rgba(226, 87, 74, 0.28);
}
.actions .ghost.danger:hover {
  background: rgba(226, 87, 74, 0.07);
}
.actions .danger {
  border: none;
  background: var(--elf-danger);
  color: #fff;
  font-weight: 600;
}
.actions .danger:hover {
  filter: brightness(1.06);
}
</style>

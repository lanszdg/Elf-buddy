<script setup lang="ts">
/**
 * 首次引导：认领小精灵（起名 + 捏脸 + 勾兴趣）。
 * 捏脸实时预览（docs/05 §1.5）：改参数即见活体精灵。
 * 提交后 brain 落 profile.json/interests.json 并后台跑首轮管线（POST /profile）。
 */
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { SpritePetRenderer } from '../pet/sprite'
import { DEFAULT_SPRITE, SPRITE_OPTIONS, type SpriteConfig } from '../pet/renderer'
import { brainFetch } from '../brain'

const emit = defineEmits<{
  done: [profile: { elf_name: string; sprite_config: SpriteConfig }]
}>()

const elfName = ref('')
const cfg = ref<SpriteConfig>({ ...DEFAULT_SPRITE })
const picked = ref<Set<string>>(new Set())
const customs = ref<string[]>([])
const customInput = ref('')
const submitting = ref(false)
// 内容源（§3.4）：默认全选 8 源，可关掉不喜欢的，可添自定义 RSS
const feedsPicked = ref<Set<string>>(new Set())
const customFeeds = ref<string[]>([])
const feedInput = ref('')

const previewCanvas = ref<HTMLCanvasElement | null>(null)
const preview = new SpritePetRenderer()
let previewReady = false

/** 预设兴趣（name + desc 供 LLM 打标理解语义）。 */
const PRESETS: { name: string; desc: string }[] = [
  { name: 'AI编程工具', desc: 'AI 辅助编程、代码生成、开发者效率' },
  { name: '独立游戏', desc: '独立游戏发售、预告、开发日志' },
  { name: '咖啡', desc: '精品咖啡、冲煮方法、器具' },
  { name: '数码硬件', desc: '新硬件发布、评测、装机' },
  { name: '科幻影视', desc: '科幻电影、剧集、世界观设定' },
  { name: '音乐', desc: '新专辑、音乐制作、演出' },
  { name: '运动健身', desc: '训练方法、装备、赛事' },
  { name: '投资理财', desc: '市场动态、理财方法、宏观' },
  { name: '摄影', desc: '器材、拍摄技巧、后期' },
  { name: '美食烹饪', desc: '食谱、探店、厨具' },
]

/** 预设内容源（与 brain config 默认 26 源一致；§3.4 引导页可选）。 */
const PRESET_FEEDS: { name: string; url: string }[] = [
  { name: '36氪', url: 'https://36kr.com/feed' },
  { name: '少数派', url: 'https://sspai.com/feed' },
  { name: '爱范儿', url: 'https://www.ifanr.com/feed' },
  { name: '极客公园', url: 'https://www.geekpark.net/rss' },
  { name: 'InfoQ', url: 'https://www.infoq.cn/feed' },
  { name: '奇客', url: 'https://www.solidot.org/index.rss' },
  { name: '博客园', url: 'https://www.cnblogs.com/rss' },
  { name: '开源中国', url: 'https://www.oschina.net/news/rss' },
  { name: 'OpenAI', url: 'https://openai.com/news/rss.xml' },
  { name: 'DeepMind', url: 'https://deepmind.google/blog/rss.xml' },
  { name: 'Hugging Face', url: 'https://huggingface.co/blog/feed.xml' },
  { name: 'Lilian Weng', url: 'https://lilianweng.github.io/index.xml' },
  { name: 'Ahead of AI', url: 'https://magazine.sebastianraschka.com/feed' },
  { name: 'Jay Alammar', url: 'https://jalammar.github.io/feed.xml' },
  { name: 'Latent Space', url: 'https://www.latent.space/feed' },
  { name: 'arXiv·AI', url: 'https://rss.arxiv.org/rss/cs.AI' },
  { name: 'arXiv·ML', url: 'https://rss.arxiv.org/rss/cs.LG' },
  { name: 'MIT科技评论', url: 'https://www.technologyreview.com/feed/' },
  { name: 'TechCrunch', url: 'https://techcrunch.com/feed/' },
  { name: 'The Verge', url: 'https://www.theverge.com/rss/index.xml' },
  { name: 'Ars Technica', url: 'https://arstechnica.com/?feed=rss2' },
  { name: 'Wired', url: 'https://www.wired.com/feed/rss' },
  { name: 'Import AI', url: 'https://importai.substack.com/feed' },
  { name: 'Rundown AI', url: 'https://www.therundown.ai/feed' },
  { name: 'TLDR AI', url: 'https://tldr.tech/api/rss/ai' },
]
PRESET_FEEDS.forEach((f) => feedsPicked.value.add(f.url)) // 默认全选

onMounted(async () => {
  window.elfShell.setOnboarding(true)
  await nextTick()
  if (previewCanvas.value) {
    await preview.init(previewCanvas.value)
    await preview.setModel(JSON.stringify(cfg.value))
    previewReady = true
  }
})

onUnmounted(() => preview.dispose())

// 捏脸参数变化 → 实时重建预览
watch(cfg, (v) => previewReady && preview.setModel(JSON.stringify(v)), { deep: true })

function toggle(name: string) {
  picked.value.has(name) ? picked.value.delete(name) : picked.value.add(name)
}

function addCustom() {
  const v = customInput.value.trim()
  if (v && !customs.value.includes(v) && customs.value.length < 5) {
    customs.value.push(v)
    picked.value.add(v)
  }
  customInput.value = ''
}

function toggleFeed(url: string) {
  feedsPicked.value.has(url) ? feedsPicked.value.delete(url) : feedsPicked.value.add(url)
}

function addFeed() {
  const v = feedInput.value.trim()
  if (v && v.startsWith('http') && !customFeeds.value.includes(v) && customFeeds.value.length < 5) {
    customFeeds.value.push(v)
    feedsPicked.value.add(v)
  }
  feedInput.value = ''
}

const canSubmit = () =>
  elfName.value.trim().length > 0 && picked.value.size >= 2 && !submitting.value

async function submit() {
  if (!canSubmit()) return
  submitting.value = true
  const interests = [...picked.value].map((name) => {
    const preset = PRESETS.find((p) => p.name === name)
    return preset ?? { name, desc: name }
  })
  const profile = {
    elf_name: elfName.value.trim(),
    sprite_config: cfg.value,
    interests,
    feed_urls: [...feedsPicked.value], // §3.4 自选内容源；空数组=brain 用默认源
  }
  try {
    await brainFetch('/profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(profile),
    })
  } finally {
    // brain 不可达也放行——profile 会在下次启动重试引导
    emit('done', { elf_name: profile.elf_name, sprite_config: cfg.value })
  }
}
</script>

<template>
  <div class="onboarding">
    <div class="card">
      <h1>认领你的小精灵</h1>
      <p class="sub">它会默默关注你感兴趣的世界，在你歇口气的时候讲给你听</p>

      <div class="pet-row">
        <div class="preview">
          <canvas ref="previewCanvas" class="preview-canvas" />
        </div>
        <label class="field name-field">
          <span>给它起个名字</span>
          <input v-model="elfName" maxlength="12" placeholder="比如：豆豆" />
        </label>
      </div>

      <div class="field">
        <span>捏出它的样子</span>
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
        </div>
        <div class="option-row">
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

      <div class="field">
        <span>你感兴趣的事（至少选 2 个）</span>
        <div class="chips">
          <button
            v-for="p in PRESETS"
            :key="p.name"
            class="chip"
            :class="{ active: picked.has(p.name) }"
            @click="toggle(p.name)"
          >
            {{ p.name }}
          </button>
        </div>
        <div class="custom-row">
          <input
            v-model="customInput"
            placeholder="自定义兴趣，回车添加"
            @keyup.enter="addCustom"
          />
        </div>
        <div v-if="customs.length" class="chips">
          <button
            v-for="c in customs"
            :key="c"
            class="chip active"
            @click="toggle(c)"
          >
            {{ c }} ×
          </button>
        </div>
      </div>

      <div class="field">
        <span>它从哪里看世界（内容源，可增减）</span>
        <div class="chips">
          <button
            v-for="f in PRESET_FEEDS"
            :key="f.url"
            class="chip"
            :class="{ active: feedsPicked.has(f.url) }"
            :title="f.url"
            @click="toggleFeed(f.url)"
          >
            {{ f.name }}
          </button>
        </div>
        <div class="custom-row">
          <input
            v-model="feedInput"
            placeholder="自定义 RSS 链接，回车添加"
            @keyup.enter="addFeed"
          />
        </div>
        <div v-if="customFeeds.length" class="chips">
          <button
            v-for="u in customFeeds"
            :key="u"
            class="chip active"
            :title="u"
            @click="toggleFeed(u)"
          >
            {{ u.replace(/^https?:\/\//, '').slice(0, 24) }} ×
          </button>
        </div>
      </div>

      <button class="go" :disabled="!canSubmit()" @click="submit">
        {{ submitting ? '正在唤醒…' : `让${elfName.trim() || '它'}来见我` }}
      </button>
      <p v-if="submitting" class="hint">它正在去了解你的世界，第一次准备内容需要一两分钟</p>
    </div>
  </div>
</template>

<style scoped>
/* 与桌宠卡片同一套视觉语言：暖白底 + 靛蓝强调 + 薄荷点缀 */
.onboarding {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background:
    radial-gradient(120% 80% at 50% 0%, #eef2ff 0%, rgba(238, 242, 255, 0) 62%),
    linear-gradient(170deg, #fbfcff 0%, #f5f7fd 100%);
  border-radius: 22px;
  box-shadow:
    0 30px 60px -28px rgba(20, 30, 60, 0.35),
    0 0 0 1px rgba(20, 30, 60, 0.06);
  color: var(--elf-ink);
}
.card {
  width: 86%;
  max-height: 90%;
  overflow-y: auto;
  padding: 4px;
}
h1 {
  margin: 0 0 6px;
  font-size: 21px;
  font-weight: 700;
  letter-spacing: 0.01em;
}
.sub {
  margin: 0 0 18px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--elf-ink-2);
}
.pet-row {
  display: flex;
  gap: 16px;
  align-items: center;
  margin-bottom: 18px;
}
.preview {
  width: 116px;
  height: 116px;
  flex-shrink: 0;
  border-radius: 18px;
  background: radial-gradient(circle at 50% 60%, #ffffff 0%, #eef2ff 72%);
  border: 1px solid var(--elf-line-strong);
  box-shadow: inset 0 1px 0 #fff, 0 10px 22px -16px rgba(20, 30, 60, 0.4);
  overflow: hidden;
}
.preview-canvas {
  width: 100%;
  height: 100%;
  display: block;
}
.name-field {
  flex: 1;
  margin-bottom: 0;
}
.field {
  display: block;
  margin-bottom: 18px;
}
.field > span {
  display: block;
  margin-bottom: 8px;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.03em;
  color: var(--elf-ink-3);
}
input {
  width: 100%;
  box-sizing: border-box;
  height: 38px;
  padding: 0 12px;
  border-radius: 10px;
  border: 1px solid var(--elf-line-strong);
  background: #fff;
  color: var(--elf-ink);
  font-size: 13.5px;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
input:focus {
  border-color: rgba(91, 124, 250, 0.55);
  box-shadow: 0 0 0 3px rgba(91, 124, 250, 0.12);
}
.option-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.swatch {
  width: 26px;
  height: 26px;
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
  border-color: #fff;
  box-shadow: 0 0 0 2px var(--elf-accent);
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.chip {
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid var(--elf-line-strong);
  background: #fff;
  color: var(--elf-ink-2);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}
.chip:hover {
  border-color: rgba(91, 124, 250, 0.4);
  color: var(--elf-accent);
}
.chip.active {
  background: rgba(91, 124, 250, 0.1);
  border-color: rgba(91, 124, 250, 0.45);
  color: var(--elf-accent);
  font-weight: 600;
}
.custom-row {
  margin-top: 4px;
}
.go {
  width: 100%;
  height: 42px;
  margin-top: 4px;
  border: none;
  border-radius: 12px;
  background: linear-gradient(180deg, #6a8bff, #5474f0);
  color: #fff;
  font-size: 14.5px;
  font-weight: 600;
  letter-spacing: 0.02em;
  cursor: pointer;
  box-shadow: 0 10px 20px -10px rgba(84, 116, 240, 0.85);
  transition: filter 0.15s;
}
.go:hover:not(:disabled) {
  filter: brightness(1.05);
}
.go:disabled {
  background: rgba(23, 32, 56, 0.1);
  color: var(--elf-ink-3);
  box-shadow: none;
  cursor: not-allowed;
}
.hint {
  margin-top: 12px;
  text-align: center;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--elf-ink-3);
}
</style>

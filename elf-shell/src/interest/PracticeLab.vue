<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { simulateSpring, DEFAULT_DAMPING, DEFAULT_STIFFNESS } from '../pet/spring'
import { download, saveInterest } from './api'

const emit = defineEmits<{ saved: [message: string] }>()
const stiffness = ref(DEFAULT_STIFFNESS)
const damping = ref(DEFAULT_DAMPING)
const strength = ref(0.45)
const selected = ref(1)
const note = ref('')
const started = ref(false)
const frame = ref(0)
const saving = ref(false)
const error = ref('')
let animation = 0
let drag: { pointer: number; start: number; time: number } | undefined

const variants = computed(() => [
  { title: '轻阻尼', damping: Math.max(1, damping.value - 4), color: '#97613e' },
  { title: '当前设置', damping: damping.value, color: '#596543' },
  { title: '强阻尼', damping: damping.value + 4, color: '#776488' },
].map(option => ({ ...option, ...simulateSpring(stiffness.value, option.damping, strength.value) })))

function play() {
  cancelAnimationFrame(animation)
  frame.value = 0
  started.value = true
  const begin = performance.now()
  const render = (now: number) => {
    frame.value = Math.min(600, Math.floor((now - begin) * 0.12))
    if (frame.value < 600) animation = requestAnimationFrame(render)
  }
  animation = requestAnimationFrame(render)
}
function beginDrag(event: PointerEvent) {
  if (event.button !== 0) return
  (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  drag = { pointer: event.pointerId, start: event.clientX, time: performance.now() }
}
function releaseDrag(event: PointerEvent) {
  if (drag?.pointer !== event.pointerId) return
  const speed = Math.abs(event.clientX - drag.start) / Math.max(80, performance.now() - drag.time)
  strength.value = Math.max(0.15, Math.min(1, 0.18 + speed * 0.35))
  drag = undefined
  play()
}
function chosen() {
  const picked = variants.value[selected.value]
  return { stiffness: stiffness.value, damping: picked.damping, strength: Number(strength.value.toFixed(2)),
    rebounds: picked.swings, settleSeconds: Number(picked.settle.toFixed(2)), note: note.value.trim() }
}
async function save() {
  saving.value = true
  error.value = ''
  try {
    await saveInterest('practice', '拖拽手感 · ' + variants.value[selected.value].title, chosen())
    emit('saved', '已保存手感参数，可在成果里导出。')
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    saving.value = false
  }
}
onUnmounted(() => cancelAnimationFrame(animation))
</script>

<template>
  <div class="stack">
    <p class="muted">向一侧拖动任意精灵再松手，三个角色会用同样的初始摆幅回正。摆动穿过中心且超过 0.015 弧度，记为一次回摆。</p>
    <div class="controls">
      <label>弹簧刚度 {{ stiffness }} <input v-model.number="stiffness" type="range" min="40" max="230" step="5" /></label>
      <label>阻尼 {{ damping }} <input v-model.number="damping" type="range" min="5" max="18" /></label>
      <label>初始摆幅 {{ strength.toFixed(2) }} <input v-model.number="strength" type="range" min="0.15" max="1" step="0.05" /></label>
      <button type="button" class="primary" @click="play">同时释放</button>
    </div>
    <div class="lab-grid">
      <article v-for="(variant, index) in variants" :key="variant.title" class="card lab-card" :class="{ chosen: selected === index }">
        <h3>{{ variant.title }}</h3>
        <button class="swing-pad" type="button" :aria-label="'拖动并释放' + variant.title" @pointerdown="beginDrag" @pointerup="releaseDrag" @pointercancel="drag = undefined" @keydown.enter.prevent="play" @keydown.space.prevent="play">
          <span class="swing-pet" :style="{ '--pet-tint': variant.color, transform: 'rotate(' + (started ? variant.frames[frame] : 0) + 'rad)' }"><i></i></span>
        </button>
        <div class="metrics"><span>阻尼 <b>{{ variant.damping }}</b></span><span>回摆 <b>{{ variant.swings }} 次</b></span><span>归位 <b>{{ variant.settle.toFixed(2) }} 秒</b></span></div>
        <button type="button" :aria-pressed="selected === index" @click="selected = index">选用这组参数</button>
      </article>
    </div>
    <p class="hint" v-if="variants[selected].swings">这组会回摆 {{ variants[selected].swings }} 次。想减少摆动，可调高一点阻尼重试。</p>
    <p class="hint" v-else>这组没有明显回摆。想增加一点弹性，可调低阻尼重试。</p>
    <label>你喜欢它的原因 <input v-model="note" maxlength="400" placeholder="例如落地干脆，耳朵还有一点回摆" /></label>
    <div class="actions">
      <button class="primary" :disabled="saving" @click="save">保存结果</button>
      <button @click="download('elf-spring.json', JSON.stringify(chosen(), null, 2), 'application/json')">导出参数</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </div>
</template>

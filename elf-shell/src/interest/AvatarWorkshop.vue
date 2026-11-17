<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { EAR_AVATAR, ROUND_AVATAR, AVATAR_KEY, parseAvatar, type Avatar } from '../pet/avatar'
import { DEFAULT_SPRITE } from '../pet/renderer'
import { SpritePetRenderer } from '../pet/sprite'
import { interestApi, type Item } from './api'

const emit = defineEmits<{ saved: [message: string] }>()
const stored = parseAvatar(localStorage.getItem(AVATAR_KEY))
const avatar = ref<Avatar>({ ...(stored ?? EAR_AVATAR) })
const canvas = ref<HTMLCanvasElement>()
const error = ref('')
const drawingName = ref('')
const parameters = ref<Item[]>([])
const springId = ref('')
const building = ref(false)
const controls = computed(() => avatar.value.kind === 'drawing' ? [
  { key: 'shoulder', label: '肩部转轴上下', min: -0.14, max: 0.14, step: 0.01 },
  { key: 'hip', label: '髋部转轴上下', min: -0.14, max: 0.14, step: 0.01 },
] as const : [
  { key: 'width', label: '身体宽度', min: 0.65, max: 1.4, step: 0.01 },
  { key: 'height', label: '身体高度', min: 0.68, max: 1.25, step: 0.01 },
  { key: 'eyes', label: '眼距', min: 0.65, max: 1.45, step: 0.01 },
  { key: 'ears', label: '耳朵长度', min: 0, max: 1.6, step: 0.05 },
  { key: 'arms', label: '手臂长度', min: 0.3, max: 1.6, step: 0.05 },
  { key: 'legs', label: '腿的长度', min: 0.3, max: 1.6, step: 0.05 },
  { key: 'tail', label: '尾巴长度', min: 0, max: 1.5, step: 0.05 },
] as const)
const pet = new SpritePetRenderer()
let initialized = false
let requested = false
let alive = true
let last: { x: number; y: number; time: number } | undefined

async function render() {
  requested = true
  if (building.value || !initialized) return
  building.value = true
  try {
    while (requested && alive) {
      requested = false
      const cfg = { ...DEFAULT_SPRITE, color: avatar.value.color, avatar: avatar.value,
        skin: avatar.value.kind === 'drawing' ? avatar.value.drawing : undefined }
      await pet.setModel(JSON.stringify(cfg))
    }
  } catch { if (alive) error.value = '形象预览加载失败。' }
  finally {
    building.value = false
    if (!alive && initialized) pet.dispose()
  }
}
watch(avatar, () => void render(), { deep: true })
onMounted(async () => {
  if (!canvas.value) return
  void interestApi<Item[]>('/items?kind=practice').then(result => { parameters.value = result }).catch(() => {})
  try {
    await pet.init(canvas.value)
    initialized = true
    building.value = false
    if (alive) await render()
    else pet.dispose()
  } catch { if (alive) error.value = '角色渲染初始化失败，请通过 Electron 桌宠打开工坊。' }
})
onUnmounted(() => {
  alive = false
  if (initialized && !building.value) pet.dispose()
})

function preset(value: Avatar) {
  avatar.value = { ...value }
  error.value = ''
}
async function importDrawing(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  error.value = ''
  try {
    if (file.size > 20 * 1024 * 1024) throw new Error('请选用不超过 20 MB 的图片')
    const image = await createImageBitmap(file)
    try {
      const output = document.createElement('canvas')
      const scale = Math.min(1, 400 / Math.max(image.width, image.height))
      output.width = Math.max(1, Math.round(image.width * scale))
      output.height = Math.max(1, Math.round(image.height * scale))
      output.getContext('2d')!.drawImage(image, 0, 0, output.width, output.height)
      const drawing = output.toDataURL('image/webp', 0.86)
      avatar.value = { ...ROUND_AVATAR, kind: 'drawing', drawing }
      drawingName.value = file.name
    } finally { image.close() }
  } catch (err) { error.value = (err as Error).message || '无法读取这张画' }
}
function useParameters() {
  const item = parameters.value.find(row => row.id === springId.value)
  if (!item) return
  avatar.value.stiffness = Number(item.data.stiffness)
  avatar.value.damping = Number(item.data.damping)
}
function start(event: PointerEvent) {
  if (event.button !== 0) return
  const target = event.currentTarget as HTMLElement
  target.setPointerCapture(event.pointerId)
  const box = target.getBoundingClientRect()
  pet.grab((event.clientX - box.left - box.width / 2) / 42)
  last = { x: event.clientX, y: event.clientY, time: performance.now() }
}
function move(event: PointerEvent) {
  if (!last) return
  const time = performance.now()
  const elapsed = Math.max(12, time - last.time) / 1000
  pet.dragVelocity((event.clientX - last.x) / elapsed, (event.clientY - last.y) / elapsed)
  last = { x: event.clientX, y: event.clientY, time }
}
function drop() {
  if (last) { pet.releaseGrab(); pet.land(0.26) }
  last = undefined
}
function apply() {
  const value = JSON.stringify(avatar.value)
  if (!parseAvatar(value) || !window.elfShell?.applyAvatar) { error.value = '请在桌宠工坊中应用有效形象。'; return }
  try {
    localStorage.setItem(AVATAR_KEY, value)
    window.elfShell.applyAvatar(value)
    emit('saved', '已应用到桌面精灵。')
  } catch { error.value = '本地存储空间不足，请使用更小的图片。' }
}
function reset() {
  if (!window.elfShell?.applyAvatar) return
  localStorage.removeItem(AVATAR_KEY)
  window.elfShell.applyAvatar('')
  emit('saved', '已恢复原有桌宠设置。')
}
</script>

<template>
  <div class="two-col avatar-layout">
    <article class="card stack dense">
      <div class="avatar-preview" @pointerdown="start" @pointermove="move" @pointerup="drop" @pointercancel="drop" @lostpointercapture="drop"><canvas ref="canvas" /></div>
      <div class="actions"><button @click="pet.walk()">走两步</button><button @click="pet.attract()">跳一下</button><button @click="pet.poke()">戳一下</button></div>
      <p class="muted">左右拖动预览来检查耳朵、手脚和尾巴的摆动。</p>
    </article>
    <div class="stack">
      <div class="actions"><button @click="preset(ROUND_AVATAR)">圆身体</button><button @click="preset(EAR_AVATAR)">长耳朵</button></div>
      <label>导入自己的画<input type="file" accept="image/png,image/jpeg,image/webp" @change="importDrawing" /></label>
      <p v-if="avatar.kind === 'drawing'" class="muted">{{ drawingName }} 手脚轮廓要清楚。能分离的手脚可调整转轴后预览。</p>
      <label v-else>身体颜色<input v-model="avatar.color" type="color" /></label>
      <label v-for="setting in controls" :key="setting.key">{{ setting.label }} {{ avatar[setting.key].toFixed(2) }}
        <input v-model.number="avatar[setting.key]" type="range" :min="setting.min" :max="setting.max" :step="setting.step" />
      </label>
      <label v-if="parameters.length">使用保存过的手感参数<select v-model="springId" @change="useParameters"><option value="">保持当前</option><option v-for="item in parameters" :key="item.id" :value="item.id">{{ item.title }} · {{ item.data.stiffness }} / {{ item.data.damping }}</option></select></label>
      <div class="actions"><button class="primary" @click="apply">应用到桌宠</button><button @click="reset">恢复原有形象</button></div>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </div>
  </div>
</template>

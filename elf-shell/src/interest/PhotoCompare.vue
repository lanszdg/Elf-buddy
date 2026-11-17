<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue'
import { FULL, loadPhoto, measure, savedPhoto, type Photo, type Region } from './photo'
import { interestApi, saveInterest, type Item } from './api'

const props = defineProps<{ recordId?: string }>()
const emit = defineEmits<{ saved: [message: string] }>()
const left = shallowRef<Photo>()
const right = shallowRef<Photo>()
const area = ref<Region>({ ...FULL })
const activeRegion = ref<Region>()
const levelsA = computed(() => left.value && measure(left.value, area.value))
const levelsB = computed(() => right.value && measure(right.value, area.value))
const goal = ref<'bright' | 'dark'>('bright')
const preference = ref('')
const note = ref('')
const error = ref('')
const saving = ref(false)
const regionStyle = computed(() => {
  const { x, y, w, h } = activeRegion.value ?? area.value
  return { left: x * 100 + '%', top: y * 100 + '%', width: w * 100 + '%', height: h * 100 + '%' }
})
let drag: { x: number; y: number; pointer: number } | undefined
let decodeA = 0, decodeB = 0

const hint = computed(() => {
  if (!levelsA.value || !levelsB.value) return ''
  const diff = levelsA.value[goal.value] - levelsB.value[goal.value]
  if (Math.abs(diff) < 0.5) return '这一项差异不到 0.5 个百分点。可以标出更小的对应区域，再比较局部。'
  const better = diff < 0 ? 'A' : 'B'
  return goal.value === 'bright'
    ? better + ' 在选区内的近白像素少 ' + Math.abs(diff).toFixed(1) + ' 个百分点。先放大看亮部纹理。如果细节有用，可以从这张开始提一点阴影。'
    : better + ' 在选区内的近黑像素少 ' + Math.abs(diff).toFixed(1) + ' 个百分点。先放大看暗部，再确认提亮后是否把亮部冲淡。'
})

async function read(event: Event, side: 'a' | 'b') {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const mark = side === 'a' ? ++decodeA : ++decodeB
  error.value = ''
  try {
    const photo = await loadPhoto(file)
    if (mark !== (side === 'a' ? decodeA : decodeB)) return
    if (side === 'a') left.value = photo
    else right.value = photo
  } catch (err) { error.value = (err as Error).message }
}
function coordinate(event: PointerEvent) {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const unit = (value: number) => Math.min(1, Math.max(0, value))
  return { x: unit((event.clientX - rect.left) / rect.width), y: unit((event.clientY - rect.top) / rect.height) }
}
function startRegion(event: PointerEvent) {
  if (event.button !== 0) return
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  drag = { ...coordinate(event), pointer: event.pointerId }
}
function moveRegion(event: PointerEvent) {
  if (!drag || event.pointerId !== drag.pointer) return
  const { x, y } = coordinate(event)
  activeRegion.value = { x: Math.min(x, drag.x), y: Math.min(y, drag.y), w: Math.abs(x - drag.x), h: Math.abs(y - drag.y) }
}
function finishRegion(event: PointerEvent) {
  moveRegion(event)
  const selected = activeRegion.value
  if (selected && selected.w >= 0.02 && selected.h >= 0.02) area.value = selected
  cancelRegion()
}
function cancelRegion() { drag = undefined; activeRegion.value = undefined }
function bar(value: number, histogram: number[]) {
  return (100 * value / Math.max(1, ...histogram)).toFixed(1) + '%'
}
async function save() {
  if (!left.value || !right.value) return
  saving.value = true
  error.value = ''
  try {
    await saveInterest('photo', '照片对照 · ' + left.value.name.slice(0, 70), {
      filenames: [left.value.name, right.value.name], previews: [left.value.url, right.value.url],
      area: area.value, goal: goal.value, preference: preference.value, note: note.value.trim(),
      result: hint.value, levels: [levelsA.value, levelsB.value],
    })
    emit('saved', '已保存两张预览和对照结果。')
  } catch (err) { error.value = (err as Error).message }
  finally { saving.value = false }
}
watch(() => props.recordId, async (id, _, onCleanup) => {
  if (!id) return
  let current = true
  onCleanup(() => { current = false })
  try {
    const item = await interestApi<Item>('/items/' + id)
    const urls = item.data.previews as string[] | undefined
    const names = item.data.filenames as string[]
    if (!urls?.length) return
    const [a, b] = await Promise.all(urls.map((url, i) => savedPhoto(url, names[i])))
    if (!current) return
    left.value = a
    right.value = b
    area.value = item.data.area as Region ?? { ...FULL }
    goal.value = item.data.goal === 'dark' ? 'dark' : 'bright'
    preference.value = String(item.data.preference || '')
    note.value = String(item.data.note || '')
  } catch (err) { if (current) error.value = (err as Error).message }
}, { immediate: true })
</script>

<template>
  <div class="stack">
    <p class="muted">用同一取景的两张照片对照。在任意一张图上拖出矩形，两张图按相对位置对齐。数据来自本地缩放预览，像素接近白或黑只能作为细节检查线索。</p>
    <div class="controls">
      <label>观察目标<select v-model="goal"><option value="bright">保留亮部细节</option><option value="dark">看清暗处</option></select></label>
      <button @click="area = { ...FULL }">比较整张照片</button>
    </div>
    <div class="two-col">
      <article v-for="(photo, index) in [left, right]" :key="index" class="card stack">
        <label>照片 {{ index === 0 ? 'A' : 'B' }} <input type="file" accept="image/jpeg,image/png,image/webp,image/bmp,image/avif" @change="read($event, index === 0 ? 'a' : 'b')" /></label>
        <div v-if="photo" class="photo-frame" @pointerdown="startRegion" @pointermove="moveRegion" @pointerup="finishRegion" @pointercancel="cancelRegion">
          <img :src="photo.url" :alt="photo.name" draggable="false" />
          <div class="photo-region" :style="regionStyle"></div>
        </div>
        <template v-for="levels in [index === 0 ? levelsA : levelsB]">
          <div v-if="levels" :key="index" class="stack dense">
            <div class="histogram" role="img" aria-label="选区亮度分布，从暗到亮"><i v-for="(value, i) in levels.histogram" :key="i" :style="{ height: bar(value, levels.histogram) }" /></div>
            <div class="metrics"><span>近白 <b>{{ levels.bright.toFixed(1) }}%</b></span><span>近黑 <b>{{ levels.dark.toFixed(1) }}%</b></span><span>亮度 <b>{{ Math.round(levels.mean) }} / 255</b></span></div>
          </div>
        </template>
      </article>
    </div>
    <p v-if="hint" class="hint">{{ hint }}</p>
    <div v-if="hint" class="controls">
      <label>保留哪张<select v-model="preference"><option value="">还没决定</option><option>A</option><option>B</option><option>两张都留</option></select></label>
      <label class="grow">记录原因<input v-model="note" maxlength="500" placeholder="例如 A 的窗外树叶更有层次" /></label>
      <button class="primary" :disabled="saving" @click="save">保存对照</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </div>
</template>

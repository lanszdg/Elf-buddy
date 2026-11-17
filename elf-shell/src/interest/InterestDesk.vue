<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { interestTools, isInterestTool, type InterestTool } from './toolRoutes'
import PracticeLab from './PracticeLab.vue'
import PhotoCompare from './PhotoCompare.vue'
import AvatarWorkshop from './AvatarWorkshop.vue'
import { interestApi, saveInterest, dateText, httpUrl, download, defaultDue, type Item, type Kind } from './api'
import { exportEvent, parseCalendar } from './calendar'
import './interest.css'

const query = new URLSearchParams(location.search)
const initialSection = query.get('section')
const section = ref<InterestTool>(isInterestTool(initialSection) ? initialSection : 'handoff')
const current = computed(() => interestTools[section.value])
watch(current, tool => { document.title = tool.name + ' · Elf Buddy' }, { immediate: true })
const items = ref<Item[]>([])
const status = ref('')
const error = ref('')
const busy = ref(false)
const viewingPhoto = ref<string>()
let unsubscribe: (() => void) | undefined
let poll = 0

async function refresh() {
  items.value = await interestApi<Item[]>('/items')
}
async function action(run: () => Promise<void>) {
  if (busy.value) return
  error.value = ''
  status.value = ''
  busy.value = true
  try { await run() }
  catch (err) { error.value = err instanceof Error ? err.message : '操作失败' }
  finally { busy.value = false }
}
function report(message: string) {
  status.value = message
  void refresh().catch(err => { error.value = err.message })
}
onMounted(() => {
  unsubscribe = window.elfShell?.onInterestSection(value => {
    if (!isInterestTool(value)) return
    section.value = value
    status.value = ''
    error.value = ''
    void refresh().catch(() => {})
  })
  void action(refresh)
  poll = window.setInterval(() => { if (!busy.value) void refresh().catch(() => {}) }, 25_000)
})
onUnmounted(() => { unsubscribe?.(); clearInterval(poll) })

function entries(kind: Kind) { return items.value.filter(item => item.kind === kind) }
const field = (item: Item, key: string) => String(item.data[key] ?? '')
function checkedUrl(value: string) {
  const url = httpUrl(value)
  if (value.trim() && !url) throw new Error('请输入有效的 HTTP 或 HTTPS 链接')
  return url
}
async function openLink(value: unknown) {
  const url = httpUrl(value)
  if (!url) return
  if (window.elfShell?.openInterestLink) {
    try { await window.elfShell.openInterestLink(url) }
    catch { error.value = '系统无法打开这个链接' }
  } else window.open(url, '_blank', 'noopener,noreferrer')
}
async function remove(item: Item) {
  await action(async () => { await interestApi('/items/' + item.id, 'DELETE'); await refresh(); status.value = '已删除。' })
}

const moods = [
  { id: 'relax', name: '放松' }, { id: 'learn', name: '学一点' }, { id: 'create', name: '动手做' },
]
const formats = [
  { id: 'read', name: '看文章 / 书' }, { id: 'watch', name: '看视频' }, { id: 'play', name: '玩游戏' }, { id: 'make', name: '动手' },
]
const nameOf = (options: typeof moods, id: string) => options.find(option => option.id === id)?.name || id
const pickForm = ref({ title: '', minutes: 20, mood: 'relax', format: 'read', url: '', note: '' })
const freeMinutes = ref(25)
const nowMood = ref('relax')
const nowFormat = ref('')
const consider = ref(false)
const suggestions = computed(() => entries('pick').filter(item => !item.done &&
  Number(item.data.minutes) <= freeMinutes.value && item.data.mood === nowMood.value &&
  (!nowFormat.value || item.data.format === nowFormat.value))
  .sort((a, b) => Number(b.data.minutes) - Number(a.data.minutes)).slice(0, 2))
async function addPick() {
  await action(async () => {
    const { title, minutes, mood, format, url, note } = pickForm.value
    await saveInterest('pick', title, { minutes, mood, format, url: checkedUrl(url), note: note.trim() })
    pickForm.value.title = ''
    pickForm.value.url = ''
    pickForm.value.note = ''
    await refresh()
    status.value = '已加入兴趣清单。'
  })
}
async function finish(item: Item, done = true) {
  await action(async () => {
    await interestApi('/items/' + item.id, 'PATCH', { done })
    await refresh()
    status.value = done ? '已标记完成。' : '已恢复。'
  })
}

interface Source { title: string; url: string; text: string }
interface Evidence { id: number; title: string; url: string; excerpts: string[] }
interface Research { evidence: Evidence[]; findings: { text: string; sources: number[] }[]; mode: string }
const researchText = ref('')
const sources = ref<Source[]>(Array.from({ length: 2 }, () => ({ title: '', url: '', text: '' })))
const useModel = ref(false)
const comparison = ref<Research>()
const comparedQuestion = ref('')
async function compare() {
  await action(async () => {
    const question = researchText.value.trim()
    const prepared = sources.value.map(source => ({ title: source.title.trim(), url: checkedUrl(source.url), text: source.text.trim() }))
    comparison.value = await interestApi<Research>('/compare', 'POST', { question, sources: prepared, use_model: useModel.value })
    comparedQuestion.value = question
  })
}
async function saveComparison() {
  if (!comparison.value) return
  const snapshot = comparison.value
  await action(async () => {
    await saveInterest('research', comparedQuestion.value.slice(0, 180), { question: comparedQuestion.value, ...snapshot })
    await refresh()
    status.value = '资料对照已保存到成果。'
  })
}

const eventForm = ref({ title: '', starts: '', ends: '', location: '', price: '', url: '', note: '' })
const city = ref('')
const from = ref('')
const until = ref('')
const budget = ref<number | ''>('')
const eventKeyword = ref('')
const catalog = ref<{ title: string; uid: string; starts: number; location: string; url: string; price: number | null; currency?: string; note: string }[]>([])
const showEventSource = ref(false)
const prices = (price: unknown, currency: unknown) => typeof price === 'number'
  ? price + (typeof currency === 'string' && currency !== 'CNY' ? ' ' + currency : ' 元') : '费用待核实'
async function searchEvents() {
  await action(async () => {
    const query = new URLSearchParams({ city: city.value.trim(), keyword: eventKeyword.value.trim() })
    if (from.value) query.set('start', new Date(from.value + 'T00:00').toISOString())
    if (until.value) query.set('end', new Date(until.value + 'T23:59:59').toISOString())
    const result = await interestApi<{ configured: boolean; events: typeof catalog.value }>('/events/catalog?' + query)
    showEventSource.value = !result.configured
    catalog.value = result.events.filter(row => budget.value === '' || (row.currency === 'CNY' && typeof row.price === 'number' && row.price <= budget.value))
    if (result.configured && !catalog.value.length) status.value = '这个活动源暂时没有符合条件的条目。'
  })
}
async function addCatalogEvent(event: typeof catalog.value[number]) {
  await action(async () => {
    if (entries('event').some(item => item.data.uid === event.uid)) { status.value = '已在本地活动清单中。'; return }
    await saveInterest('event', event.title.slice(0, 180), { ...event, url: httpUrl(event.url) })
    await refresh()
    status.value = '已保存活动。'
  })
}
const events = computed(() => entries('event').filter(item => {
  const start = Number(item.data.starts)
  const end = Number(item.data.ends || start)
  const min = from.value ? new Date(from.value + 'T00:00').getTime() / 1000 : 0
  const max = until.value ? new Date(until.value + 'T23:59:59').getTime() / 1000 : Infinity
  return end >= min && start <= max && (!city.value || field(item, 'location').toLowerCase().includes(city.value.trim().toLowerCase())) &&
    (budget.value === '' || (typeof item.data.price === 'number' && (!item.data.currency || item.data.currency === 'CNY') && item.data.price <= Number(budget.value)))
}).sort((a, b) => Number(a.data.starts) - Number(b.data.starts)))
async function addEvent() {
  await action(async () => {
    const { title, starts, ends, location, price, url, note } = eventForm.value
    const start = new Date(starts).getTime() / 1000
    const end = ends ? new Date(ends).getTime() / 1000 : undefined
    if (end != null && end <= start) throw new Error('结束时间需晚于开始时间')
    await saveInterest('event', title, { starts: start, ends: end, location, price: price === '' ? null : Number(price), url: checkedUrl(url), note })
    eventForm.value.title = ''
    await refresh()
    status.value = '已保存活动。'
  })
}
async function importEvents(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  await action(async () => {
    if (file.size > 1024 * 1024) throw new Error('请选择小于 1 MB 的日历文件')
    const parsed = parseCalendar(await file.text())
    const known = new Set(entries('event').map(item => item.data.uid))
    let added = 0
    for (const item of parsed) {
      if (known.has(item.uid)) continue
      await saveInterest('event', item.title, { ...item, url: httpUrl(item.url), price: null })
      known.add(item.uid)
      added++
    }
    await refresh()
    status.value = parsed.length ? '已导入 ' + added + ' 项活动，重复项已跳过。' : '没有可识别的活动。'
  })
}
function calendar(item: Item) {
  download('elf-event.ics', exportEvent({
    title: item.title, uid: item.id, starts: Number(item.data.starts), ends: typeof item.data.ends === 'number' ? item.data.ends : undefined,
    location: field(item, 'location'), url: httpUrl(item.data.url), note: field(item, 'note'),
  }), 'text/calendar;charset=utf-8')
}

const handForm = ref({ title: '', lines: '', note: '', reminder: defaultDue() })
const pending = computed(() => entries('handoff').filter(item => !item.done))
function droppedHandoff(event: DragEvent) {
  const payload = (event.dataTransfer?.getData('text/uri-list') || event.dataTransfer?.getData('text/plain') || '')
    .split(/\r?\n/).filter(line => line.trim() && !line.startsWith('#')).join('\n')
  if (!payload) return
  const parts = [handForm.value.lines, payload].join('\n').split('\n').filter(line => line.trim())
  handForm.value.lines = parts.slice(0, 3).join('\n')
  if (!handForm.value.title) {
    const url = httpUrl(parts[0])
    handForm.value.title = url ? new URL(url).hostname : parts[0].slice(0, 80)
  }
}
const handWhen = (item: Item) => item.due && item.due <= Date.now() / 1000 ? '可以继续了' : dateText(item.due) || '随时继续'
async function addHandoff() {
  await action(async () => {
    const { title, lines, note, reminder } = handForm.value
    const parts = lines.split('\n').map(line => line.trim()).filter(Boolean).slice(0, 3)
    const urls = parts.map(httpUrl).filter(Boolean)
    const texts = parts.filter(part => !httpUrl(part))
    if (!parts.length && !note.trim()) throw new Error('留一条相关资料或下一步')
    await saveInterest('handoff', title, { urls, texts, note: note.trim() }, reminder ? new Date(reminder).getTime() / 1000 : undefined)
    handForm.value = { title: '', lines: '', note: '', reminder: defaultDue() }
    await refresh()
    status.value = '已收下。'
  })
}
async function putBack(item: Item) {
  await action(async () => {
    await interestApi('/items/' + item.id, 'PATCH', { due: Date.now() / 1000 + 1800, done: false })
    await refresh()
    status.value = '30 分钟后再提醒。'
  })
}

const watchForm = ref({ repo: '', needle: '', stable: true })
const releaseRows = computed(() => entries('release').filter(item => !item.done))
function watchMessage(result: { count: number; status: string }) {
  if (result.status === 'baseline') return '已记录当前版本，之后满足条件的变化会出现在这里。'
  if (result.status === 'waiting') return '刚检查过或服务要求稍后重试。'
  return result.count ? '发现 ' + result.count + ' 条符合条件的变化。' : '暂时没有新变化。'
}
async function addWatch() {
  await action(async () => {
    let repo = watchForm.value.repo.trim().replace(/^https?:\/\/github\.com\//i, '').replace(/\/$|\.git$/g, '')
    if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) throw new Error('请填写形如 owner/repo 的公开仓库')
    const row = await saveInterest('watch', repo, { ...watchForm.value, repo })
    watchForm.value.repo = ''
    try {
      status.value = watchMessage(await interestApi('/watch/' + row.id + '/check', 'POST'))
    } finally { await refresh() }
  })
}
async function checkWatch(item: Item) {
  await action(async () => {
    try { status.value = watchMessage(await interestApi('/watch/' + item.id + '/check', 'POST')) }
    finally { await refresh() }
  })
}

const resultItems = computed(() => items.value.filter(item => ['practice', 'photo', 'research'].includes(item.kind)))
const selected = ref<string[]>([])
const draft = ref('')
const label = { practice: '手感实验', photo: '照片对照', research: '资料对照' } as const
const assets = (item: Item) => Array.isArray(item.data.assets) ? item.data.assets as { name: string; url: string }[] : []
const strings = (item: Item, key: string) => Array.isArray(item.data[key]) ? item.data[key] as string[] : []
function resultBlock(item: Item): string {
  const prefix = '## ' + item.title + '\n\n'
  if (item.kind === 'practice') return prefix + [
    '- 刚度 ' + field(item, 'stiffness') + '，阻尼 ' + field(item, 'damping'),
    '- 初始摆幅 ' + field(item, 'strength') + '，回摆 ' + field(item, 'rebounds') + ' 次，归位 ' + field(item, 'settleSeconds') + ' 秒',
    field(item, 'note'),
  ].filter(Boolean).join('\n\n')
  if (item.kind === 'photo') return prefix + [
    strings(item, 'filenames').join(' / '), field(item, 'result'),
    field(item, 'preference') && '选择 ' + field(item, 'preference'), field(item, 'note'),
  ].filter(Boolean).join('\n\n')
  const evidence = item.data.evidence as Evidence[]
  const findings = (item.data.findings as Research['findings'] || []).map(f => '- ' + f.text + '（' + f.sources.map(id => '[' + id + ']').join('、') + '）')
  return prefix + [...findings, ...evidence.map(source => [
    '[' + source.id + '] ' + source.title + (httpUrl(source.url) ? ' — ' + httpUrl(source.url) : ''),
    ...source.excerpts.map(text => '> ' + text.replace(/\n/g, ' ')),
  ].join('\n'))].join('\n\n')
}
function compose() {
  draft.value = '# 我的兴趣成果\n\n' + resultItems.value.filter(item => selected.value.includes(item.id)).map(resultBlock).join('\n\n')
}
async function downloadPreview(item: Item, index: number) {
  await action(async () => {
    const full = await interestApi<Item>('/items/' + item.id)
    const url = strings(full, 'previews')[index]
    if (!url?.startsWith('data:image/jpeg;base64,')) return
    const link = document.createElement('a')
    link.href = url
    link.download = 'elf-photo-' + (index ? 'B' : 'A') + '.jpg'
    link.click()
  })
}
function viewPhoto(item: Item) { viewingPhoto.value = item.id; section.value = 'photos' }
</script>

<template>
  <div class="interest-desk">
    <main class="desk-main">
      <header class="desk-head"><h1>{{ current.name }}</h1></header>
      <p v-if="error" class="error" role="alert">{{ error }} <button class="text-button" @click="action(refresh)">重新连接</button></p>
      <p v-if="status" class="hint" role="status">{{ status }}</p>
      <PracticeLab v-if="section === 'practice'" @saved="report" />
      <PhotoCompare v-else-if="section === 'photos'" :record-id="viewingPhoto" @saved="report" />
      <AvatarWorkshop v-else-if="section === 'avatar'" @saved="report" />

      <div v-else-if="section === 'picks'" class="stack">
        <form class="card controls" @submit.prevent="consider = true">
          <label>有空多久（分钟）<input v-model.number="freeMinutes" type="number" min="1" max="600" required /></label>
          <label>想做什么<select v-model="nowMood"><option v-for="choice in moods" :key="choice.id" :value="choice.id">{{ choice.name }}</option></select></label>
          <label>可以用什么<select v-model="nowFormat"><option value="">都可以</option><option v-for="choice in formats" :key="choice.id" :value="choice.id">{{ choice.name }}</option></select></label>
          <button class="primary">从我的清单挑</button>
        </form>
        <div v-if="consider" class="stack dense">
          <article v-for="item in suggestions" :key="item.id" class="card row">
            <div><h3>{{ item.title }}</h3><p>{{ item.data.minutes }} 分钟 · {{ nameOf(formats, field(item, 'format')) }} <span v-if="item.data.note"> · {{ item.data.note }}</span></p></div>
            <div class="actions"><button v-if="httpUrl(item.data.url)" class="primary" @click="openLink(item.data.url)">开始</button><button @click="finish(item)">做完了</button></div>
          </article>
          <p v-if="!suggestions.length" class="hint">清单里没有适合这段时间的活动。可加入你已有的内容，或调整时间和类型。</p>
        </div>
        <form class="card stack" @submit.prevent="addPick">
          <h2>加入我的兴趣清单</h2>
          <div class="controls"><label class="grow">名称<input v-model.trim="pickForm.title" maxlength="180" required /></label><label>需要几分钟<input v-model.number="pickForm.minutes" type="number" min="1" max="600" required /></label></div>
          <div class="controls"><label>用途<select v-model="pickForm.mood"><option v-for="m in moods" :key="m.id" :value="m.id">{{ m.name }}</option></select></label><label>类型<select v-model="pickForm.format"><option v-for="m in formats" :key="m.id" :value="m.id">{{ m.name }}</option></select></label><label class="grow">打开的链接（可选）<input v-model.trim="pickForm.url" type="url" maxlength="2000" placeholder="https://" /></label></div>
          <label>说明（可选）<input v-model.trim="pickForm.note" maxlength="300" placeholder="例如第三章，书在桌上" /></label>
          <div><button class="primary" :disabled="busy">加入清单</button></div>
        </form>
        <article v-for="item in entries('pick')" :key="item.id" class="card row">
          <div><h3>{{ item.title }}</h3><p>{{ item.data.minutes }} 分钟 · {{ nameOf(moods, field(item, 'mood')) }}{{ item.done ? ' · 已完成' : '' }}</p></div>
          <div class="actions"><button v-if="httpUrl(item.data.url)" @click="openLink(item.data.url)">打开</button><button @click="finish(item, !item.done)">{{ item.done ? '再看看' : '完成' }}</button><button class="text-button" @click="remove(item)">删除</button></div>
        </article>
      </div>

      <div v-else-if="section === 'research'" class="stack">
        <form class="stack" @submit.prevent="compare">
          <label>要弄明白的问题<input v-model.trim="researchText" minlength="2" maxlength="500" required placeholder="例如不同资料怎样解释阻尼对回摆次数的影响" /></label>
          <article v-for="(source, index) in sources" :key="index" class="card stack dense">
            <div class="controls"><label class="grow">资料 {{ index + 1 }} 标题<input v-model.trim="source.title" maxlength="150" required /></label><label class="grow">出处链接（可选）<input v-model.trim="source.url" type="url" maxlength="2000" placeholder="https://" /></label></div>
            <label>贴入需要对照的原文段落<textarea v-model.trim="source.text" rows="4" maxlength="12000" required /></label>
            <button v-if="sources.length > 2" type="button" class="text-button" @click="sources.splice(index, 1)">移除此资料</button>
          </article>
          <div class="actions"><button v-if="sources.length < 5" type="button" @click="sources.push({ title: '', url: '', text: '' })">增加一份资料</button><button class="primary" :disabled="busy">开始对照</button></div>
          <label class="check"><input v-model="useModel" type="checkbox" /> 使用已配置的模型提炼共同点和分歧（问题与匹配的段落会发送给模型服务）</label>
        </form>
        <template v-if="comparison">
          <h2>{{ comparedQuestion }}</h2>
          <p v-if="comparison.mode === 'unavailable'" class="hint">模型暂不可用，下面是本地找到的原文片段。</p>
          <article v-for="finding in comparison.findings" :key="finding.text" class="card"><p>{{ finding.text }}</p><small>资料 {{ finding.sources.join('、') }}</small></article>
          <article v-for="e in comparison.evidence" :key="e.id" class="card stack dense">
            <div class="row"><h3>{{ e.id }}. {{ e.title }}</h3><button v-if="httpUrl(e.url)" @click="openLink(e.url)">查看出处</button></div>
            <blockquote v-for="excerpt in e.excerpts" :key="excerpt">{{ excerpt }}</blockquote>
            <p v-if="!e.excerpts.length" class="muted">当前问题在这份资料里没有找到词项重合的片段。</p>
          </article>
          <div><button class="primary" :disabled="busy" @click="saveComparison">保存对照</button></div>
        </template>
      </div>

      <div v-else-if="section === 'events'" class="stack">
        <div class="card controls">
          <label>城市或地点<input v-model="city" placeholder="例如海淀" /></label>
          <label>起始日期<input v-model="from" type="date" /></label>
          <label>截止日期<input v-model="until" type="date" /></label>
          <label>最高费用（元）<input v-model.number="budget" type="number" min="0" placeholder="不限" /></label>
          <label>导入日历<input type="file" accept=".ics,text/calendar" :disabled="busy" @change="importEvents" /></label>
        </div>
        <form class="card controls" @submit.prevent="searchEvents">
          <label class="grow">活动关键词<input v-model.trim="eventKeyword" maxlength="100" placeholder="例如摄影、展览、音乐" /></label>
          <button class="primary" :disabled="busy">查找公开活动</button>
        </form>
        <p v-if="showEventSource" class="hint">公开检索使用 Ticketmaster 活动源。在本机配置 ELF_TICKETMASTER_API_KEY 后可查询有覆盖的城市。也可直接导入主办方日历或录入活动。</p>
        <article v-for="event in catalog" :key="event.uid" class="card row">
          <div><h3>{{ event.title }}</h3><p>{{ dateText(event.starts) }} · {{ event.location }} · {{ prices(event.price, event.currency) }}</p></div>
          <div class="actions"><button v-if="httpUrl(event.url)" @click="openLink(event.url)">查看活动</button><button class="primary" @click="addCatalogEvent(event)">保存到行程</button></div>
        </article>
        <p v-if="!events.length" class="muted">还没有符合条件的本地活动。可从主办方页面补充活动，或导入 .ics 文件。</p>
        <article v-for="item in events" :key="item.id" class="card row">
          <div><h3>{{ item.title }}</h3><p>{{ dateText(Number(item.data.starts)) }} · {{ item.data.location || '地点待核实' }} · {{ prices(item.data.price, item.data.currency) }}</p><p v-if="item.data.note">{{ item.data.note }}</p></div>
          <div class="actions"><button v-if="httpUrl(item.data.url)" @click="openLink(item.data.url)">核对官方入口</button><button class="primary" @click="calendar(item)">导出到日历</button><button class="text-button" @click="remove(item)">删除</button></div>
        </article>
        <form class="card stack" @submit.prevent="addEvent">
          <h2>录入活动</h2>
          <div class="controls"><label class="grow">名称<input v-model.trim="eventForm.title" maxlength="180" required /></label><label>开始<input v-model="eventForm.starts" type="datetime-local" required /></label><label>结束（可选）<input v-model="eventForm.ends" type="datetime-local" /></label></div>
          <div class="controls"><label class="grow">活动地点<input v-model.trim="eventForm.location" maxlength="250" /></label><label>费用（元）<input v-model="eventForm.price" type="number" min="0" step="0.01" placeholder="待核实" /></label><label class="grow">官方链接<input v-model.trim="eventForm.url" type="url" maxlength="2000" placeholder="https://" /></label></div>
          <label>预约方式或其他条件<input v-model.trim="eventForm.note" maxlength="1000" /></label>
          <div><button class="primary" :disabled="busy">保存活动</button></div>
        </form>
      </div>

      <div v-else-if="section === 'handoff'" class="stack">
        <article v-for="item in pending" :key="item.id" class="card stack dense">
          <div class="row"><h3>{{ item.title }}</h3><small>{{ handWhen(item) }}</small></div>
          <p v-if="item.data.note">{{ item.data.note }}</p>
          <p v-for="line in strings(item, 'texts')" :key="line">{{ line }}</p>
          <div class="actions"><button v-for="(url, i) in strings(item, 'urls')" :key="url" @click="openLink(url)">打开资料 {{ i + 1 }}</button><button class="primary" @click="finish(item)">完成</button><button @click="putBack(item)">30 分钟后</button><button class="text-button" @click="remove(item)">删除</button></div>
        </article>
        <form class="card stack" @submit.prevent="addHandoff">
          <h2>留给稍后的自己</h2>
          <label>这件事叫什么<input v-model.trim="handForm.title" maxlength="180" required /></label>
          <label @dragover.prevent @drop.prevent="droppedHandoff">拖入链接或粘贴短文本，每行一条<textarea v-model="handForm.lines" rows="3" maxlength="6000" @drop.prevent.stop="droppedHandoff" /></label>
          <div class="controls"><label class="grow">回来先做什么<input v-model.trim="handForm.note" maxlength="400" /></label><label>提醒时间<input v-model="handForm.reminder" type="datetime-local" /></label><button class="primary" :disabled="busy">交给精灵</button></div>
        </form>
        <article v-for="item in entries('handoff').filter(row => row.done)" :key="item.id" class="card row">
          <div><h3>{{ item.title }}</h3><p>已完成</p></div><div class="actions"><button @click="finish(item, false)">恢复</button><button class="text-button" @click="remove(item)">删除</button></div>
        </article>
      </div>

      <div v-else-if="section === 'watch'" class="stack">
        <form class="card stack" @submit.prevent="addWatch">
          <div class="controls"><label class="grow">公开 GitHub 仓库<input v-model.trim="watchForm.repo" maxlength="230" required placeholder="owner/repo" /></label><label class="grow">安装包名称关键词（可选）<input v-model.trim="watchForm.needle" maxlength="120" placeholder="例如 windows 或 setup.exe" /></label></div>
          <div class="actions"><label class="check"><input v-model="watchForm.stable" type="checkbox" /> 只看稳定版</label><button class="primary" :disabled="busy">开始追踪</button></div>
        </form>
        <article v-for="item in releaseRows" :key="item.id" class="card stack dense">
          <div class="row"><h3>{{ item.title }}</h3><small>{{ dateText(item.created) }}</small></div>
          <p v-if="item.data.condition">匹配资产关键词 {{ item.data.condition }}</p>
          <div class="actions"><button v-if="httpUrl(item.data.url)" class="primary" @click="openLink(item.data.url)">看版本原文</button><button v-for="asset in assets(item)" :key="asset.name" @click="openLink(asset.url)">{{ asset.name }}</button><button @click="finish(item)">已看</button></div>
        </article>
        <article v-for="item in entries('watch')" :key="item.id" class="card row">
          <div><h3>{{ item.title }}</h3><p>{{ item.done ? '已暂停' : item.data.status === 'error' ? '上次读取失败' : '追踪中' }} · 当前 {{ item.data.latest || '待读取' }}</p></div>
          <div class="actions"><button v-if="!item.done" :disabled="busy" @click="checkWatch(item)">现在检查</button><button @click="finish(item, !item.done)">{{ item.done ? '恢复' : '暂停' }}</button><button class="text-button" @click="remove(item)">删除</button></div>
        </article>
      </div>

      <div v-else-if="section === 'results'" class="stack">
        <p v-if="!resultItems.length" class="muted">完成一次手感实验、照片对照或资料对照并保存，成果就会出现在这里。</p>
        <article v-for="item in resultItems" :key="item.id" class="card row">
          <label class="check"><input v-model="selected" type="checkbox" :value="item.id" /><span>{{ item.title }}<small>{{ label[item.kind as keyof typeof label] }} · {{ dateText(item.created) }}</small></span></label>
          <div class="actions">
            <template v-if="item.kind === 'photo'"><button @click="viewPhoto(item)">打开对照</button><button @click="downloadPreview(item, 0)">照片 A</button><button @click="downloadPreview(item, 1)">照片 B</button></template>
            <button v-if="item.kind === 'practice'" @click="download('elf-spring.json', JSON.stringify(item.data, null, 2), 'application/json')">导出参数</button>
            <button class="text-button" @click="remove(item)">删除</button>
          </div>
        </article>
        <div v-if="resultItems.length" class="actions"><button class="primary" :disabled="!selected.some(id => resultItems.some(item => item.id === id))" @click="compose">整理选中的成果</button></div>
        <label v-if="draft">编辑成果<textarea v-model="draft" rows="10" /></label>
        <div v-if="draft"><button class="primary" @click="download('elf-interests.md', draft, 'text/markdown;charset=utf-8')">导出 Markdown</button></div>
      </div>
    </main>
  </div>
</template>

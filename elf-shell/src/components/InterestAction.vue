<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { httpUrl, interestApi } from '../interest/api'
import { activityTime, type Activity, type ActivityIdea, type ActivityOutcome } from '../interest/activity'

const props = defineProps<{ current: Activity | null; recent: Activity | null; intent: string; now: number }>()
const emit = defineEmits<{ started: [activity: Activity]; finished: [activity: Activity] }>()
const minutes = ref(10)
const idea = ref<ActivityIdea | null>(null)
const requestBusy = ref(false)
const error = ref('')
const note = ref('')
const closing = ref(false)
const seen: string[] = []
const timeChoices = [5, 10, 20]
const clock = computed(() => props.current ? activityTime(props.current.until, props.now) : '')
const elapsed = computed(() => {
  const activity = props.current
  if (!activity) return 0
  return Math.max(0, Math.min(100, (props.now - activity.started) * 100 / (activity.until - activity.started)))
})
let requestId = 0

function minutesFrom(text: string) {
  const span = text.match(/(\d{1,3})\s*(分钟|小时)/)
  if (span) return Math.max(2, Math.min(60, Number(span[1]) * (span[2] === '小时' ? 60 : 1)))
  return text.includes('半小时') ? 30 : 10
}

async function pick(another = false) {
  const id = ++requestId
  if (another && idea.value) seen.push(idea.value.key)
  else seen.length = 0
  requestBusy.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({
      minutes: String(minutes.value), intent: props.intent, exclude: seen.slice(-12).join(','),
    })
    const suggestion = await interestApi<ActivityIdea | null>('/activity/suggest?' + params)
    if (id === requestId) idea.value = suggestion
  } catch {
    if (id === requestId) error.value = '刚才没找到活动，点一下重试。'
  } finally {
    if (id === requestId) requestBusy.value = false
  }
}

watch(() => props.intent, text => {
  minutes.value = minutesFrom(text)
  if (!props.current) void pick()
}, { immediate: true })
watch(() => props.current?.id, (id, previous) => {
  if (!id && previous) {
    closing.value = false
    note.value = ''
    void pick()
  }
})

async function start() {
  if (!idea.value || requestBusy.value) return
  requestBusy.value = true
  error.value = ''
  try {
    const activity = await interestApi<Activity>('/activity/current', 'POST', idea.value)
    emit('started', activity)
  } catch { error.value = '没能开始计时，再试一下。' }
  finally { requestBusy.value = false }
}

async function openResource() {
  const url = httpUrl(props.current?.idea.url)
  if (!url) return
  try { await window.elfShell.openInterestLink(url) }
  catch { error.value = '这个链接没能打开。' }
}

async function finish(outcome: ActivityOutcome) {
  if (!props.current || requestBusy.value) return
  requestBusy.value = true
  error.value = ''
  try {
    const activity = await interestApi<Activity>('/activity/current', 'PATCH', { id: props.current.id, outcome, note: note.value.trim() })
    emit('finished', activity)
  } catch { error.value = '没能保存，这段行动还在。' }
  finally { requestBusy.value = false }
}
</script>

<template>
  <section class="action-card" aria-label="兴趣行动">
    <template v-if="current">
      <div class="action-top">
        <span class="action-topic">{{ current.idea.topic }}</span>
        <span class="clock" :aria-label="clock === '00:00' ? '时间到了' : '剩余时间'">{{ clock === '00:00' ? '时间到了' : clock }}</span>
      </div>
      <div class="time-track" role="progressbar" aria-label="已用时间" :aria-valuenow="Math.round(elapsed)">
        <span :style="{ width: elapsed + '%' }"></span>
      </div>
      <h2>{{ current.idea.title }}</h2>
      <p class="action-step">{{ current.idea.step }}</p>
      <button v-if="httpUrl(current.idea.url) && !closing" type="button" class="resource" @click="openResource">打开资料 ↗</button>
      <template v-if="closing">
        <form class="action-form" @submit.prevent="finish('done')">
          <input v-model="note" maxlength="500" aria-label="成果或下一步" placeholder="留一句成果或下次从哪开始（可选）" />
          <button class="go" :disabled="requestBusy">记下成果</button>
        </form>
        <div class="action-bottom">
          <button type="button" :disabled="requestBusy" @click="closing = false">返回</button>
          <button type="button" :disabled="requestBusy" @click="finish('later')">晚点继续</button>
          <button type="button" :disabled="requestBusy" @click="finish('skip')">结束这次</button>
        </div>
      </template>
      <div v-else class="action-bottom">
        <button type="button" class="go" @click="closing = true">收尾</button>
      </div>
    </template>
    <template v-else>
      <div class="action-top">
        <span class="action-topic">现在做一件</span>
        <div class="time-choices" aria-label="可用时间">
          <button v-for="value in timeChoices" :key="value" type="button"
            :aria-pressed="minutes === value" @click="minutes = value; pick()">{{ value }} 分钟</button>
        </div>
      </div>
      <template v-if="idea">
        <h2>{{ idea.title }}</h2>
        <p class="action-step">{{ idea.step }}</p>
        <p class="action-reason">{{ idea.minutes }} 分钟 · {{ idea.reason }}</p>
        <div class="action-bottom">
          <button type="button" class="go" :disabled="requestBusy" @click="start">开始</button>
          <button type="button" :disabled="requestBusy" @click="pick(true)">换一个</button>
        </div>
      </template>
      <p v-else-if="requestBusy" class="action-step">在找一件现在能做的事…</p>
      <button v-else type="button" class="resource" @click="pick()">重新挑一次</button>
      <p v-if="recent?.note" class="recent" :title="recent.note">上次留下 · {{ recent.note }}</p>
    </template>
    <p v-if="error" class="action-error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.action-card {
  display: grid;
  gap: 9px;
  min-width: 0;
  padding: 13px;
  border: 1px solid #443a75;
  border-radius: 13px;
  color: #fcf8ff;
  background: radial-gradient(ellipse at 104% 4%, #534777 0%, transparent 65%), #232039;
}
.action-top, .action-bottom, .time-choices, .action-form { display: flex; align-items: center; gap: 7px; }
.action-top { justify-content: space-between; }
.action-topic, .clock { color: #d9cef6; font-size: 11px; font-weight: 680; }
.clock { color: #bcf1dd; font-size: 16px; font-variant-numeric: tabular-nums; }
.action-card h2 { margin: 0; font-size: 16px; line-height: 1.35; overflow-wrap: anywhere; }
.action-card p { margin: 0; }
.action-step { color: #f0eafc; font-size: 12px; line-height: 1.5; overflow: auto; max-height: 77px; overflow-wrap: anywhere; }
.action-reason, .recent, .action-error { color: #cabbeb; font-size: 11px; }
.recent { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border-top: 1px solid #504567; padding-top: 7px; }
.action-error { color: #ffd4dc; }
.action-card button {
  border: 0;
  border-radius: 7px;
  padding: 5px 8px;
  color: #f1eaff;
  font: inherit;
  font-size: 11px;
  line-height: 17px;
  background: #ffffff14;
  cursor: pointer;
}
.action-card button:hover { background: #ffffff26; }
.action-card button:focus-visible { outline: 2px solid #b5eed5; outline-offset: 2px; }
.action-card button:disabled { opacity: 0.5; cursor: default; }
.action-card .go, .action-card [aria-pressed='true'] { background: #c1f1dc; color: #202d2e; font-weight: 740; }
.action-card .go:hover, .action-card [aria-pressed='true']:hover { background: #defaeb; }
.action-card .go { min-width: 62px; white-space: nowrap; }
.action-card .resource { justify-self: start; }
.time-track { height: 3px; border-radius: 8px; background: #ffffff26; overflow: hidden; }
.time-track span { display: block; height: 100%; background: #b1efd4; transition: width 220ms ease; }
.action-card input { min-width: 0; flex: 1; padding: 7px; border: 1px solid #8374ab; border-radius: 7px; color: #fff; background: #2b2440; font: inherit; }
.action-card input::placeholder { color: #bcb0d3; }
</style>

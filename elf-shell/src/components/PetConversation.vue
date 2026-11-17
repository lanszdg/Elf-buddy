<script setup lang="ts">
import { nextTick, onUnmounted, ref } from 'vue'
import { brainFetch, type ChatReply } from '../brain'
import { dateText, httpUrl, saveInterest, type Item } from '../interest/api'
import { requestedTool, type InterestTool } from '../interest/toolRoutes'
import type { Activity } from '../interest/activity'
import InterestAction from './InterestAction.vue'

defineProps<{ name: string; notice?: Item; activity: Activity | null; recentActivity: Activity | null; now: number }>()
const emit = defineEmits<{
  close: []; tool: [section: InterestTool]; saved: []; replied: [reply: ChatReply]; waiting: [waiting: boolean]
  activityStarted: [activity: Activity]; activityFinished: [activity: Activity]
}>()
const input = ref('')
const field = ref<HTMLInputElement | HTMLTextAreaElement>()
const message = ref('')
const saving = ref(false)
const mode = ref<'chat' | 'save'>('chat')
const intent = ref('')
const choices = ref(false)
const minutes = ref(30)
const request = new AbortController()
onUnmounted(() => request.abort())

const more: { section: InterestTool; label: string }[] = [
  { section: 'picks', label: '我的兴趣清单' },
  { section: 'photos', label: '对照两张照片' },
  { section: 'research', label: '比较几份资料' },
  { section: 'events', label: '找个周末活动' },
  { section: 'watch', label: '关注一个新版本' },
  { section: 'results', label: '找回我的成果' },
  { section: 'avatar', label: '换个形象' },
]

function saveMode() {
  mode.value = 'save'
  choices.value = false
  message.value = ''
  nextTick(() => field.value?.focus())
}

function acceptDrop(event: DragEvent) {
  const data = event.dataTransfer
  const value = (data?.getData('text/uri-list') || data?.getData('text/plain') || '')
    .split(/\r?\n/).filter(line => line && !line.startsWith('#')).join('\n').trim()
  if (!value) return
  input.value = value.slice(0, 1800)
  saveMode()
}

function onActivityFinished(activity: Activity) {
  emit('activityFinished', activity)
  if (activity.outcome === 'later') message.value = '把下一步收好了。30 分钟后可以接着做。'
  if (activity.outcome === 'done') message.value = activity.note ? '记住这一步了。' : '这一步完成了。'
  if (activity.outcome === 'skip') message.value = '这次先到这里。'
}

async function submit() {
  const text = input.value.trim()
  if (!text || saving.value) return
  if (mode.value === 'save') {
    saving.value = true
    try {
      const rows = text.split(/\r?\n/).map(row => row.trim()).filter(Boolean)
      const urls = rows.map(httpUrl).filter(Boolean).slice(0, 3)
      const notes = rows.filter(row => !httpUrl(row)).join('\n').slice(0, 1200)
      const title = notes.split('\n')[0]?.slice(0, 60) || (urls[0] ? new URL(urls[0]).hostname : '稍后继续')
      await saveInterest('handoff', title, { urls, texts: [], note: notes }, Date.now() / 1000 + minutes.value * 60)
      input.value = ''
      mode.value = 'chat'
      message.value = '收好了。' + minutes.value + ' 分钟后提醒你。'
      emit('saved')
    } catch { message.value = '刚才没存上，内容还在输入框里。' }
    finally { saving.value = false }
    return
  }

  if (/^(玩什么|做什么)[？?]?$|帮我(挑|选)|挑一件|有空.{0,12}(做什么|玩什么)/.test(text) || /\d+\s*分钟.{0,12}(放松|练习|动手)/.test(text)) {
    input.value = ''
    message.value = ''
    choices.value = false
    intent.value = text
    return
  }
  const tool = requestedTool(text)
  if (tool) { input.value = ''; emit('tool', tool); return }
  if (/^随手记$|^收一下$|^待会提醒这条$/.test(text)) { input.value = ''; saveMode(); return }

  saving.value = true
  choices.value = false
  message.value = ''
  emit('waiting', true)
  try {
    const response = await brainFetch('/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }), signal: request.signal,
    })
    if (!response.ok) throw new Error('Chat failed')
    const reply = await response.json() as ChatReply
    message.value = reply.reply_text || '我在听。'
    input.value = ''
    emit('replied', reply)
  } catch {
    if (!request.signal.aborted) message.value = '刚才没接上。你可以再发一次。'
  } finally {
    saving.value = false
    emit('waiting', false)
  }
}
</script>

<template>
  <section class="pet-dialog bubble" :aria-label="name + '对话'" @keydown.esc.stop="emit('close')"
    @dragover.prevent @drop.prevent="acceptDrop">
    <header class="dialog-head">
      <span class="speaker"><i aria-hidden="true"></i>{{ name }}</span>
      <button class="dialog-close" type="button" aria-label="收起对话" @click="emit('close')">×</button>
    </header>
    <div v-if="mode === 'save' || message || notice" class="dialog-copy" aria-live="polite">
      <p v-if="mode === 'save'" class="hello">把想留到稍后的事交给我。</p>
      <p v-else-if="message" class="reply-text">{{ message }}</p>
      <template v-else-if="notice">
        <p class="hello">{{ notice.title }}</p>
        <p class="context">{{ typeof notice.data.note === 'string' ? notice.data.note : notice.kind === 'release' ? '关注的项目有新版本。' : dateText(notice.due) }}</p>
        <button class="soft-action" type="button" @click="emit('tool', notice.kind === 'release' ? 'watch' : 'handoff')">接着看</button>
      </template>
    </div>
    <InterestAction v-if="mode === 'chat' && !choices && (!message || activity)"
      :current="activity" :recent="recentActivity" :intent="intent" :now="now"
      @started="emit('activityStarted', $event)" @finished="onActivityFinished" />
    <form class="dialog-form" @submit.prevent="submit">
      <template v-if="mode === 'save'">
        <textarea ref="field" v-model="input" aria-label="稍后要做的事" maxlength="1800" rows="2" placeholder="贴个链接，或写一句回来先做什么"></textarea>
        <div class="save-actions">
          <select v-model.number="minutes" aria-label="提醒时间">
            <option :value="15">15 分钟后</option><option :value="30">30 分钟后</option><option :value="60">1 小时后</option>
          </select>
          <button class="text-action" type="button" @click="mode = 'chat'">返回</button>
          <button class="send" :disabled="saving || !input.trim()">收下</button>
        </div>
      </template>
      <div v-else class="chat-row">
        <input ref="field" v-model="input" maxlength="500" placeholder="问一件事，或说你想做什么" aria-label="想对精灵说什么" />
        <button class="send" :disabled="saving || !input.trim()">{{ saving ? '…' : '说' }}</button>
      </div>
    </form>
    <div v-if="mode === 'chat'" class="dialog-actions">
      <button type="button" :disabled="saving" @click="saveMode">随手收</button>
      <button v-if="message && !activity" type="button" :disabled="saving" @click="message = ''">挑一件做</button>
      <button type="button" class="more" :disabled="saving" :aria-expanded="choices" aria-label="更多可请精灵做的事" @click="choices = !choices">···</button>
    </div>
    <div v-if="choices" class="dialog-choices">
      <button v-for="item in more" :key="item.section" type="button" @click="emit('tool', item.section)">{{ item.label }}</button>
    </div>
  </section>
</template>

<style scoped>
.pet-dialog.bubble {
  inset: auto 48px 144px;
  max-height: calc(100% - 168px);
  overflow: visible;
  padding: 13px 14px 12px;
  gap: 9px;
  color: #25354a;
  background: #fcfaf5;
  border: 1px solid #a9b3ba;
  border-radius: 15px;
  box-shadow: 0 10px 26px -9px #26374e66;
  backdrop-filter: none;
  animation: none;
  font-size: 12px;
}
.pet-dialog::after {
  content: '';
  position: absolute;
  z-index: -1;
  bottom: -5px;
  left: calc(50% - 5px);
  width: 10px;
  height: 10px;
  background: #fcfaf5;
  border-right: 1px solid #a9b3ba;
  border-bottom: 1px solid #a9b3ba;
  transform: rotate(45deg);
}
.dialog-head, .chat-row, .dialog-actions, .save-actions { display: flex; align-items: center; gap: 7px; }
.dialog-head { justify-content: space-between; }
.speaker { display: flex; align-items: center; gap: 6px; color: #4c6275; font-weight: 650; }
.speaker i { width: 7px; height: 7px; border-radius: 50%; background: #658eaa; }
.dialog-close { width: 24px; height: 24px; margin: -5px -4px -5px 0; font-size: 18px; }
.dialog-copy { min-height: 21px; max-height: 101px; overflow: auto; overflow-wrap: anywhere; }
.dialog-copy p { margin: 0; }
.hello { font-size: 15px; line-height: 1.5; font-weight: 650; }
.reply-text { line-height: 1.6; white-space: pre-wrap; }
.context { font-size: 11px; color: #586674; line-height: 1.5; }
.dialog-form { display: grid; gap: 7px; }
.dialog-form input, .dialog-form textarea, select {
  width: 100%; min-width: 0; box-sizing: border-box; padding: 7px 8px;
  background: #fffefa; color: #25354a; border: 1px solid #bdc6cc; border-radius: 7px;
  font-size: 12px; line-height: 16px;
}
.dialog-form textarea { resize: none; }
.dialog-form input:focus, .dialog-form textarea:focus { outline: 2px solid #7194ac; outline-offset: -1px; }
.dialog-form select { width: auto; margin-right: auto; }
.pet-dialog button {
  color: #344b5e; background: transparent; border: 0; border-radius: 6px;
  font: inherit; cursor: pointer; white-space: nowrap;
}
.pet-dialog button:hover { background: #e9eff2; }
.pet-dialog .send { padding: 7px 11px; color: white; background: #365b78; font-weight: 650; }
.pet-dialog .send:hover { background: #284a65; }
.pet-dialog .send:disabled { opacity: 0.5; cursor: default; }
.dialog-actions button, .dialog-choices button, .pet-dialog .soft-action { padding: 4px 7px; background: #edf2f3; }
.pet-dialog .soft-action { margin-top: 4px; }
.pet-dialog .text-action { margin-top: 3px; padding: 4px 3px; text-decoration: underline; }
.dialog-actions button:hover, .dialog-choices button:hover { background: #dceaf0; }
.dialog-actions .more { margin-left: auto; padding: 2px 7px; font-size: 15px; font-weight: 700; }
.dialog-choices { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 5px; }
</style>

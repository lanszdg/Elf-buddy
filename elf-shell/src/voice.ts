/**
 * 语音对话（docs/05 §8.4）：麦克风 PCM 上行 → 脑侧 KWS/ASR → voice.say 下行 → TTS 朗读。
 *
 * 隐私红线：音频只走 127.0.0.1 环回，不出机；ASR 全在脑侧本地推理。
 * TTS：脑侧在线神经语音（§11.27 Edge 小艺）MP3 下行优先，失败回落系统 speechSynthesis。
 * 口型：speechSynthesis boundary 事件驱动 mouthPulse；MP3 与无 boundary 语音
 *        由 sprite 内部回退正弦，不哑巴。
 */
import { brainWs } from './brain'

export interface VoiceCallbacks {
  onWake: () => void // 听到唤醒词（进入聆听态）
  onHeard: (text: string) => void // ASR 出终稿（空串=没听清，退出聆听态）
  onSay: (text: string) => void // 精灵要开口（文本已出，随后 TTS）
  onConverse: () => void // 进入续聊窗：下一句不用再喊名字
  onIdle: () => void // 续聊窗静音超时，回到待唤醒
  onError: (message: string) => void
}

const TARGET_RATE = 16000

export class VoiceClient {
  private ws: WebSocket | null = null
  private ctx: AudioContext | null = null
  private stream: MediaStream | null = null
  private speaking = false
  private audioEl: HTMLAudioElement | null = null
  private mouth: { speak(on: boolean): void; mouthPulse?(): void } | null = null

  constructor(private cb: VoiceCallbacks) {}

  /** 由 App.vue 注入口型驱动对象（pet）。 */
  setMouth(m: { speak(on: boolean): void; mouthPulse?(): void }): void {
    this.mouth = m
  }

  async start(): Promise<void> {
    this.ws = brainWs('/ws/voice')
    this.ws.onmessage = (e) => this.onMessage(JSON.parse(e.data))
    this.ws.onclose = () => {
      if (this.stream) this.cb.onError('语音连接断开') // 非正常关闭（stream 还在=非主动 stop）
      this.teardownAudio()
    }
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    })
    this.ctx = new AudioContext()
    const src = this.ctx.createMediaStreamSource(this.stream)
    const proc = this.ctx.createScriptProcessor(4096, 1, 1)
    proc.onaudioprocess = (e) => {
      if (this.ws?.readyState !== WebSocket.OPEN) return
      const pcm = this.downsample(e.inputBuffer.getChannelData(0), this.ctx!.sampleRate)
      this.ws.send(pcm)
    }
    src.connect(proc)
    proc.connect(this.ctx.destination)
  }

  stop(): void {
    this.teardownAudio()
    this.ws?.close()
    this.ws = null
    speechSynthesis.cancel()
    this.stopAudioEl()
  }

  /** 停掉在线语音 MP3 播放。 */
  private stopAudioEl(): void {
    if (this.audioEl) {
      this.audioEl.onended = null
      this.audioEl.onerror = null
      this.audioEl.pause()
      this.audioEl = null
    }
  }

  private teardownAudio(): void {
    this.stream?.getTracks().forEach((t) => t.stop())
    this.stream = null
    this.ctx?.close()
    this.ctx = null
  }

  /** 盒子滤波抽取到 16kHz Int16 PCM。 */
  private downsample(input: Float32Array, inRate: number): ArrayBuffer {
    const ratio = inRate / TARGET_RATE
    const outLen = Math.floor(input.length / ratio)
    const out = new Int16Array(outLen)
    for (let i = 0; i < outLen; i++) {
      const base = Math.floor(i * ratio)
      let sum = 0
      for (let j = 0; j < Math.floor(ratio); j++) sum += input[base + j] ?? 0
      const v = Math.max(-1, Math.min(1, sum / Math.floor(ratio)))
      out[i] = v < 0 ? v * 0x8000 : v * 0x7fff
    }
    return out.buffer
  }

  private onMessage(ev: { type: string; text?: string; payload?: { text: string; heard?: string; audio?: string; message?: string } }): void {
    if (ev.type === 'voice.wake') {
      speechSynthesis.cancel() // barge-in：它醒了就别说个没完
      this.stopAudioEl()
      this.gate(false) // 打断即开麦，别等 onend 回调
      this.mouth?.speak(false)
      this.speaking = false
      this.cb.onWake()
    } else if (ev.type === 'voice.asr_final') {
      this.cb.onHeard(ev.text ?? '')
    } else if (ev.type === 'voice.converse') {
      this.cb.onConverse()
    } else if (ev.type === 'voice.idle') {
      this.cb.onIdle()
    } else if (ev.type === 'voice.say') {
      const text = ev.payload?.text ?? ''
      this.cb.onSay(text)
      this.speak(text, ev.payload?.audio)
    } else if (ev.type === 'voice.error') {
      this.cb.onError(ev.payload?.message ?? '语音不可用')
    }
  }

  /** 告诉脑侧「我在出声/我说完了」：播放期间暂停 ASR，避免它听见自己。 */
  private gate(on: boolean): void {
    if (this.ws?.readyState !== WebSocket.OPEN) return
    this.ws.send(JSON.stringify({ type: 'voice.gate', on }))
  }

  /**
   * 朗读：有在线神经语音（§11.27）就播 MP3，否则系统 TTS 兜底。
   * 口型：speechSynthesis boundary 事件驱动 mouthPulse；MP3 与无 boundary 的系统语音
   *       由 sprite 内部回退正弦，不哑巴。
   */
  speak(text: string, audioB64?: string): void {
    if (this.speaking) speechSynthesis.cancel()
    this.stopAudioEl()
    if (audioB64 && this.playMp3(audioB64)) return
    this.speakSystem(text)
  }

  /** 在线 MP3 播放；返回 false 表示解码/播放失败，调用方回落系统 TTS。 */
  private playMp3(audioB64: string): boolean {
    try {
      const bin = atob(audioB64)
      const buf = new Uint8Array(bin.length)
      for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i)
      const url = URL.createObjectURL(new Blob([buf], { type: 'audio/mpeg' }))
      const a = new Audio(url)
      this.audioEl = a
      a.onplay = () => {
        this.speaking = true
        this.gate(true)
        this.mouth?.speak(true)
      }
      const done = () => {
        this.speaking = false
        this.gate(false)
        this.mouth?.speak(false)
        URL.revokeObjectURL(url)
        if (this.audioEl === a) this.audioEl = null
      }
      a.onended = done
      a.onerror = done
      void a.play()
      return true
    } catch {
      return false
    }
  }

  /** 系统 TTS 朗读（零体积；boundary 事件驱动口型，无 boundary 时 sprite 回退正弦）。 */
  private speakSystem(text: string): void {
    const u = new SpeechSynthesisUtterance(text)
    u.lang = 'zh-CN'
    u.rate = 1.05
    u.onstart = () => {
      this.speaking = true
      this.gate(true)
      this.mouth?.speak(true)
    }
    u.onboundary = () => this.mouth?.mouthPulse?.()
    const done = () => {
      this.speaking = false
      this.gate(false)
      this.mouth?.speak(false)
    }
    u.onend = done
    u.onerror = done
    speechSynthesis.speak(u)
  }
}

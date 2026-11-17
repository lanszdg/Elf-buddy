/**
 * 皮肤抠图（skin-1，docs/05 §11.37）：onnxruntime-web(wasm 单线程) + ISNet，纯本地推理。
 * 图不出本机、零 API 成本；模型/运行时经 elf://model/ 协议取（main.ts modelsDir 管道）。
 *
 * 关键约束：renderer 无 SharedArrayBuffer（窗口未开 COOP/COEP，test-sab 探针实测），
 * ORT 线程版 wasm 不可用 → 钉死 onnxruntime-web@1.18.0 + numThreads=1 + ort-wasm-simd.wasm。
 * 预处理按官方 preprocessor_config：1024×1024 NCHW float32，do_rescale=false（保持 0-255 域），
 * (x - 128) / 256（mean=[128,128,128], std=[256,256,256]）。
 */

declare global {
  interface Window {
    ort?: any
  }
}

/* eslint-disable @typescript-eslint/no-explicit-any */

const MODEL_URL = 'elf://model/isnet-general-use.quantized.onnx'
const WASM_DIR = 'elf://model/'
const NET = 1024
const CACHE_PREFIX = 'elf.skin.cutout.v1.'

let ortPromise: Promise<any> | null = null
let sessionPromise: Promise<any> | null = null

function loadOrt(): Promise<any> {
  ortPromise ??= new Promise((resolve, reject) => {
    if (window.ort) return resolve(window.ort)
    const s = document.createElement('script')
    s.src = `${WASM_DIR}ort.min.js`
    s.onload = () => (window.ort ? resolve(window.ort) : reject(new Error('ORT 未就位')))
    s.onerror = () => reject(new Error('脚本加载失败: ort.min.js'))
    document.head.appendChild(s)
  })
  return ortPromise
}

async function getSession(): Promise<any> {
  const ort = await loadOrt()
  console.log('[skin] ort 就位，创建 session…')
  // SAB 不可用：强制单线程，否则会尝试加载 threaded wasm 而初始化失败
  ort.env.wasm.numThreads = 1
  ort.env.wasm.wasmPaths = WASM_DIR
  sessionPromise ??= ort.InferenceSession.create(MODEL_URL, {
    executionProviders: ['wasm'],
    graphOptimizationLevel: 'basic',
  }).then((s: any) => {
    console.log('[skin] session 就绪')
    return s
  })
  return sessionPromise
}

/** FNV-1a：dataURL 很长，只存哈希当缓存键。 */
function fnv1a(s: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(16)
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('皮肤图加载失败'))
    img.src = url
  })
}

/**
 * dataURL → 抠出人物（透明背景、按主体裁剪）的 PNG dataURL。
 * 结果按原图哈希缓存 localStorage（抠一次 ~秒级，不能每次开机重做）。
 * 失败抛错——调用方静默回落原图（既有兜底哲学）。
 */
export async function cutoutSkin(dataURL: string): Promise<string> {
  const key = CACHE_PREFIX + fnv1a(dataURL)
  const hit = localStorage.getItem(key)
  if (hit) return hit

  const ort = await loadOrt()
  const session = await getSession()
  const img = await loadImage(dataURL)

  // cover-fit：以短边为边长的中心方块 → 1024²（ISNet 训练输入布局）
  const d = Math.min(img.naturalWidth, img.naturalHeight)
  const sx = (img.naturalWidth - d) / 2
  const sy = (img.naturalHeight - d) / 2
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = NET
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!
  ctx.drawImage(img, sx, sy, d, d, 0, 0, NET, NET)

  // 前处理：NCHW float32，保持 0-255 域，(x-128)/256
  const rgba = ctx.getImageData(0, 0, NET, NET).data
  const plane = NET * NET
  const data = new Float32Array(3 * plane)
  for (let i = 0; i < plane; i++) {
    const j = i * 4
    data[i] = (rgba[j] - 128) / 256
    data[plane + i] = (rgba[j + 1] - 128) / 256
    data[plane * 2 + i] = (rgba[j + 2] - 128) / 256
  }
  const feeds: Record<string, any> = {}
  feeds[session.inputNames[0]] = new ort.Tensor('float32', data, [1, 3, NET, NET])
  const results = await session.run(feeds)

  // 后处理：mask min-max 归一 + 软阈值羽化 → 合成 RGBA → 主体包围盒裁剪
  const mask = results[session.outputNames[0]].data as Float32Array
  if (mask.length !== plane) throw new Error(`mask 尺寸异常: ${mask.length}`)
  let lo = Infinity
  let hi = -Infinity
  for (let i = 0; i < plane; i++) {
    const v = mask[i]
    if (v < lo) lo = v
    if (v > hi) hi = v
  }
  const range = hi - lo || 1
  const out = ctx.createImageData(NET, NET)
  const od = out.data
  let minX = NET
  let minY = NET
  let maxX = -1
  let maxY = -1
  for (let y = 0; y < NET; y++) {
    for (let x = 0; x < NET; x++) {
      const i = y * NET + x
      // 线性 ramp [0.3, 0.7] ≈ smoothstep 羽化，比硬阈值干净（dev.to 抠图质量手法）
      let a = (mask[i] - lo) / range
      a = a <= 0.3 ? 0 : a >= 0.7 ? 1 : (a - 0.3) / 0.4
      const j = i * 4
      od[j] = rgba[j]
      od[j + 1] = rgba[j + 1]
      od[j + 2] = rgba[j + 2]
      od[j + 3] = Math.round(a * 255)
      if (a > 0.5) {
        if (x < minX) minX = x
        if (x > maxX) maxX = x
        if (y < minY) minY = y
        if (y > maxY) maxY = y
      }
    }
  }
  if (maxX < 0) throw new Error('mask 全空（未检出人物）')
  console.log(`[skin] mask 完成 lo=${lo.toFixed(3)} hi=${hi.toFixed(3)} bbox=${maxX - minX + 1}×${maxY - minY + 1}@${minX},${minY}`)

  // 主体包围盒 + 2px 边距裁剪：去掉透明留白（原图人物居中时整图会缩成一点）
  const pad = 2
  minX = Math.max(0, minX - pad)
  minY = Math.max(0, minY - pad)
  maxX = Math.min(NET - 1, maxX + pad)
  maxY = Math.min(NET - 1, maxY + pad)
  const w = maxX - minX + 1
  const h = maxY - minY + 1
  const crop = document.createElement('canvas')
  crop.width = w
  crop.height = h
  crop.getContext('2d')!.putImageData(out, -minX, -minY)

  const result = crop.toDataURL('image/png')
  try {
    localStorage.setItem(key, result)
  } catch {
    // 配额满等：下次重做而已，不阻断
  }
  return result
}

/** skin-2 肢体切分结果：裁出的子图（含 3px 边距）+ 枢轴 + 子图原点（全图像素坐标系）。
 * 枢轴=与肩/髋的接合点，子精灵绕它旋转；躯干底图保留完整四肢印刷，旋转缝隙被回盖。 */
export interface SkinLimb {
  kind: 'armL' | 'armR' | 'legL' | 'legR'
  canvas: HTMLCanvasElement
  ox: number // 子图左上角在全图中的坐标
  oy: number
  pivotX: number // 枢轴在全图中的坐标
  pivotY: number
}

/**
 * 从抠图结果切出四肢（skin-2，docs/05 §11.37 二期）。
 * 纯启发式（无模型）：按主体包围盒取手臂带/腿带，列投影定躯干边缘与腿缝；
 * 任何肢体面积/长度不达标就丢弃该肢——全部失败时调用方回落 skin-1 整图活化。
 * 像素级扫描 ~10ms 级，不做缓存。
 */
export async function segmentLimbs(cutoutURL: string): Promise<SkinLimb[]> {
  const img = await loadImage(cutoutURL)
  const w = img.naturalWidth
  const h = img.naturalHeight
  const canvas = document.createElement('canvas')
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!
  ctx.drawImage(img, 0, 0)
  const rgba = ctx.getImageData(0, 0, w, h).data
  const opaque = (x: number, y: number) => rgba[(y * w + x) * 4 + 3] > 128

  // 主体包围盒
  let minX = w
  let minY = h
  let maxX = -1
  let maxY = -1
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (!opaque(x, y)) continue
      if (x < minX) minX = x
      if (x > maxX) maxX = x
      if (y < minY) minY = y
      if (y > maxY) maxY = y
    }
  }
  if (maxX < 0) return []
  const bw = maxX - minX + 1
  const bh = maxY - minY + 1
  const minArea = bw * bh * 0.012

  const limbs: SkinLimb[] = []
  const crop = (x0: number, y0: number, x1: number, y1: number): { c: HTMLCanvasElement; ox: number; oy: number } => {
    const pad = 3
    const cx0 = Math.max(0, x0 - pad)
    const cy0 = Math.max(0, y0 - pad)
    const cx1 = Math.min(w - 1, x1 + pad)
    const cy1 = Math.min(h - 1, y1 + pad)
    const c = document.createElement('canvas')
    c.width = cx1 - cx0 + 1
    c.height = cy1 - cy0 + 1
    c.getContext('2d')!.putImageData(ctx.getImageData(cx0, cy0, c.width, c.height), 0, 0)
    return { c, ox: cx0, oy: cy0 }
  }

  // 手臂带 0.30H~0.60H：不透明计数 ≥ 半带高的列 = 躯干；带内躯干外的连通像素 = 手臂
  const aY0 = minY + Math.floor(bh * 0.3)
  const aY1 = minY + Math.floor(bh * 0.6)
  const bandRows = aY1 - aY0 + 1
  const colCount = new Array<number>(w).fill(0)
  for (let y = aY0; y <= aY1; y++) {
    for (let x = minX; x <= maxX; x++) if (opaque(x, y)) colCount[x]++
  }
  let torsoL = -1
  let torsoR = -1
  for (let x = minX; x <= maxX; x++) if (colCount[x] >= bandRows * 0.5) { torsoL = x; break }
  for (let x = maxX; x >= minX; x--) if (colCount[x] >= bandRows * 0.5) { torsoR = x; break }
  if (torsoL >= 0 && torsoR > torsoL) {
    for (const side of [-1, 1] as const) {
      const outside = side === -1 ? (x: number) => x < torsoL : (x: number) => x > torsoR
      let x0 = w
      let x1 = -1
      let y0 = h
      let y1 = -1
      let area = 0
      for (let y = aY0; y <= aY1; y++) {
        for (let x = minX; x <= maxX; x++) {
          if (!opaque(x, y) || !outside(x)) continue
          area++
          if (x < x0) x0 = x
          if (x > x1) x1 = x
          if (y < y0) y0 = y
          if (y > y1) y1 = y
        }
      }
      if (area >= minArea && x1 >= x0 && y1 - y0 + 1 >= bh * 0.06) {
        const { c, ox, oy } = crop(x0, y0, x1, y1)
        // 肩枢轴：靠躯干侧（内）上角，略往下 18%（避开肩部圆角）
        limbs.push({
          kind: side === -1 ? 'armL' : 'armR',
          canvas: c,
          ox,
          oy,
          pivotX: side === -1 ? x1 : x0,
          pivotY: y0 + (y1 - y0) * 0.18,
        })
      }
    }
  }

  // 腿带 0.62H~底：中央区最长零计数列带 = 腿缝；无缝（并腿）则居中硬分
  const lY0 = minY + Math.floor(bh * 0.62)
  const legCount = new Array<number>(w).fill(0)
  for (let y = lY0; y <= maxY; y++) {
    for (let x = minX; x <= maxX; x++) if (opaque(x, y)) legCount[x]++
  }
  const c0 = minX + Math.floor(bw * 0.3)
  const c1 = minX + Math.floor(bw * 0.7)
  let bestRun = 0
  let runEnd = -1
  let run = 0
  for (let x = c0; x <= c1; x++) {
    if (legCount[x] === 0) {
      run++
      if (run > bestRun) { bestRun = run; runEnd = x }
    } else run = 0
  }
  const gapX = bestRun >= Math.max(2, bw * 0.01) ? runEnd - (bestRun >> 1) : minX + (bw >> 1)
  for (const side of [-1, 1] as const) {
    const inLeg = side === -1 ? (x: number) => x < gapX : (x: number) => x >= gapX
    let x0 = w
    let x1 = -1
    let y0 = h
    let y1 = -1
    let area = 0
    for (let y = lY0; y <= maxY; y++) {
      for (let x = minX; x <= maxX; x++) {
        if (!opaque(x, y) || !inLeg(x)) continue
        area++
        if (x < x0) x0 = x
        if (x > x1) x1 = x
        if (y < y0) y0 = y
        if (y > y1) y1 = y
      }
    }
    if (area >= minArea && x1 >= x0 && y1 - y0 + 1 >= bh * 0.1) {
      const { c, ox, oy } = crop(x0, y0, x1, y1)
      // 髋枢轴：靠中线侧（内）上角
      limbs.push({
        kind: side === -1 ? 'legL' : 'legR',
        canvas: c,
        ox,
        oy,
        pivotX: side === -1 ? x1 : x0,
        pivotY: y0 + (y1 - y0) * 0.08,
      })
    }
  }
  return limbs
}

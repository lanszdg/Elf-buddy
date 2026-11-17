export interface Region { x: number; y: number; w: number; h: number }
export const FULL: Region = { x: 0, y: 0, w: 1, h: 1 }
export interface Photo {
  name: string
  url: string
  canvas: HTMLCanvasElement
}
export interface Levels { bright: number; dark: number; mean: number; histogram: number[] }

export async function loadPhoto(file: File): Promise<Photo> {
  if (file.size > 35 * 1024 * 1024) throw new Error('请选用不超过 35 MB 的照片')
  let bitmap: ImageBitmap
  try { bitmap = await createImageBitmap(file) }
  catch { throw new Error('无法读取照片。可使用 JPEG、PNG 或 WebP。') }
  try {
    const scale = Math.min(1, 950 / Math.max(bitmap.width, bitmap.height))
    const canvas = document.createElement('canvas')
    canvas.width = Math.max(1, Math.round(bitmap.width * scale))
    canvas.height = Math.max(1, Math.round(bitmap.height * scale))
    const ctx = canvas.getContext('2d', { willReadFrequently: true })!
    ctx.fillStyle = '#fff'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
    return { name: file.name, url: canvas.toDataURL('image/jpeg', 0.82), canvas }
  } finally { bitmap.close() }
}

export async function savedPhoto(url: string, name: string): Promise<Photo> {
  const img = new Image()
  img.src = url
  await img.decode()
  const canvas = document.createElement('canvas')
  canvas.width = img.naturalWidth
  canvas.height = img.naturalHeight
  canvas.getContext('2d', { willReadFrequently: true })!.drawImage(img, 0, 0)
  return { url, name, canvas }
}

export function inspectPixels(pixels: Uint8ClampedArray): Levels {
  let bright = 0, dark = 0, total = 0
  const histogram = new Array<number>(24).fill(0)
  const count = pixels.length / 4
  for (let i = 0; i < pixels.length; i += 4) {
    const value = 0.2126 * pixels[i] + 0.7152 * pixels[i + 1] + 0.0722 * pixels[i + 2]
    total += value
    if (value >= 249) bright++
    if (value <= 12) dark++
    histogram[Math.min(23, Math.floor(value / 256 * 24))]++
  }
  return { bright: bright / count * 100, dark: dark / count * 100, mean: total / count, histogram }
}

export function measure(photo: Photo, area: Region): Levels {
  const { canvas } = photo
  const x = Math.min(canvas.width - 1, Math.floor(area.x * canvas.width))
  const y = Math.min(canvas.height - 1, Math.floor(area.y * canvas.height))
  const w = Math.max(1, Math.min(canvas.width - x, Math.ceil(area.w * canvas.width)))
  const h = Math.max(1, Math.min(canvas.height - y, Math.ceil(area.h * canvas.height)))
  return inspectPixels(canvas.getContext('2d')!.getImageData(x, y, w, h).data)
}

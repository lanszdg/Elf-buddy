// These coordinates share the rig's origin at the character's feet.
export const BODY_WIDTH = 104
export const BODY_HEIGHT = 112
export const BODY_ORIGIN_Y = -100
export const FACE_EYE_Y = -49

export function mixColor(from: number, to: number, amount: number): number {
  const blend = (shift: number) => Math.round(
    ((from >> shift) & 255) * (1 - amount) + ((to >> shift) & 255) * amount,
  )
  return (blend(16) << 16) + (blend(8) << 8) + blend(0)
}

function cssColor(color: number): string {
  return '#' + color.toString(16).padStart(6, '0')
}

/** Baked once per coat colour. The whole silhouette is opaque across all backgrounds. */
export function drawCompanionCoat(color: number): HTMLCanvasElement {
  const canvas = document.createElement('canvas')
  const density = 3
  canvas.width = BODY_WIDTH * density
  canvas.height = BODY_HEIGHT * density
  const ctx = canvas.getContext('2d')!
  ctx.scale(density, density)
  ctx.translate(BODY_WIDTH / 2, -BODY_ORIGIN_Y)

  const silhouette = new Path2D()
  silhouette.moveTo(-40, -35)
  silhouette.bezierCurveTo(-44, -49, -38, -66, -26, -75)
  silhouette.bezierCurveTo(-14, -84, 8, -88, 24, -78)
  silhouette.bezierCurveTo(39, -69, 47, -52, 43, -34)
  silhouette.bezierCurveTo(40, -15, 27, -8, 9, -7)
  silhouette.bezierCurveTo(-12, -5, -34, -11, -40, -35)
  silhouette.closePath()

  const gradient = ctx.createLinearGradient(-18, -91, 19, -9)
  gradient.addColorStop(0, cssColor(mixColor(color, 0xfff6df, 0.72)))
  gradient.addColorStop(0.41, cssColor(mixColor(color, 0xe1f0ea, 0.27)))
  gradient.addColorStop(0.79, cssColor(mixColor(color, 0x656b86, 0.28)))
  gradient.addColorStop(1, cssColor(mixColor(color, 0x3a456b, 0.48)))
  ctx.fillStyle = gradient
  ctx.fill(silhouette)

  ctx.save()
  ctx.clip(silhouette)
  const rim = ctx.createRadialGradient(-19, -69, 2, -16, -64, 66)
  rim.addColorStop(0, 'rgba(255,251,229,0.51)')
  rim.addColorStop(0.67, 'rgba(255,251,229,0.07)')
  rim.addColorStop(1, 'rgba(255,251,229,0)')
  ctx.fillStyle = rim
  ctx.fillRect(-49, -92, 99, 91)

  // Soft ivory facial marking gives the eyes a stable contrast on darker coat colours.
  ctx.beginPath()
  ctx.moveTo(-32, -43)
  ctx.bezierCurveTo(-32, -57, -23, -61, -13, -59)
  ctx.bezierCurveTo(-7, -58, -3, -56, 0, -54)
  ctx.bezierCurveTo(7, -58, 15, -60, 23, -57)
  ctx.bezierCurveTo(32, -54, 35, -45, 30, -36)
  ctx.bezierCurveTo(26, -25, 13, -21, 0, -22)
  ctx.bezierCurveTo(-15, -22, -30, -29, -32, -43)
  ctx.closePath()
  const face = ctx.createLinearGradient(0, -63, 0, -18)
  face.addColorStop(0, cssColor(mixColor(color, 0xfff6df, 0.84)))
  face.addColorStop(1, cssColor(mixColor(color, 0xf3ddc7, 0.68)))
  ctx.fillStyle = face
  ctx.fill()

  ctx.lineCap = 'round'
  ctx.beginPath()
  ctx.moveTo(-29, -66)
  ctx.bezierCurveTo(-24, -73, -14, -77, -7, -77)
  ctx.lineWidth = 2
  ctx.strokeStyle = 'rgba(255,255,245,0.70)'
  ctx.stroke()

  // The small cheek tint stays inside the outline, even on the smallest zoom.
  for (const side of [-1, 1]) {
    ctx.beginPath()
    ctx.ellipse(side * 23.7, -38, 5.5, 2.5, side * -0.11, 0, Math.PI * 2)
    ctx.fillStyle = 'rgba(204,104,98,0.31)'
    ctx.fill()
  }
  ctx.restore()

  ctx.lineWidth = 2.15
  ctx.strokeStyle = cssColor(mixColor(color, 0x283047, 0.77))
  ctx.stroke(silhouette)
  return canvas
}

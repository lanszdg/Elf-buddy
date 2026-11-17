export type PartKind = 'arm' | 'leg' | 'ear' | 'tail'

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value))

/** Drag velocity stays in screen pixels per second, independent of pointer event rate. */
export class DragPose {
  held = false
  grip = 0
  lift = 0
  x = 0
  y = 0
  private targetX = 0
  private targetY = 0
  private age = 0

  start(grip: number) {
    this.held = true
    this.grip = clamp(grip, -1, 1)
    this.age = 0
    this.targetX = 0
    this.targetY = 0
  }

  move(vx: number, vy: number) {
    this.targetX = clamp(vx / 650, -1.35, 1.35)
    this.targetY = clamp(vy / 650, -1.35, 1.35)
    this.age = 0
  }

  release(vx = 0, vy = 0) {
    this.held = false
    this.move(vx, vy)
  }

  step(dt: number) {
    const frame = clamp(dt, 0, 0.04)
    this.age += frame
    if (this.age > 0.04) {
      const decay = Math.exp(-frame * (this.held ? 11 : 6.2))
      this.targetX *= decay
      this.targetY *= decay
    }
    const response = 1 - Math.exp(-frame * 17)
    this.x += (this.targetX - this.x) * response
    this.y += (this.targetY - this.y) * response
    this.lift += ((this.held ? 1 : 0) - this.lift) * (1 - Math.exp(-frame * (this.held ? 19 : 14)))
  }

  angle(kind: PartKind, side: number, time: number, phase: number): number {
    const swing = Math.sin(time * 7.5 + phase)
    const drift = this.x
    const vertical = this.y
    if (kind === 'ear') return drift * 0.93 + this.lift * (side * (0.38 + vertical * 0.22) + swing * 0.13)
    if (kind === 'arm') return drift * 0.56 + this.lift * (-side * 0.66 + swing * 0.21 - side * vertical * 0.3)
    if (kind === 'leg') return drift * 0.72 + this.lift * (side * 0.42 + swing * 0.39 - side * vertical * 0.24)
    return drift * 0.93 + this.lift * (0.33 + swing * 0.2)
  }

  get tilt() {
    return clamp(this.x * 0.28 + this.grip * this.lift * 0.15, -0.44, 0.44)
  }
}

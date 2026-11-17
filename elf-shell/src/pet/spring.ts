export interface SpringState {
  angle: number
  vel: number
}

export const DEFAULT_STIFFNESS = 120
export const DEFAULT_DAMPING = 8

export function stepSpring(state: SpringState, target: number, stiffness: number, damping: number, dt: number) {
  state.vel += (target - state.angle) * stiffness * dt
  state.vel *= Math.exp(-damping * dt)
  state.angle += state.vel * dt
}

export function simulateSpring(stiffness: number, damping: number, strength: number) {
  const fps = 120
  const state: SpringState = { angle: strength, vel: strength * 3 }
  const frames = [state.angle]
  let lastOutside = 0
  let lastDirection = Math.sign(state.angle)
  let swings = 0
  for (let frame = 1; frame <= fps * 5; frame++) {
    stepSpring(state, 0, stiffness, damping, 1 / fps)
    frames.push(state.angle)
    if (Math.abs(state.angle) > 0.015 || Math.abs(state.vel) > 0.12) lastOutside = frame
    if (Math.abs(state.angle) > 0.015 && Math.sign(state.angle) !== lastDirection) {
      swings++
      lastDirection = Math.sign(state.angle)
    }
  }
  return { frames, swings, fps, settle: (lastOutside + 1) / fps }
}

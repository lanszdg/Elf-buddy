export interface Avatar {
  kind: 'round' | 'long-ears' | 'drawing'
  color: string
  width: number
  height: number
  eyes: number
  ears: number
  arms: number
  legs: number
  tail: number
  stiffness: number
  damping: number
  shoulder: number
  hip: number
  drawing?: string
}

export const AVATAR_KEY = 'elf_custom_avatar'
export const ROUND_AVATAR: Avatar = {
  kind: 'round', color: '#bb895f', width: 1.01, height: 0.94, eyes: 1,
  ears: 0.35, arms: 0.8, legs: 0.65, tail: 0.4, stiffness: 120, damping: 8, shoulder: 0, hip: 0,
}
export const EAR_AVATAR: Avatar = {
  ...ROUND_AVATAR, kind: 'long-ears', color: '#9d8ead', width: 0.83, height: 1.02,
  eyes: 0.88, ears: 1.2, arms: 1.1, legs: 0.95, tail: 0.82,
}

export function parseAvatar(text: string | null): Avatar | undefined {
  if (!text) return
  try {
    const data = JSON.parse(text) as Partial<Avatar>
    if (!['round', 'long-ears', 'drawing'].includes(data.kind || '')) return
    if (data.kind === 'drawing' &&
      (typeof data.drawing !== 'string' || !data.drawing.startsWith('data:image/') || data.drawing.length > 600000)) return
    const clamp = (key: keyof Avatar, min: number, max: number) => {
      const value = data[key]
      return typeof value === 'number' && Number.isFinite(value)
        ? Math.max(min, Math.min(max, value)) : ROUND_AVATAR[key] as number
    }
    return {
      kind: data.kind!,
      color: /^#[\da-f]{6}$/i.test(data.color || '') ? data.color! : ROUND_AVATAR.color,
      width: clamp('width', 0.65, 1.4), height: clamp('height', 0.68, 1.25), eyes: clamp('eyes', 0.65, 1.45),
      ears: clamp('ears', 0, 1.6), arms: clamp('arms', 0.3, 1.6), legs: clamp('legs', 0.3, 1.6),
      tail: clamp('tail', 0, 1.5), stiffness: clamp('stiffness', 40, 230), damping: clamp('damping', 1, 22),
      shoulder: clamp('shoulder', -0.14, 0.14), hip: clamp('hip', -0.14, 0.14),
      drawing: data.kind === 'drawing' ? data.drawing : undefined,
    }
  } catch { return }
}

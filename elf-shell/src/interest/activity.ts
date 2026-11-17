import { interestApi } from './api'

export interface ActivityIdea {
  key: string
  topic: string
  title: string
  step: string
  reason: string
  minutes: number
  url: string
  source: string
}

export interface Activity {
  id: string
  idea: ActivityIdea
  started: number
  until: number
  note: string
  outcome: ActivityOutcome | null
}

export type ActivityOutcome = 'done' | 'later' | 'skip'
export interface ActivityState { current: Activity | null; recent: Activity | null }

export const activityState = () => interestApi<ActivityState>('/activity/current')

export function activityTime(until: number, now: number) {
  const remaining = Math.max(0, Math.ceil(until - now))
  return String(Math.floor(remaining / 60)).padStart(2, '0') + ':' + String(remaining % 60).padStart(2, '0')
}

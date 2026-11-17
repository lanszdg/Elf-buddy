export interface CalendarEvent {
  title: string
  starts: number
  ends?: number
  location: string
  url: string
  note: string
  uid: string
}

function parseDate(value: string, zone?: string): number | undefined {
  const parts = /^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})(Z)?)?$/.exec(value)
  if (!parts) return
  const [, year, month, day, hour = '0', minute = '0', second = '0', utc] = parts
  const args = [Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second)] as const
  const timestamp = Date.UTC(...args)
  if (utc) return timestamp / 1000
  if (!zone || !parts[4]) return new Date(...args).getTime() / 1000
  try {
    const format = new Intl.DateTimeFormat('en-CA', {
      timeZone: zone, year: 'numeric', month: 'numeric', day: 'numeric',
      hour: 'numeric', minute: 'numeric', second: 'numeric', hourCycle: 'h23',
    })
    let candidate = timestamp
    for (let i = 0; i < 3; i++) {
      const local = Object.fromEntries(format.formatToParts(candidate).map(p => [p.type, Number(p.value)]))
      const represented = Date.UTC(local.year, local.month - 1, local.day, local.hour, local.minute, local.second)
      if (represented === timestamp) return candidate / 1000
      candidate += timestamp - represented
    }
  } catch {
    // 未识别的时区需用户核实后手工添加
  }
}

const unescapeText = (value: string) => value.replace(/\\([nN,;\\])/g, (_, c: string) => c.toLowerCase() === 'n' ? '\n' : c)

export function parseCalendar(text: string): CalendarEvent[] {
  const events: CalendarEvent[] = []
  const lines = text.replace(/\r?\n[ \t]/g, '').split(/\r?\n/)
  let event: Partial<CalendarEvent> | undefined
  let cancelled = false
  for (const line of lines) {
    if (line === 'BEGIN:VEVENT') { event = {}; cancelled = false; continue }
    if (line === 'END:VEVENT') {
      if (event?.title && event.starts !== undefined && !cancelled) {
        events.push({
          title: event.title, starts: event.starts, ends: event.ends,
          location: event.location || '', url: event.url || '', note: event.note || '',
          uid: event.uid || event.title + '|' + event.starts,
        })
      }
      event = undefined
      continue
    }
    if (!event) continue
    const separator = line.indexOf(':')
    if (separator < 0) continue
    const [key, ...params] = line.slice(0, separator).split(';')
    const zone = params.find(p => p.startsWith('TZID='))?.slice(5).replace(/^"|"$/g, '')
    const value = line.slice(separator + 1)
    switch (key.toUpperCase()) {
      case 'DTSTART': event.starts = parseDate(value, zone); break
      case 'DTEND': event.ends = parseDate(value, zone); break
      case 'SUMMARY': event.title = unescapeText(value).slice(0, 180); break
      case 'LOCATION': event.location = unescapeText(value).slice(0, 250); break
      case 'DESCRIPTION': event.note = unescapeText(value).slice(0, 1000); break
      case 'URL': event.url = value; break
      case 'UID': event.uid = value.slice(0, 300); break
      case 'STATUS': cancelled = value === 'CANCELLED'; break
    }
  }
  return events
}

function icalText(value: string): string {
  return value.replace(/\\/g, '\\\\').replace(/[\r\n]+/g, '\\n').replace(/([;,])/g, '\\$1')
}
function stamp(seconds: number): string {
  return new Date(seconds * 1000).toISOString().replace(/[-:]|\.\d{3}/g, '')
}

export function exportEvent(event: CalendarEvent): string {
  const lines = [
    'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Elf Buddy//Interest Desk//ZH',
    'BEGIN:VEVENT', 'UID:' + icalText(event.uid) + '@elf-buddy.local',
    'DTSTAMP:' + stamp(Date.now() / 1000), 'DTSTART:' + stamp(event.starts),
  ]
  if (event.ends && event.ends > event.starts) lines.push('DTEND:' + stamp(event.ends))
  lines.push('SUMMARY:' + icalText(event.title), 'LOCATION:' + icalText(event.location))
  lines.push('DESCRIPTION:' + icalText([event.note, event.url].filter(Boolean).join('\n')))
  lines.push('END:VEVENT', 'END:VCALENDAR', '')
  return lines.join('\r\n')
}

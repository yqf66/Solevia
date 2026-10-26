import { factLabels } from './plannerPresentation'

function displayFact(value: unknown): string {
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map(displayFact).filter(Boolean).join('、')
  return ''
}

/** Only named user-facing facts belong in a confirmed-goal summary. */
export function RequirementSummary({ snapshot, fields = Object.keys(factLabels) }: {
  snapshot: Record<string, unknown>
  fields?: string[]
}) {
  return fields.filter(key => Object.hasOwn(factLabels, key)).map(key => {
    const text = displayFact(snapshot[key])
    return text ? <div className="confirmed-row" key={key}><strong>{factLabels[key]}</strong><p>{text}</p></div> : null
  })
}

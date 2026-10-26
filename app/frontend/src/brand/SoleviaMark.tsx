import './brand.css'

/** Official Solevia artwork supplied by the product owner. */
export function SoleviaMark({ className = '' }: { className?: string }) {
  return <img className={`solevia-mark ${className}`} src="/solevia-mark.png" width={32} height={32} alt="" aria-hidden="true" decoding="async" />
}

export function SoleviaWordmark() {
  return <><span className="solevia-emblem"><SoleviaMark /></span><span className="solevia-wordmark"><strong>Solevia<span>足下</span></strong><small>从这一步，走向想去的地方</small></span></>
}

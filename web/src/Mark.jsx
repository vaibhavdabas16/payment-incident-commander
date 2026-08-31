/** The product mark.
 *
 *  A gradient rounded square is not a logo, it is a placeholder that shipped. This one draws the
 *  thing the product exists for: a success rate running flat, dropping, and coming back — with the
 *  moment of detection marked on the fall. It reads as a mark at 20px and as a diagram at 64px,
 *  which is the whole job.
 *
 *  Colours come from CSS custom properties so the mark inverts on the dark sidebar without a
 *  second copy of the artwork.
 */
export default function Mark({ size = 24, detail = false }) {
  return (
    <svg
      className="mark"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      role="img"
      aria-label="Payment Incident Commander"
    >
      <rect width="32" height="32" rx="9" fill="currentColor" />
      <path
        d="M5.5 12.5 H12 L15 21 H18.5 L21.5 12.5 H26.5"
        stroke="var(--mark-line, #fff)"
        strokeWidth="2.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* The detection point. Only at sizes where a 2px dot is not mud. */}
      {detail ? <circle cx="15" cy="21" r="2.6" fill="var(--mark-dot, #2b53f5)" /> : null}
    </svg>
  )
}

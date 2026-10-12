'use client'

import { useEffect, useRef, useState } from 'react'

interface LogoProps {
  /** Display size of the portrait mark in px. */
  size?: number
  /** Show the "eljazari.ai" wordmark next to the mark. */
  showWordmark?: boolean
  /** Optional tagline rendered under the wordmark. */
  tagline?: string
  className?: string
}

/**
 * Brand lockup for eljazari.ai.
 *
 * Renders the charcoal scholar portrait (public/eljazari-logo.png). If the
 * image is missing it falls back to a clean "EJ" monogram so the UI never
 * breaks before the asset is added.
 */
export function Logo({ size = 32, showWordmark = true, tagline, className = '' }: LogoProps) {
  const [imgOk, setImgOk] = useState(true)
  const imgRef = useRef<HTMLImageElement>(null)

  // Catch the case where the image already 404'd before React attached the
  // onError handler (SSR → hydration gap) so the monogram fallback still shows.
  useEffect(() => {
    const img = imgRef.current
    if (img && img.complete && img.naturalWidth === 0) setImgOk(false)
  }, [])

  return (
    <div className={`flex items-center gap-2.5 ${className}`}>
      <div
        className="relative flex flex-shrink-0 items-center justify-center"
        style={{ width: size, height: size }}
      >
        {imgOk ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            ref={imgRef}
            src="/eljazari-logo.png"
            alt=""
            width={size}
            height={size}
            className="h-full w-full object-contain grayscale"
            onError={() => setImgOk(false)}
          />
        ) : (
          <span
            className="font-semibold tracking-tight text-brand-400"
            style={{ fontSize: size * 0.4 }}
          >
            EJ
          </span>
        )}
      </div>

      {showWordmark && (
        <div className="leading-none">
          <span className="text-sm font-semibold tracking-tight text-foreground">
            eljazari<span className="text-brand-400">.ai</span>
          </span>
          {tagline && (
            <p className="mt-1 text-[10px] font-medium text-muted-foreground">{tagline}</p>
          )}
        </div>
      )}
    </div>
  )
}

export default Logo

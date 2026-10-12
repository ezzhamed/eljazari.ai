'use client'

/**
 * Landing page: one prompt box and the missions known to fly.
 *
 * A run is watched in the workspace (/dashboard), not here: the landing page
 * used to grow a second, smaller copy of the workspace once a run started.
 */

import Link from 'next/link'
import { useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { AlertCircle, ArrowRight, Loader2, Send } from 'lucide-react'
import { PhaseBadge } from '@/components/app/PhaseBadge'
import { Logo } from '@/components/brand/Logo'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { STAGES } from '@/lib/pipeline'
import { useDroneStore } from '@/lib/stores/drone-store'

/**
 * The supported missions of the published prompt suite
 * (benchmarks/prompt_suite_v2.json), with English interface examples.
 *
 * The previous examples offered a fixed-wing VTOL and an 8 kg lift, which the
 * system refuses by design: a first click landed on a refusal.
 */
const EXAMPLES = [
  { tag: 'Camera hover', lang: 'en', text: 'A small quadcopter carrying a 150 g camera. Take off, hover at 10 m for 20 seconds, then land.' },
  { tag: 'Roof inspection', lang: 'en', text: 'A quadcopter for roof inspection carrying a 300 g sensor. Fly a 20 m square at 15 m altitude, then return and land.' },
  { tag: 'Out and back', lang: 'en', text: 'A lightweight quadcopter with a 100 g payload. Fly to a point 30 m north at 12 m altitude, then come back and land.' },
  { tag: 'Farm survey', lang: 'en', text: 'A quadcopter carrying a 250 g sensor. Fly a 25 m square at 15 m altitude, then return and land.' },
  { tag: 'Camera inspection', lang: 'en', text: 'A small quadcopter carrying a 200 g camera. Take off, hover at 10 m for 20 seconds, then land.' },
  { tag: 'Cinema octocopter', lang: 'en', text: 'An octocopter carrying a 2 kg cinema camera. Hover at 10 m, then land.' },
] as const

export default function HomePage() {
  const router = useRouter()
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const startProject = useDroneStore((s) => s.startProject)
  const isLoading = useDroneStore((s) => s.isLoading)
  const error = useDroneStore((s) => s.error)
  const phase = useDroneStore((s) => s.currentPhase)
  const userPrompt = useDroneStore((s) => s.userPrompt)
  const projectId = useDroneStore((s) => s.projectId)
  const [prompt, setPrompt] = useState('')
  const hasRun = Boolean(projectId) && phase !== 'idle'

  const submit = async () => {
    const text = prompt.trim()
    if (!text || isLoading) return
    await startProject(text)
    // Only leave once the run exists; a failed launch stays here, under the box.
    if (!useDroneStore.getState().error) router.push('/dashboard')
  }

  return (
    <main className="bg-hero-glow flex min-h-screen flex-col">
      <header className="sticky top-0 z-20 border-b border-border/60 bg-background/70 backdrop-blur-xl">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-5">
          <Logo size={30} />
          {hasRun && (
            <Button asChild size="sm" variant="outline" className="h-8 gap-1.5 text-xs">
              <Link href="/dashboard">
                Open workspace <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </Button>
          )}
        </div>
      </header>

      <div className="relative flex-1">
        <div className="bg-grid pointer-events-none absolute inset-0 opacity-50 [mask-image:linear-gradient(to_bottom,black,transparent_75%)]" />

        <div className="relative mx-auto flex max-w-3xl flex-col items-center px-5 pb-16 pt-14 sm:pt-20">
          <Logo size={84} showWordmark={false} />
          <Badge tone="brand" className="mt-6">Multi-agent drone design · PX4 simulation</Badge>
          <h1 className="mt-4 text-center text-3xl font-semibold tracking-tight sm:text-4xl">
            Build and test a drone from a prompt
          </h1>
          <p className="mt-3 max-w-xl text-center text-sm leading-6 text-muted-foreground">
            Describe the aircraft and its mission, in English or Arabic. The agents design it, write and check its
            mission code, and fly it in PX4 SITL; the flight is compared with the mission before it is accepted.
          </p>

          {hasRun && (
            <div className="mt-8 flex w-full items-center gap-4 rounded-xl border border-border bg-card px-4 py-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="eyebrow">Current run</span>
                  <PhaseBadge phase={phase} />
                </div>
                <p className="mt-1 truncate text-sm text-foreground/90" dir="auto" title={userPrompt}>{userPrompt}</p>
              </div>
              <Button asChild size="sm" className="h-8 flex-shrink-0 gap-1.5 text-xs">
                <Link href="/dashboard">Open <ArrowRight className="h-3.5 w-3.5" /></Link>
              </Button>
            </div>
          )}

          <div className="mt-6 w-full">
            <div className="rounded-2xl border border-border bg-card shadow-2xl shadow-black/40 transition-colors focus-within:border-brand-500/40 focus-within:ring-1 focus-within:ring-brand-500/20">
              <label htmlFor="prompt" className="sr-only">Describe the drone and its mission</label>
              <textarea
                id="prompt"
                ref={textareaRef}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    void submit()
                  }
                }}
                rows={3}
                dir="auto"
                className="w-full resize-none bg-transparent px-5 pb-2 pt-4 text-[15px] leading-7 text-foreground outline-none placeholder:text-muted-foreground/60"
                placeholder="Describe the drone, its payload and its mission…"
              />
              <div className="flex items-center justify-between gap-3 px-3 pb-3 pt-1">
                <span className="px-2 text-2xs text-muted-foreground">
                  {prompt.length > 0 ? `${prompt.length} characters` : 'Enter to run · Shift+Enter for a new line'}
                </span>
                <Button onClick={() => void submit()} disabled={!prompt.trim() || isLoading} className="h-9 gap-2 rounded-xl px-4 text-[13px] font-semibold">
                  {isLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
                  {hasRun ? 'Start a new run' : 'Run pipeline'}
                </Button>
              </div>
            </div>

            {error && (
              <div role="alert" className="mt-3 flex items-start gap-3 rounded-xl border border-destructive/25 bg-destructive/[0.06] px-4 py-3 text-sm text-destructive">
                <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <span>{error}</span>
              </div>
            )}
          </div>

          <section className="mt-10 w-full" aria-labelledby="examples-heading">
            <div className="mb-3 flex items-baseline justify-between gap-4">
              <h2 id="examples-heading" className="text-sm font-medium">Missions from the benchmark</h2>
              <span className="text-2xs text-muted-foreground">Supported flat X quad-, hexa- and octocopters</span>
            </div>
            <div className="grid gap-2.5 sm:grid-cols-2">
              {EXAMPLES.map((ex) => (
                <button
                  key={ex.tag}
                  type="button"
                  dir="ltr"
                  onClick={() => {
                    setPrompt(ex.text)
                    textareaRef.current?.focus()
                  }}
                  className="focus-ring group flex flex-col items-start rounded-xl border border-border bg-card/60 px-4 py-3 text-start transition-colors hover:border-white/20 hover:bg-card"
                >
                  <span className="flex w-full items-center justify-between gap-2">
                    <span className="text-xs font-semibold text-brand-300">{ex.tag}</span>
                    <Badge>English</Badge>
                  </span>
                  <span className="mt-1.5 line-clamp-2 text-xs leading-5 text-muted-foreground group-hover:text-foreground/80">
                    {ex.text}
                  </span>
                </button>
              ))}
            </div>
          </section>

          <section className="mt-12 w-full" aria-labelledby="how-heading">
            <h2 id="how-heading" className="mb-3 text-sm font-medium">What happens to a prompt</h2>
            <ol className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
              {STAGES.map((stage, i) => (
                <li key={stage.id} className="flex gap-3 rounded-xl border border-border/70 px-4 py-3">
                  <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-surface-raised text-2xs font-semibold tabular-nums text-muted-foreground">
                    {i + 1}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-sm text-foreground">{stage.label}</span>
                    <span className="block text-xs leading-5 text-muted-foreground">{stage.description}</span>
                  </span>
                </li>
              ))}
            </ol>
            <p className="mt-4 text-center text-2xs text-muted-foreground">
              Flights are PX4 software-in-the-loop simulations of catalogue components, not flight-tested hardware.
            </p>
          </section>
        </div>
      </div>
    </main>
  )
}

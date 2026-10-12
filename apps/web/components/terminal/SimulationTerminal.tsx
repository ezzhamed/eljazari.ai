'use client'

import { useEffect, useRef } from 'react'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'
import { usePipelineStore } from '@/lib/stores/pipeline-store'

/**
 * fit() reads the renderer's cell dimensions, which xterm only computes once
 * the terminal is attached to a laid-out element. Calling it on a container
 * that is still 0×0 (first paint, a hidden tab, a collapsing panel) threw
 * "Cannot read properties of undefined (reading 'dimensions')" straight out
 * of xterm and into Next's error overlay. Skip until the box has a size.
 */
function safeFit(fitAddon: FitAddon, host: HTMLElement | null) {
  if (!host || host.clientWidth === 0 || host.clientHeight === 0) return
  try {
    fitAddon.fit()
  } catch {
    /* renderer not ready yet — the next resize will retry */
  }
}

export default function SimulationTerminal() {
  const termRef = useRef<HTMLDivElement>(null)
  const xtermRef = useRef<Terminal | null>(null)
  // Running count of lines already written, so a burst of several entries in
  // one store update is echoed in full instead of only its last line.
  const writtenRef = useRef(0)
  const pipelineLogs = usePipelineStore((s) => s.pipelineLogs)
  const pipelineLogTotal = usePipelineStore((s) => s.pipelineLogTotal)

  useEffect(() => {
    const host = termRef.current
    if (!host) return

    const term = new Terminal({
      theme: {
        background: '#121212',
        foreground: '#d6d6d6',
        cursor: '#cdb892',
        selectionBackground: '#b89b6e40',
      },
      fontSize: 12,
      fontFamily: 'JetBrains Mono, Fira Code, monospace',
      cursorBlink: true,
      convertEol: true,
    })

    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    const observer = new ResizeObserver(() => safeFit(fitAddon, host))

    // Open on the next frame, not synchronously. open() schedules its own
    // animation-frame work that dereferences the render service; if the
    // component is torn down before that frame (React StrictMode mounts,
    // unmounts and remounts every effect in development, and a quick route
    // change does the same for real) xterm crashes inside its own callback
    // with "reading 'dimensions'". Deferring means an effect that is undone
    // immediately never opens at all.
    let opened = false
    const raf = requestAnimationFrame(() => {
      term.open(host)
      opened = true
      term.writeln('\x1b[32m[eljazari.ai] Simulation Terminal Ready\x1b[0m')
      term.writeln('\x1b[90m─────────────────────────────────────────\x1b[0m')
      xtermRef.current = term
      // Replay whatever the run has logged so far, so a page opened
      // mid-flight does not start from a blank terminal.
      const { pipelineLogs: existing, pipelineLogTotal: total } = usePipelineStore.getState()
      const stamp = `\x1b[90m[${new Date().toISOString().slice(11, 19)}]\x1b[0m`
      for (const line of existing) term.writeln(`${stamp} ${line}`)
      writtenRef.current = total
      safeFit(fitAddon, host)
      // Follow the container rather than the window — the dashboard panels
      // resize without a window resize event.
      observer.observe(host)
    })

    return () => {
      cancelAnimationFrame(raf)
      observer.disconnect()
      xtermRef.current = null
      if (opened) term.dispose()
    }
  }, [])

  // Stream logs to terminal
  useEffect(() => {
    const term = xtermRef.current
    if (!term) return
    // A reset (new run) drops the total back below what was written.
    if (pipelineLogTotal < writtenRef.current) writtenRef.current = 0
    const unseen = Math.min(pipelineLogTotal - writtenRef.current, pipelineLogs.length)
    if (unseen <= 0) return
    const stamp = `\x1b[90m[${new Date().toISOString().slice(11, 19)}]\x1b[0m`
    for (const line of pipelineLogs.slice(-unseen)) {
      term.writeln(`${stamp} ${line}`)
    }
    writtenRef.current = pipelineLogTotal
  }, [pipelineLogs, pipelineLogTotal])

  return <div ref={termRef} className="w-full h-full" />
}

'use client'

import { useEffect, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'

interface CodeEditorProps {
  code:      string
  language?: string
  readOnly?: boolean
  height?:   string
}

interface MonacoEditorHandle {
  dispose: () => void
  getValue: () => string
  setValue: (value: string) => void
}

/**
 * Monaco-based code editor.
 * Uses the raw monaco-editor package (no @monaco-editor/react dependency).
 * Workers are configured via the webpack config (next.config.js).
 */
export default function CodeEditor({
  code,
  language = 'python',
  readOnly = true,
  height   = '100%',
}: CodeEditorProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const editorRef = useRef<MonacoEditorHandle | null>(null)
  // Monaco is a multi-megabyte chunk that takes seconds on first load; until
  // it is up, the panel was simply black with no hint that anything was coming.
  const [ready, setReady] = useState(false)
  const hasCode = Boolean(code)

  useEffect(() => {
    if (!containerRef.current) return
    let mounted = true

    // Dynamically import monaco-editor to prevent SSR issues
    import('monaco-editor').then((monaco) => {
      if (!mounted || !containerRef.current) return

      // Syntax theme matching the app's charcoal and sand palette
      monaco.editor.defineTheme('drone-dark', {
        base: 'vs-dark',
        inherit: true,
        rules: [
          { token: 'keyword',  foreground: 'dcc9a8', fontStyle: 'bold' },
          { token: 'string',   foreground: '9cc7a8' },
          { token: 'comment',  foreground: '6b6b6b', fontStyle: 'italic' },
          { token: 'function', foreground: '8fb3e0' },
          { token: 'number',   foreground: 'e0a86f' },
        ],
        // The app's card surface and sand accent (globals.css).
        colors: {
          'editor.background': '#121212',
          'editor.foreground': '#e6e6e6',
          'editorLineNumber.foreground': '#3d3d3d',
          'editorLineNumber.activeForeground': '#8a8a8a',
          'editor.selectionBackground': '#b89b6e40',
          'editor.lineHighlightBackground': '#181818',
          'editorCursor.foreground': '#cdb892',
          'editorGutter.background': '#121212',
        },
      })

      editorRef.current = monaco.editor.create(containerRef.current, {
        value:           code,
        language,
        theme:           'drone-dark',
        readOnly,
        automaticLayout: true,
        minimap:         { enabled: false },
        fontSize:        13,
        fontFamily:      'JetBrains Mono, Fira Code, Consolas, monospace',
        lineNumbers:     'on',
        scrollBeyondLastLine: false,
        wordWrap:        'on',
        padding:         { top: 12, bottom: 12 },
        renderLineHighlight: 'line',
        smoothScrolling: true,
        cursorBlinking:  'smooth',
      })
      setReady(true)
    })

    return () => {
      mounted = false
      editorRef.current?.dispose()
      editorRef.current = null
      setReady(false)
    }
    // `hasCode` is a dependency because the container only exists once there
    // is code: an editor created against the placeholder would never mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [language, readOnly, hasCode])

  // Update content when code prop changes
  useEffect(() => {
    if (!editorRef.current) return
    const current = editorRef.current.getValue()
    if (current !== code) {
      editorRef.current.setValue(code)
    }
  }, [code])

  if (!code) {
    return (
      <div className="flex items-center justify-center bg-card text-sm text-muted-foreground" style={{ height }}>
        No code generated yet…
      </div>
    )
  }

  return (
    <div className="relative bg-card" style={{ height, width: '100%' }}>
      <div ref={containerRef} style={{ height: '100%', width: '100%' }} />
      {!ready && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading editor…
        </div>
      )}
    </div>
  )
}

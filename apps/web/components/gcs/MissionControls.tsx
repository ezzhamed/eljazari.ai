'use client'

/**
 * MissionControls — WebSocket-driven mission command panel.
 *
 * Buttons: ARM · TAKEOFF · LAND · RTL · PAUSE · E-STOP
 * Each button fires a socket.io event via the store's socket reference.
 * E-STOP is always enabled; others follow an allowed-state matrix.
 */

import { useCallback, useState } from 'react'
import { ArrowDownToLine, House, KeyRound, OctagonX, Pause, PlaneTakeoff, type LucideIcon } from 'lucide-react'
import { useDroneStore } from '@/lib/stores/drone-store'
import { useWSStore } from '@/lib/stores/ws-store'

type CommandId = 'arm' | 'takeoff' | 'land' | 'rtl' | 'pause' | 'estop'

interface CommandDef {
  id:      CommandId
  label:   string
  icon:    LucideIcon
  color:   string
  allowed: string[]   // currentPhase values where this button is enabled
  confirm: boolean    // require confirmation dialog
}

const COMMANDS: CommandDef[] = [
  {
    id: 'arm', label: 'ARM', icon: KeyRound,
    color: 'border-info/30 text-info hover:bg-info/15',
    allowed: ['idle', 'ready'],
    confirm: false,
  },
  {
    id: 'takeoff', label: 'TAKEOFF', icon: PlaneTakeoff,
    color: 'border-success/30 text-success hover:bg-success/15',
    allowed: ['armed'],
    confirm: false,
  },
  {
    id: 'land', label: 'LAND', icon: ArrowDownToLine,
    color: 'border-warning/30 text-warning hover:bg-warning/15',
    allowed: ['simulation', 'running', 'flying'],
    confirm: false,
  },
  {
    id: 'rtl', label: 'RTL', icon: House,
    color: 'border-warning/30 text-warning hover:bg-warning/15',
    allowed: ['simulation', 'running', 'flying'],
    confirm: true,
  },
  {
    id: 'pause', label: 'PAUSE', icon: Pause,
    color: 'border-brand-500/30 text-brand-300 hover:bg-brand-500/15',
    allowed: ['simulation', 'running', 'flying'],
    confirm: false,
  },
  {
    id: 'estop', label: 'E-STOP', icon: OctagonX,
    color: 'border-destructive/30 text-destructive hover:bg-destructive/15 font-bold',
    allowed: ['*'],   // always allowed
    confirm: true,
  },
]

// ── Sub-components ────────────────────────────────────────────

function ConfirmDialog({
  command,
  onConfirm,
  onCancel,
}: {
  command: CommandDef
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="rounded-xl border border-border bg-popover p-5 shadow-xl max-w-xs w-full mx-4">
        <div className="mb-2 flex items-center gap-2 text-sm text-foreground">
          <command.icon className="h-4 w-4" aria-hidden /> Confirm <span className="font-semibold">{command.label}</span>
        </div>
        <div className="text-xs text-muted-foreground mb-4">
          {command.id === 'estop'
            ? 'This will immediately cut all motors. Are you sure?'
            : `Send ${command.label} command to the drone?`}
        </div>
        <div className="flex gap-3">
          <button
            onClick={onConfirm}
            className="flex-1 rounded-lg border border-destructive/30 bg-destructive/10 py-2 text-xs font-semibold text-destructive transition-colors hover:bg-destructive/15"
          >
            Confirm
          </button>
          <button
            onClick={onCancel}
            className="flex-1 rounded-lg border border-border py-2 text-xs text-muted-foreground transition-colors hover:bg-white/[0.06] hover:text-foreground"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Public component ──────────────────────────────────────────

interface MissionControlsProps {
  className?: string
}

export default function MissionControls({ className = '' }: MissionControlsProps) {
  const { currentPhase } = useDroneStore()
  const { socket, connected } = useWSStore()
  const [pending, setPending] = useState<CommandDef | null>(null)
  const [lastSent, setLastSent] = useState<string | null>(null)

  const isEnabled = useCallback(
    (cmd: CommandDef) => {
      if (cmd.allowed.includes('*')) return true
      return cmd.allowed.some(p => currentPhase.includes(p))
    },
    [currentPhase],
  )

  const sendCommand = useCallback(
    (cmd: CommandDef) => {
      if (socket) {
        socket.emit('gcs:command', { command: cmd.id, timestamp: Date.now() })
      }
      setLastSent(cmd.id)
      setTimeout(() => setLastSent(null), 1500)
    },
    [socket],
  )

  const handleClick = useCallback(
    (cmd: CommandDef) => {
      if (!isEnabled(cmd)) return
      if (cmd.confirm) {
        setPending(cmd)
      } else {
        sendCommand(cmd)
      }
    },
    [isEnabled, sendCommand],
  )

  const handleConfirm = useCallback(() => {
    if (pending) sendCommand(pending)
    setPending(null)
  }, [pending, sendCommand])

  return (
    <>
      <div className={`rounded-xl border border-border bg-card overflow-hidden ${className}`}>
        {/* Header */}
        <div className="flex min-h-[3rem] items-center justify-between gap-3 border-b border-border px-4 py-2.5">
          <span className="text-sm font-medium text-foreground">Mission controls</span>
          <span className="text-2xs text-muted-foreground">Run phase: {currentPhase.replace('_', ' ')}</span>
        </div>

        {/* Buttons */}
        <div className="grid grid-cols-3 gap-2 p-3">
          {COMMANDS.map(cmd => {
            const enabled = isEnabled(cmd)
            const active  = lastSent === cmd.id
            return (
              <button
                key={cmd.id}
                onClick={() => handleClick(cmd)}
                disabled={!enabled}
                className={`
                  relative flex flex-col items-center gap-1 py-3 px-2 rounded-lg
                  border text-xs transition-all duration-150
                  ${enabled ? cmd.color : 'border-border text-muted-foreground/60 cursor-not-allowed'}
                  ${active ? 'ring-1 ring-white/20 scale-95' : ''}
                `}
              >
                <cmd.icon className="h-4 w-4" aria-hidden />
                <span className="text-2xs font-semibold tracking-wider">{cmd.label}</span>
                {active && (
                  <span className="absolute top-1 right-1 w-1.5 h-1.5 rounded-full bg-success animate-ping" />
                )}
              </button>
            )
          })}
        </div>

        {/* Connection status */}
        <div className="px-3 pb-3">
          <div className="flex items-center gap-2 bg-background/60 rounded-lg px-3 py-2">
            <span className={`w-2 h-2 rounded-full ${connected ? 'bg-success' : 'bg-destructive'}`} />
            <span className="text-2xs text-muted-foreground">
              {connected ? 'Connected to the backend' : 'Not connected to the backend'}
            </span>
          </div>
        </div>
      </div>

      {/* Confirm dialog */}
      {pending && (
        <ConfirmDialog
          command={pending}
          onConfirm={handleConfirm}
          onCancel={() => setPending(null)}
        />
      )}
    </>
  )
}

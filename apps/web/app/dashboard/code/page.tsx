'use client'

import dynamic from 'next/dynamic'
import { Code2, FileCode2, Gauge, ShieldCheck } from 'lucide-react'
import { ReviewCard } from '@/components/app/AgentPanel'
import { RestoringState } from '@/components/app/RestoringState'
import { Badge, type BadgeTone } from '@/components/ui/badge'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { EmptyState } from '@/components/ui/empty-state'
import { KeyValueList } from '@/components/ui/key-value'
import { PageBody, PageHeader } from '@/components/ui/page'
import { useDroneStore, type CodeStatus } from '@/lib/stores/drone-store'

const CodeEditor = dynamic(() => import('@/components/code/CodeEditor'), { ssr: false })

const STATUS: Record<CodeStatus, { label: string; tone: BadgeTone }> = {
  pending: { label: 'Not started', tone: 'neutral' },
  generating: { label: 'Generating', tone: 'brand' },
  ready: { label: 'Ready for review', tone: 'warning' },
  approved: { label: 'Approved', tone: 'success' },
}

const SEVERITY_TONE: Record<string, BadgeTone> = {
  critical: 'danger',
  high: 'danger',
  medium: 'warning',
  low: 'info',
}

/**
 * The Safety layer's own defaults (config/settings.py). A deployment can change
 * them in its environment, which this page cannot see, hence the label.
 */
const DEFAULT_LIMITS = [
  { label: 'Max altitude', value: '120 m' },
  { label: 'Max speed', value: '15 m/s' },
  { label: 'Max geofence radius', value: '500 m' },
  { label: 'Min battery', value: '20 %' },
]

export default function CodePage() {
  const generatedCode = useDroneStore((s) => s.generatedCode)
  const safetyReport = useDroneStore((s) => s.safetyReport)
  const codeStatus = useDroneStore((s) => s.codeStatus)
  const codeIteration = useDroneStore((s) => s.codeIteration)
  const phase = useDroneStore((s) => s.currentPhase)
  const hitlPending = useDroneStore((s) => s.hitlPending)
  const isRestoring = useDroneStore((s) => s.isRestoring)
  const status = STATUS[codeStatus] ?? STATUS.pending

  return (
    <PageBody fill>
      <PageHeader
        title="Code & safety"
        icon={Code2}
        description="The PX4 / MAVSDK mission the Code agent wrote, and what the Safety layer found and added."
        status={<Badge tone={status.tone} dot={codeStatus === 'generating' ? 'pulse' : codeStatus !== 'pending'}>{status.label}</Badge>}
        actions={codeIteration > 0 ? <Badge>Version {codeIteration}</Badge> : undefined}
      />

      {hitlPending && phase === 'review_code' && <ReviewCard inline />}

      <div className="grid min-h-0 flex-1 gap-4 2xl:grid-cols-[minmax(0,1fr)_340px]">
        <Card className="min-h-[520px] overflow-hidden">
          <CardHeader
            title="mission.py"
            icon={FileCode2}
            description="Python · MAVSDK · read-only"
            actions={generatedCode ? <span className="text-2xs tabular-nums text-muted-foreground">{generatedCode.split('\n').length} lines</span> : undefined}
          />
          <div className="min-h-0 flex-1">
            {generatedCode ? (
              <CodeEditor code={generatedCode} language="python" readOnly height="100%" />
            ) : isRestoring ? (
              <RestoringState />
            ) : (
              <EmptyState icon={FileCode2} title="No mission code yet" description="The code appears here once the design is approved and the Code agent finishes." />
            )}
          </div>
        </Card>

        <div className="grid min-h-0 content-start items-start gap-4 lg:grid-cols-2 2xl:flex 2xl:flex-col 2xl:overflow-y-auto">
          <Card>
            <CardHeader
              title="Safety report"
              icon={ShieldCheck}
              actions={
                safetyReport ? (
                  <Badge tone={safetyReport.passed ? 'success' : 'danger'}>{safetyReport.passed ? 'Passed' : 'Failed'}</Badge>
                ) : (
                  <Badge>Waiting</Badge>
                )
              }
            />
            <CardContent className="space-y-4">
              {!safetyReport ? (
                <p className="text-xs text-muted-foreground">The Safety layer reports after the code is written.</p>
              ) : (
                <>
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-muted-foreground">Risk level</span>
                    <Badge tone={SEVERITY_TONE[safetyReport.risk_level] ?? 'neutral'}>{safetyReport.risk_level}</Badge>
                  </div>

                  <div>
                    <p className="eyebrow mb-2">Issues ({safetyReport.issues.length})</p>
                    {safetyReport.issues.length === 0 ? (
                      <p className="text-xs text-muted-foreground">None found.</p>
                    ) : (
                      <ul className="space-y-2">
                        {safetyReport.issues.map((issue, i) => (
                          <li key={`${issue.rule_id}-${i}`} className="rounded-lg border border-border bg-background/60 p-3">
                            <div className="flex items-center justify-between gap-2">
                              <span className="font-mono text-2xs text-muted-foreground">
                                {issue.rule_id}
                                {issue.line_number ? ` · line ${issue.line_number}` : ''}
                              </span>
                              <Badge tone={SEVERITY_TONE[issue.severity] ?? 'neutral'}>{issue.severity}</Badge>
                            </div>
                            <p className="mt-1.5 text-xs leading-5 text-foreground">{issue.description}</p>
                            {issue.suggestion && <p className="mt-1 text-xs leading-5 text-muted-foreground">{issue.suggestion}</p>}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>

                  <div>
                    <p className="eyebrow mb-2">Protocols injected ({safetyReport.injected_protocols.length})</p>
                    <div className="flex flex-wrap gap-1.5">
                      {safetyReport.injected_protocols.map((protocol) => (
                        <Badge key={protocol} tone="brand">{protocol.replace(/_/g, ' ')}</Badge>
                      ))}
                    </div>
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader title="Limits" icon={Gauge} description="Safety-layer defaults; a deployment may change them" />
            <CardContent>
              <KeyValueList items={DEFAULT_LIMITS} />
            </CardContent>
          </Card>
        </div>
      </div>
    </PageBody>
  )
}

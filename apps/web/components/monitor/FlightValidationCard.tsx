'use client'

import { ShieldCheck } from 'lucide-react'
import type { FlightValidation } from '@eljazari/shared-types'
import { Badge, type BadgeTone } from '@/components/ui/badge'
import { Card, CardContent, CardHeader } from '@/components/ui/card'

const STATUS: Record<FlightValidation['status'], { label: string; tone: BadgeTone }> = {
  passed: { label: 'Basic checks passed', tone: 'success' },
  rejected_by_checks: { label: 'Rejected by checks', tone: 'danger' },
  blocked: { label: 'Blocked — not flown', tone: 'warning' },
  unknown: { label: 'Not evaluated', tone: 'neutral' },
}

function aircraftSummary(aircraft: FlightValidation['aircraft']): { text: string; tone: string } {
  if (aircraft.kind === 'generated') {
    if (!aircraft.identity_attested) {
      return { text: 'Generated design — the simulator did not attest loading it', tone: 'text-destructive' }
    }
    const mass =
      typeof aircraft.simulated_total_mass_kg === 'number'
        ? ` · ${aircraft.simulated_total_mass_kg.toFixed(3)} kg in simulation`
        : ''
    return {
      text: `Generated design ${aircraft.model_instance ?? ''}${mass} · identity attested`,
      tone: 'text-success',
    }
  }
  if (aircraft.kind === 'reference_x500') {
    return {
      text: 'Reference x500 — this flight does not validate a generated design',
      tone: 'text-warning',
    }
  }
  if (aircraft.kind === 'not_flown') return { text: 'No aircraft flew', tone: 'text-muted-foreground' }
  return { text: 'Aircraft identity unknown', tone: 'text-muted-foreground' }
}

/** What actually flew, and whether the deterministic gate accepted the run. */
export function FlightValidationCard({ validation }: { validation: FlightValidation }) {
  const status = STATUS[validation.status] ?? STATUS.unknown
  const aircraft = aircraftSummary(validation.aircraft)
  const sandboxed = validation.mission_isolation === 'linux_namespaces'
  const reasons = validation.reason ? [validation.reason] : validation.acceptance_reasons

  return (
    <Card aria-label="Flight validation" role="region">
      <CardHeader
        title="Flight validation"
        icon={ShieldCheck}
        description="Deterministic acceptance gate"
        actions={
          <Badge data-testid="validation-status" tone={status.tone}>
            {status.label}
          </Badge>
        }
      />
      <CardContent className="space-y-3">
        <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-xs">
          <dt className="text-muted-foreground">Aircraft</dt>
          <dd className={aircraft.tone}>{aircraft.text}</dd>
          {validation.aircraft.validation_scope === 'sitl_catalog_estimate' && (
            <>
              <dt className="text-muted-foreground">Model scope</dt>
              <dd className="text-muted-foreground">
                SITL catalog estimate — unverified catalog curves, not calibrated or airworthy
              </dd>
            </>
          )}
          {validation.status !== 'blocked' && (
            <>
              <dt className="text-muted-foreground">Mission code</dt>
              <dd className={sandboxed ? 'text-success' : 'text-destructive'}>
                {sandboxed ? 'Ran in the isolated sandbox' : 'Did not run in the isolated sandbox'}
              </dd>
            </>
          )}
        </dl>
        {reasons.length > 0 && validation.status !== 'passed' && (
          <ul className="list-disc space-y-1 rounded-lg border border-border bg-background/60 py-2.5 pl-8 pr-3 text-xs leading-5 text-muted-foreground">
            {reasons.slice(0, 4).map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

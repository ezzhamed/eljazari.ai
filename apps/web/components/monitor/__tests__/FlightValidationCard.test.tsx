import React from 'react'
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import '@testing-library/jest-dom'
import type { FlightValidation } from '@eljazari/shared-types'
import { FlightValidationCard } from '../FlightValidationCard'

const base: FlightValidation = {
  status: 'passed',
  reason: null,
  aircraft: {
    kind: 'generated',
    model_instance: 'quadfm_77aa9fc9ee789aa5_0',
    validation_scope: 'sitl_catalog_estimate',
    simulated_total_mass_kg: 0.6263,
    identity_attested: true,
  },
  mission_isolation: 'linux_namespaces',
  acceptance_reasons: [],
}

describe('FlightValidationCard', () => {
  it('names the generated aircraft that flew and its limited scope', () => {
    render(<FlightValidationCard validation={base} />)
    expect(screen.getByTestId('validation-status')).toHaveTextContent('Basic checks passed')
    expect(screen.getByText(/quadfm_77aa9fc9ee789aa5_0/)).toHaveTextContent('identity attested')
    expect(screen.getByText(/not calibrated or airworthy/)).toBeInTheDocument()
    expect(screen.getByText('Ran in the isolated sandbox')).toBeInTheDocument()
  })

  it('does not let a reference x500 flight pass for the design', () => {
    render(
      <FlightValidationCard
        validation={{
          ...base,
          aircraft: { kind: 'reference_x500', identity_attested: false },
        }}
      />,
    )
    expect(screen.getByText(/does not validate a generated design/)).toBeInTheDocument()
  })

  it('shows why a run was blocked or rejected instead of a zero score', () => {
    const { rerender } = render(
      <FlightValidationCard
        validation={{
          ...base,
          status: 'blocked',
          reason: 'Mission code has no recorded approval; it will not be executed.',
          aircraft: { kind: 'not_flown', identity_attested: false },
          mission_isolation: null,
        }}
      />,
    )
    expect(screen.getByTestId('validation-status')).toHaveTextContent('Blocked')
    expect(screen.getByText(/no recorded approval/)).toBeInTheDocument()
    expect(screen.queryByText(/sandbox/)).not.toBeInTheDocument()

    rerender(
      <FlightValidationCard
        validation={{
          ...base,
          status: 'rejected_by_checks',
          mission_isolation: 'none',
          acceptance_reasons: ['Mission code did not run in the isolated sandbox'],
        }}
      />,
    )
    expect(screen.getByTestId('validation-status')).toHaveTextContent('Rejected by checks')
    expect(screen.getByText('Did not run in the isolated sandbox')).toBeInTheDocument()
  })
})

import { NextRequest, NextResponse } from 'next/server'

import { API_BASE_URL as API_BASE } from '@/lib/config'

export async function POST(req: NextRequest) {
  const body = await req.json()

  const response = await fetch(`${API_BASE}/api/agents/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  const data = await response.json()
  return NextResponse.json(data, { status: response.status })
}

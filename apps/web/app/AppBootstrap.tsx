'use client'

import { ProjectHydrator } from '@/components/app/ProjectHydrator'
import { useEnsureToken } from '@/lib/hooks/useProject'
import { useDroneStore } from '@/lib/stores/drone-store'
import { useConnectWS } from '@/lib/stores/ws-store'

export default function AppBootstrap() {
  useEnsureToken()
  const projectId = useDroneStore((state) => state.projectId)
  useConnectWS(projectId)
  return <ProjectHydrator />
}

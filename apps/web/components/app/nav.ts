import {
  BarChart3,
  Box,
  Code2,
  Gamepad2,
  LayoutDashboard,
  Layers,
  Radar,
  type LucideIcon,
} from 'lucide-react'
import type { StageId } from '@/lib/pipeline'

export interface NavItem {
  href: string
  label: string
  icon: LucideIcon
  /** Pipeline stages this page shows; its nav entry carries their status. */
  stages?: StageId[]
}

export const NAV_SECTIONS: { title: string; items: NavItem[] }[] = [
  {
    title: 'Run',
    items: [{ href: '/dashboard', label: 'Overview', icon: LayoutDashboard }],
  },
  {
    title: 'Pipeline',
    items: [
      { href: '/dashboard/design', label: 'Design', icon: Layers, stages: ['design'] },
      { href: '/dashboard/viewer', label: '3D model', icon: Box, stages: ['urdf_conversion'] },
      { href: '/dashboard/code', label: 'Code & safety', icon: Code2, stages: ['code_generation', 'safety_check'] },
      { href: '/dashboard/simulate', label: 'Simulation', icon: Radar, stages: ['simulation'] },
      { href: '/dashboard/monitor', label: 'Performance', icon: BarChart3, stages: ['monitoring'] },
    ],
  },
  {
    title: 'Operations',
    items: [{ href: '/dashboard/gcs', label: 'Ground control', icon: Gamepad2 }],
  },
]

export const NAV_ITEMS = NAV_SECTIONS.flatMap((section) => section.items)

/** The nav entry for a path: exact match first, then the longest prefix. */
export function navItemFor(pathname: string): NavItem | undefined {
  return (
    NAV_ITEMS.find((item) => item.href === pathname) ??
    [...NAV_ITEMS]
      .filter((item) => item.href !== '/dashboard' && pathname.startsWith(item.href))
      .sort((a, b) => b.href.length - a.href.length)[0]
  )
}

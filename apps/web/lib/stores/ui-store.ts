'use client'

import { create } from 'zustand'

const AGENT_PANEL_KEY = 'dp_agent_panel_open'

interface UIStore {
  /** Right-hand agent panel in the workspace. */
  agentPanelOpen: boolean
  /** Open or close by the reader's choice; remembered. */
  setAgentPanelOpen: (open: boolean) => void
  toggleAgentPanel: () => void
  /** Open because the pipeline needs a review; not remembered as a preference. */
  revealAgentPanel: () => void
  /** Read the saved preference; called once on mount (not during render, to keep SSR stable). */
  loadPreferences: () => void
}

function save(open: boolean) {
  try {
    window.localStorage.setItem(AGENT_PANEL_KEY, open ? '1' : '0')
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
}

export const useUIStore = create<UIStore>((set, get) => ({
  // Closed until the preference is read, so a narrow screen never flashes the overlay.
  agentPanelOpen: false,
  setAgentPanelOpen: (open) => {
    save(open)
    set({ agentPanelOpen: open })
  },
  toggleAgentPanel: () => get().setAgentPanelOpen(!get().agentPanelOpen),
  revealAgentPanel: () => set({ agentPanelOpen: true }),
  loadPreferences: () => {
    let saved: string | null = null
    try {
      saved = window.localStorage.getItem(AGENT_PANEL_KEY)
    } catch {
      /* storage unavailable */
    }
    // Without a saved choice the panel starts open only where the page keeps
    // its two-column layout beside it (2xl, 1536 px). Below xl it is an overlay.
    set({ agentPanelOpen: saved !== null ? saved === '1' : window.innerWidth >= 1536 })
  },
}))

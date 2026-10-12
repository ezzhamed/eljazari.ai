/** Keep the current session usable when browser storage is unavailable. */
const memory = new Map<string, string>()
let storageUnavailable = false

export function readSessionValue(key: string): string | null {
  if (typeof window === 'undefined') return null
  if (!storageUnavailable) {
    try {
      const value = window.localStorage.getItem(key)
      if (value === null) memory.delete(key)
      else memory.set(key, value)
      return value
    } catch {
      storageUnavailable = true
    }
  }
  return memory.get(key) ?? null
}

export function writeSessionValue(key: string, value: string | null): void {
  if (typeof window === 'undefined') return
  if (value === null) memory.delete(key)
  else memory.set(key, value)
  if (storageUnavailable) return
  try {
    if (value === null) window.localStorage.removeItem(key)
    else window.localStorage.setItem(key, value)
  } catch {
    // Stay in memory for this page lifetime, including after logout, so a
    // failed removal cannot restore an old token from persistent storage.
    storageUnavailable = true
  }
}

import { createContext, useContext } from 'react'

// The header element a page's identity trail renders into. The shell owns the slot, the page owns
// the trail and its data — so the shell needs no knowledge of routes, ids or fetches.
//
// `null` is a normal value, not an error: a page rendered without the shell (every page test does
// that) finds no slot and keeps the trail inline, which is also the pre-shell behaviour.
export const NavTrailSlotContext = createContext<HTMLElement | null>(null)

export function useNavTrailSlot(): HTMLElement | null {
  return useContext(NavTrailSlotContext)
}

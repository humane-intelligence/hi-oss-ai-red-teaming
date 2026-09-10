import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { InfoHint } from './info-hint'

const TEXT =
  'Each turn resends the whole conversation so far, so later turns re-count earlier ones.'

describe('InfoHint', () => {
  it('exposes the explanation as the button name, not as a bare "info"', () => {
    render(<InfoHint text={TEXT} />)

    // A screen-reader user gets the explanation without opening anything.
    expect(screen.getByRole('button', { name: TEXT })).toBeInTheDocument()
  })

  it('wires the trigger to the panel it opens', () => {
    render(<InfoHint text={TEXT} />)

    // `popovertarget` pointing at no element opens nothing, and nothing else catches that.
    const target = screen.getByRole('button', { name: TEXT }).getAttribute('popovertarget')
    expect(target).toBeTruthy()
    const panel = document.getElementById(target!)
    expect(panel).not.toBeNull()
    expect(panel).toHaveAttribute('popover', 'auto')
    expect(panel).toHaveTextContent(TEXT)
    // Focusable because it scrolls — opening a popover does not move focus, so without this a
    // clipped panel has no keyboard route to its own scrollbar.
    expect(panel).toHaveAttribute('tabindex', '0')
  })

  it('names the trigger with `label` and still describes it with the text', () => {
    // `label` exists for machine output: a provider error as the button's *name* announces a blob.
    // The text has to stay reachable, and via the panel rather than the `title` attribute, which
    // AT expose inconsistently.
    render(<InfoHint text={TEXT} label="Why image input is unconfirmed" />)

    const trigger = screen.getByRole('button', { name: 'Why image input is unconfirmed' })
    // The attribute is what this pins: jsdom has no popover UA stylesheet, so the panel is not
    // hidden here and the computed description alone would pass with no relation wired at all.
    // That hidden-node behaviour was verified in Chrome, not here.
    expect(trigger).toHaveAttribute('aria-describedby', trigger.getAttribute('popovertarget'))
    expect(trigger).toHaveAccessibleDescription(TEXT)
  })

  it('wires no description relation without `label`, where `title` already supplies one', () => {
    // The explicit relation exists for the `label` case only. Label-less callers keep `title` for
    // the hover tooltip, and `title` *is* the accessible description when nothing else supplies
    // one — so name and description duplicate here, which is the trade for keeping the tooltip.
    render(<InfoHint text={TEXT} />)

    const trigger = screen.getByRole('button', { name: TEXT })
    expect(trigger).not.toHaveAttribute('aria-describedby')
    expect(trigger).toHaveAccessibleDescription(TEXT)
  })

  it('gives each instance its own anchor name', () => {
    // A shared anchor name would make both panels position against one trigger.
    const { container } = render(
      <>
        <InfoHint text="first" />
        <InfoHint text="second" />
      </>,
    )

    const anchors = [...container.querySelectorAll<HTMLElement>('[style*="--hint-anchor"]')].map(
      (el) => el.style.getPropertyValue('--hint-anchor'),
    )
    expect(anchors.length).toBeGreaterThan(0)
    expect(anchors.every((a) => /^--hint-[a-zA-Z0-9]+$/.test(a))).toBe(true)
    expect(new Set(anchors).size).toBe(2)
  })
})

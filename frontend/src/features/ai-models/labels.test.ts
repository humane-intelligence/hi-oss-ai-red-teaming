import { describe, expect, it } from 'vitest'
import { modalitySummary, modelLabelSummary, modelStatus } from './labels'

describe('modalitySummary', () => {
  it('renders both directions around an arrow', () => {
    expect(modalitySummary(['text', 'image'], ['text'])).toBe('Text, Image → Text')
  })
})

describe('modelLabelSummary', () => {
  it.each([
    { labels: [], expected: '' },
    { labels: ['self-hosted'], expected: 'self-hosted' },
    { labels: ['self-hosted', 'audited'], expected: 'self-hosted, audited' },
    { labels: ['self-hosted', 'audited', 'eu-only'], expected: 'self-hosted, audited +1' },
  ])('renders $labels as "$expected"', ({ labels, expected }) => {
    expect(modelLabelSummary(labels)).toBe(expected)
  })

  it('bounds the string a model wearing the full 20 produces', () => {
    // The point of summarising: unbounded, this is the ~1300-character accessible name a screen
    // reader would read out for one option.
    const full = Array.from({ length: 20 }, (_, i) => 'x'.repeat(64) + i)
    expect(modelLabelSummary(full)).toBe(`${full[0]}, ${full[1]} +18`)
    expect(modelLabelSummary(full).length).toBeLessThan(140)
  })
})

describe('modelStatus', () => {
  it.each([
    { is_disabled: true, output_modalities: ['text'], label: 'disabled' },
    // The operator switch wins: a row turned off is off whatever it can produce.
    { is_disabled: true, output_modalities: ['image'], label: 'disabled' },
    { is_disabled: false, output_modalities: ['image'], label: 'no text output' },
    { is_disabled: false, output_modalities: ['text'], label: 'enabled' },
    { is_disabled: false, output_modalities: ['text', 'image'], label: 'enabled' },
  ])('reads $label', ({ label, ...model }) => {
    expect(modelStatus(model).label).toBe(label)
  })
})

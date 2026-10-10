import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import * as client from '../api/client'
import lotInfo from '../api/__fixtures__/lot-info.json'
import type { LotInfo } from '../api/types'
import { isLotInfo } from '../api/validate'
import en from '../i18n/locales/en.json'

function renderAt(hash: string) {
  window.location.hash = hash
  return render(<App />)
}

// P8.9 (frontend.md §2.5): the static Privacy screen, its links from the footer and the Alerts
// screen, and the operator's name and contact from GET /api/lot.
describe('PrivacyScreen', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    window.location.hash = ''
  })

  it('says what is processed and stored and that the location stays on the phone', async () => {
    renderAt('#/privacy')
    expect(await screen.findByRole('heading', { name: 'Privacy', level: 1 })).toBeInTheDocument()
    for (const name of [
      'The cameras at the lot',
      'What is stored',
      'Your location',
      'Notifications',
      'On this device',
      'Technical logs',
      'Who is responsible',
    ]) {
      expect(screen.getByRole('heading', { name, level: 2 })).toBeInTheDocument()
    }
    expect(screen.getByText(/No video is recorded/)).toBeInTheDocument()
    expect(screen.getByText(/No number plates are read/)).toBeInTheDocument()
    expect(screen.getByText(/never sent to the server and never stored/)).toBeInTheDocument()
    // every text of the locale file is on the screen: no numbered key is left out or missing
    const texts = Object.entries(en.privacy).filter(([key]) => /_\d+$/.test(key))
    expect(screen.getAllByRole('listitem').filter((li) => !li.closest('nav'))).toHaveLength(
      texts.length,
    )
    for (const [, text] of texts) expect(screen.getByText(text)).toBeInTheDocument()
    // the mock lot names no operator: the signs at the lot do
    expect(screen.getByText(/on the signs at the lot/)).toBeInTheDocument()
    expect(screen.queryByText('Contact:')).not.toBeInTheDocument()
  })

  it('names the operator and the contact the server is set up with', async () => {
    const lot = {
      ...lotInfo,
      privacy: { operator: 'Example SRL', contact: 'privacy@example.com' },
    } as LotInfo
    vi.spyOn(client, 'getLot').mockResolvedValue(lot)
    renderAt('#/privacy')
    expect(
      await screen.findByText('The cameras and this app are operated by Example SRL.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'privacy@example.com' })).toHaveAttribute(
      'href',
      'mailto:privacy@example.com',
    )
    expect(screen.queryByText(/on the signs at the lot/)).not.toBeInTheDocument()
  })

  it('shows a contact that is not an e-mail address as plain text', async () => {
    const lot = {
      ...lotInfo,
      privacy: { operator: null, contact: 'the office, door 3' },
    } as LotInfo
    vi.spyOn(client, 'getLot').mockResolvedValue(lot)
    renderAt('#/privacy')
    expect(await screen.findByText(/the office, door 3/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /office/ })).not.toBeInTheDocument()
    expect(screen.getByText(/on the signs at the lot/)).toBeInTheDocument()
  })

  it('still shows the text when the server is unreachable', async () => {
    vi.spyOn(client, 'getLot').mockRejectedValue(new Error('down'))
    renderAt('#/privacy')
    expect(await screen.findByText(/No video is recorded/)).toBeInTheDocument()
    expect(screen.getByText(/on the signs at the lot/)).toBeInTheDocument()
  })

  it('is linked from the footer of every screen and from the Alerts screen', async () => {
    renderAt('#/')
    fireEvent.click(screen.getByRole('link', { name: 'Privacy' }))
    expect(await screen.findByRole('heading', { name: 'Privacy' })).toBeInTheDocument()
    expect(window.location.hash).toBe('#/privacy')

    fireEvent.click(screen.getByRole('link', { name: 'Alerts' }))
    expect(screen.getByText(/Your location never leaves this phone/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: 'How your data is handled' }))
    expect(await screen.findByRole('heading', { name: 'Privacy' })).toBeInTheDocument()
  })

  it('accepts /api/lot with and without the privacy block', () => {
    const { privacy, ...older } = lotInfo
    expect(privacy).toEqual({ operator: null, contact: null })
    expect(isLotInfo(older)).toBe(true)
    expect(isLotInfo({ ...older, privacy: { operator: 'A', contact: null } })).toBe(true)
    expect(isLotInfo({ ...older, privacy: { operator: 1, contact: null } })).toBe(false)
    expect(isLotInfo({ ...older, privacy: null })).toBe(false)
  })
})

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import App from './App'

function renderAt(hash: string) {
  window.location.hash = hash
  return render(<App />)
}

describe('App shell', () => {
  afterEach(() => {
    window.location.hash = ''
  })

  it('shows the header, the Live screen and three tabs', () => {
    renderAt('#/')
    expect(screen.getByText('Parking')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Connecting')
    expect(screen.getByRole('heading', { name: 'Live' })).toBeInTheDocument()
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(nav.querySelectorAll('a')).toHaveLength(3)
    expect(screen.getByRole('link', { name: 'Live' })).toHaveAttribute('aria-current', 'page')
  })

  it('switches screens from the bottom nav', () => {
    renderAt('#/')
    fireEvent.click(screen.getByRole('link', { name: 'Alerts' }))
    expect(screen.getByRole('heading', { name: 'Alerts' })).toBeInTheDocument()
    expect(window.location.hash).toBe('#/alerts')
    expect(screen.getByRole('link', { name: 'Alerts' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'Live' })).not.toHaveAttribute('aria-current')

    fireEvent.click(screen.getByRole('link', { name: 'Stats' }))
    expect(screen.getByRole('heading', { name: 'Stats' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('link', { name: 'Live' }))
    expect(screen.getByRole('heading', { name: 'Live' })).toBeInTheDocument()
  })

  it.each([
    ['#/privacy', 'Privacy'],
    ['#/admin', 'Admin'],
    ['#/admin/cameras/cam-ground', 'Admin'],
  ])('routes %s without a tab selected', (hash, heading) => {
    renderAt(hash)
    expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument()
    for (const tab of ['Live', 'Alerts', 'Stats']) {
      expect(screen.getByRole('link', { name: tab })).not.toHaveAttribute('aria-current')
    }
  })

  it('sends unknown routes to Live', async () => {
    renderAt('#/nope')
    await act(async () => {})
    expect(screen.getByRole('heading', { name: 'Live' })).toBeInTheDocument()
    expect(window.location.hash).toBe('#/')
  })
})

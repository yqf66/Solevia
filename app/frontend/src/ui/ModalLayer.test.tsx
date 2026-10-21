import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ModalLayer } from './ModalLayer'

afterEach(cleanup)

describe('ModalLayer', () => {
  it('renders overlays outside the page layout and removes them on close', () => {
    const page = (open: boolean) => <section className="planner-stage-page">
      <div className="planner-primary-column">Main</div>
      <aside className="stage-context-column">Tools</aside>
      {open && <ModalLayer><div role="dialog">Preview</div></ModalLayer>}
    </section>
    const { container, rerender } = render(page(true))

    expect(container.querySelector('.planner-stage-page')?.children).toHaveLength(2)
    expect(screen.getByRole('dialog').parentElement).toBe(document.body)

    rerender(page(false))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(container.querySelector('.planner-stage-page')?.children).toHaveLength(2)
  })
})

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DataTable, type Column } from './data-table'

type Row = { id: string; name: string }

const columns: Column<Row>[] = [
  { header: 'ID', cell: (r) => r.id },
  { header: 'Name', cell: (r) => r.name },
]

const rows: Row[] = [
  { id: '1', name: 'Alice' },
  { id: '2', name: 'Bob' },
]

// The skeleton used to pass its own `px-3 py-2`, which tailwind-merge let win over the cell's
// `px-2 sm:px-3`, so the placeholder row sat 8px per cell wider than the data that replaced it.
describe('DataTable skeleton geometry', () => {
  it('pads skeleton cells exactly like loaded ones', () => {
    const cols: Column<{ id: string; a: string }>[] = [
      { id: 'a', label: 'A', header: 'A', cell: (r) => r.a },
    ]
    const { unmount } = render(
      <DataTable columns={cols} rows={undefined} rowKey={(r) => r.id} isLoading />,
    )
    const skeleton = screen.getAllByTestId('datatable-skeleton')[0]!.querySelector('td')!.className
    unmount()

    render(<DataTable columns={cols} rows={[{ id: '1', a: 'x' }]} rowKey={(r) => r.id} />)
    const loaded = screen.getByText('x').closest('td')!.className

    const padding = (c: string) =>
      (c.match(/(?:^|\s)(?:sm:)?px-\d+/g) ?? []).map((x) => x.trim()).sort()
    expect(padding(skeleton)).toEqual(padding(loaded))
  })
})

describe('DataTable', () => {
  it('renders skeleton rows when isLoading', () => {
    render(<DataTable columns={columns} rows={undefined} rowKey={(r) => r.id} isLoading />)
    const skeletons = screen.getAllByTestId('datatable-skeleton')
    expect(skeletons.length).toBeGreaterThan(0)
    expect(screen.queryByText('Nothing here yet.')).toBeNull()
  })

  it('renders data rows when loaded', () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />)
    expect(screen.getByText('Alice')).toBeInTheDocument()
    expect(screen.getByText('Bob')).toBeInTheDocument()
    expect(screen.queryByTestId('datatable-skeleton')).toBeNull()
  })

  it('renders empty label when rows is empty', () => {
    render(<DataTable columns={columns} rows={[]} rowKey={(r) => r.id} emptyLabel="No data." />)
    expect(screen.getByText('No data.')).toBeInTheDocument()
    expect(screen.queryByTestId('datatable-skeleton')).toBeNull()
  })

  it('renders an svg icon in the empty branch', () => {
    const { container } = render(
      <DataTable columns={columns} rows={[]} rowKey={(r) => r.id} emptyLabel="No data." />,
    )
    expect(container.querySelector('svg')).not.toBeNull()
  })

  it('renders emptyAction below the label when empty and emptyAction provided', () => {
    render(
      <DataTable
        columns={columns}
        rows={[]}
        rowKey={(r) => r.id}
        emptyLabel="No data."
        emptyAction={<button>Create</button>}
      />,
    )
    expect(screen.getByText('No data.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create' })).toBeInTheDocument()
  })

  it('does not render emptyAction when rows are present', () => {
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        emptyAction={<button>Create</button>}
      />,
    )
    expect(screen.queryByRole('button', { name: 'Create' })).toBeNull()
  })

  it('makes clickable rows focusable and activates them with Enter/Space', async () => {
    const user = userEvent.setup()
    const onRowClick = vi.fn()
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} onRowClick={onRowClick} />)

    const row = screen.getByText('Alice').closest('tr')!
    expect(row).toHaveAttribute('tabindex', '0')

    row.focus()
    await user.keyboard('{Enter}')
    await user.keyboard(' ')
    expect(onRowClick).toHaveBeenCalledTimes(2)
  })

  it('does not make rows focusable without onRowClick', () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />)
    expect(screen.getByText('Alice').closest('tr')).not.toHaveAttribute('tabindex')
  })

  it('toggles an expanded detail row via the per-row expander', async () => {
    const user = userEvent.setup()
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        renderExpanded={(r) => <span>detail for {r.name}</span>}
      />,
    )

    expect(screen.queryByText('detail for Alice')).toBeNull()
    const [firstExpander] = screen.getAllByRole('button', { name: /expand row/i })
    await user.click(firstExpander!)
    expect(screen.getByText('detail for Alice')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /collapse row/i }))
    expect(screen.queryByText('detail for Alice')).toBeNull()
  })

  it('prunes expansion state when a row leaves, so a reused key is not pre-expanded', async () => {
    const user = userEvent.setup()
    const expanded = (r: Row) => <span>detail for {r.name}</span>
    const { rerender } = render(
      <DataTable
        columns={columns}
        rows={[{ id: '1', name: 'Alice' }]}
        rowKey={(r) => r.id}
        renderExpanded={expanded}
      />,
    )
    await user.click(screen.getByRole('button', { name: /expand row/i }))
    expect(screen.getByText('detail for Alice')).toBeInTheDocument()

    // Row '1' leaves the data set, then a new row reuses key '1'.
    rerender(
      <DataTable
        columns={columns}
        rows={[{ id: '2', name: 'Bob' }]}
        rowKey={(r) => r.id}
        renderExpanded={expanded}
      />,
    )
    rerender(
      <DataTable
        columns={columns}
        rows={[{ id: '1', name: 'Carol' }]}
        rowKey={(r) => r.id}
        renderExpanded={expanded}
      />,
    )
    // The reused key must not carry the old expansion state.
    expect(screen.queryByText('detail for Carol')).toBeNull()
  })

  it('expander click does not trigger onRowClick', async () => {
    const user = userEvent.setup()
    const onRowClick = vi.fn()
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        onRowClick={onRowClick}
        renderExpanded={(r) => <span>detail for {r.name}</span>}
      />,
    )

    const [firstExpander] = screen.getAllByRole('button', { name: /expand row/i })
    await user.click(firstExpander!)
    expect(onRowClick).not.toHaveBeenCalled()
    expect(screen.getByText('detail for Alice')).toBeInTheDocument()
  })

  it('renders error message when isError', () => {
    render(
      <DataTable
        columns={columns}
        rows={undefined}
        rowKey={(r) => r.id}
        isError
        error={new Error('Fetch failed')}
      />,
    )
    expect(screen.getByText(/Fetch failed/)).toBeInTheDocument()
    expect(screen.queryByTestId('datatable-skeleton')).toBeNull()
  })
})

describe('DataTable — sort', () => {
  const sortColumns: Column<Row>[] = [
    { header: 'ID', cell: (r) => r.id },
    { header: 'Name', cell: (r) => r.name, sortKey: 'name' },
  ]

  it('renders a clickable header button when column has sortKey and sort is provided', () => {
    const onChange = vi.fn()
    render(
      <DataTable
        columns={sortColumns}
        rows={rows}
        rowKey={(r) => r.id}
        sort={{ by: undefined, onChange }}
      />,
    )
    expect(screen.getByRole('button', { name: /name/i })).toBeInTheDocument()
    // non-sortable column is not a button
    expect(screen.queryByRole('button', { name: /^id$/i })).toBeNull()
  })

  it('clicking a sortable header calls onChange with the sortKey (asc first)', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <DataTable
        columns={sortColumns}
        rows={rows}
        rowKey={(r) => r.id}
        sort={{ by: undefined, onChange }}
      />,
    )
    await user.click(screen.getByRole('button', { name: /name/i }))
    expect(onChange).toHaveBeenCalledWith('name')
  })

  it('clicking again when asc calls onChange with -sortKey (desc)', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <DataTable
        columns={sortColumns}
        rows={rows}
        rowKey={(r) => r.id}
        sort={{ by: 'name', onChange }}
      />,
    )
    await user.click(screen.getByRole('button', { name: /name/i }))
    expect(onChange).toHaveBeenCalledWith('-name')
  })

  it('reflects sort direction via aria-sort on the header cell', () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <DataTable
        columns={sortColumns}
        rows={rows}
        rowKey={(r) => r.id}
        sort={{ by: 'name', onChange }}
      />,
    )
    expect(screen.getByRole('columnheader', { name: /name/i })).toHaveAttribute(
      'aria-sort',
      'ascending',
    )

    rerender(
      <DataTable
        columns={sortColumns}
        rows={rows}
        rowKey={(r) => r.id}
        sort={{ by: '-name', onChange }}
      />,
    )
    expect(screen.getByRole('columnheader', { name: /name/i })).toHaveAttribute(
      'aria-sort',
      'descending',
    )
  })

  it('sortable header renders as plain text (no button) when sort prop is not provided', () => {
    render(<DataTable columns={sortColumns} rows={rows} rowKey={(r) => r.id} />)
    // no buttons at all — ID and Name both render as plain headers
    expect(screen.queryByRole('button')).toBeNull()
  })
})

describe('DataTable — column visibility', () => {
  const idColumns: Column<Row>[] = [
    { id: 'id', header: 'ID', cell: (r) => r.id },
    { id: 'name', header: 'Name', cell: (r) => r.name },
  ]

  it('hides a column whose id is in hiddenColumns', () => {
    render(
      <DataTable columns={idColumns} rows={rows} rowKey={(r) => r.id} hiddenColumns={['id']} />,
    )
    expect(screen.queryByRole('columnheader', { name: /^id$/i })).toBeNull()
    expect(screen.getByRole('columnheader', { name: /name/i })).toBeInTheDocument()
    expect(screen.getByText('Alice')).toBeInTheDocument()
    expect(screen.queryByText('1')).toBeNull()
  })

  it('renders all columns when hiddenColumns is empty or omitted', () => {
    render(<DataTable columns={idColumns} rows={rows} rowKey={(r) => r.id} hiddenColumns={[]} />)
    expect(screen.getByRole('columnheader', { name: /^id$/i })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: /name/i })).toBeInTheDocument()
  })

  it('never hides a column without an id', () => {
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        hiddenColumns={['id', 'name']}
      />,
    )
    expect(screen.getByRole('columnheader', { name: /^id$/i })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: /name/i })).toBeInTheDocument()
  })
})

describe('DataTable — selection', () => {
  it('renders no selection checkbox when the selection prop is omitted', () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />)
    expect(screen.queryByRole('checkbox')).toBeNull()
  })

  it('toggles a row via its checkbox', async () => {
    const onSelectedChange = vi.fn()
    const user = userEvent.setup()
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        selection={{ selectedKeys: new Set(), onSelectedChange }}
      />,
    )
    const boxes = screen.getAllByRole('checkbox') // [select-all, row-1, row-2]
    await user.click(boxes[1]!)
    expect(onSelectedChange).toHaveBeenCalledWith(new Set(['1']))
  })

  it('keeps selection controls available below the md breakpoint', () => {
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        selection={{ selectedKeys: new Set(), onSelectedChange: vi.fn() }}
      />,
    )

    for (const checkbox of screen.getAllByRole('checkbox')) {
      expect(checkbox.closest('th, td')).not.toHaveClass('hidden')
    }
  })

  it('select-all toggles only selectable rows', async () => {
    const onSelectedChange = vi.fn()
    const user = userEvent.setup()
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        selection={{
          selectedKeys: new Set(),
          onSelectedChange,
          rowSelectable: (r) => r.id !== '2',
        }}
      />,
    )
    await user.click(screen.getAllByRole('checkbox')[0]!) // header select-all
    expect(onSelectedChange).toHaveBeenCalledWith(new Set(['1']))
  })

  it('disables the checkbox for a non-selectable row', () => {
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        selection={{
          selectedKeys: new Set(),
          onSelectedChange: vi.fn(),
          rowSelectable: (r) => r.id !== '2',
        }}
      />,
    )
    const boxes = screen.getAllByRole('checkbox') // [select-all, row-1, row-2]
    expect(boxes[2]!).toBeDisabled()
  })

  it('selecting a row does not trigger onRowClick', async () => {
    const onRowClick = vi.fn()
    const onSelectedChange = vi.fn()
    const user = userEvent.setup()
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        onRowClick={onRowClick}
        selection={{ selectedKeys: new Set(), onSelectedChange }}
      />,
    )
    await user.click(screen.getAllByRole('checkbox')[1]!) // a row checkbox
    expect(onSelectedChange).toHaveBeenCalledWith(new Set(['1']))
    expect(onRowClick).not.toHaveBeenCalled() // checkbox stops propagation
  })
})

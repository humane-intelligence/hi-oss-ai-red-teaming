import { Search } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

// A list's search box: the submit is an icon attached to the input, so the pair reads as one
// control and leaves the toolbar room for filters.
export function SearchForm({
  value,
  onChange,
  onSubmit,
  placeholder,
  label,
}: {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  placeholder: string
  label: string
}) {
  return (
    <form
      role="search"
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit()
      }}
      className="flex w-full sm:max-w-sm sm:min-w-64 sm:flex-1"
    >
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label={label}
        className="relative min-w-0 flex-1 rounded-r-none focus-visible:z-10"
      />
      <Button
        type="submit"
        variant="outline"
        size="icon"
        aria-label="Search"
        title="Search"
        className="relative -ml-px rounded-l-none shadow-sm focus-visible:z-10"
      >
        <Search />
      </Button>
    </form>
  )
}

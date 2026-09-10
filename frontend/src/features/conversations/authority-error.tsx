import { ArrowLeft } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { humanizeError } from '@/lib/api/problem'

// A failed authority read is an error, not a refusal — and a page that renders one still owes the
// reader a way out, per the convention that every detail view carries a one-step Back.
export function AuthorityError({ error, backTo }: { error: unknown; backTo: string }) {
  const navigate = useNavigate()
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2 self-start"
        onClick={() => navigate(backTo)}
      >
        <ArrowLeft className="size-4" /> Back to evaluation
      </Button>
      <p className="text-destructive" role="alert">
        {humanizeError(error)}
      </p>
    </div>
  )
}

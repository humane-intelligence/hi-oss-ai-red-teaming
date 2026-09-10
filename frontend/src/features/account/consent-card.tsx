import { useState } from 'react'
import { toast } from 'sonner'
import { humanizeError } from '@/lib/api/problem'
import { useCurrentTerms, useTermsDocument } from '@/features/terms/queries'
import { TermsDialogLink } from '@/features/terms/terms-dialog'
import { ConsentCheckbox } from '@/features/terms/consent-checkbox'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useReachable } from '@/lib/use-reachable'
import { useUpdateMe } from './mutations'
import type { MeResponse } from '@/lib/api/types'

// Terms consent is shown, not offered: it is granted against a specific version through the
// acceptance gate, and withdrawing it is deleting the account, not unticking a box. The only
// editable consent here is the email one.
export function ConsentCard({ user }: { user: MeResponse }) {
  const update = useUpdateMe()
  const terms = useCurrentTerms()
  // The document the account accepted, which is not necessarily the current one: naming
  // `terms.data.version` next to `terms_accepted_at` would state something false about the
  // consent record as soon as a newer version lands.
  const accepted = useTermsDocument(user.accepted_terms?.id ?? '')
  // The document to offer for reading: what they agreed to, or — with no acceptance yet — the one
  // they will be asked about.
  const readable = accepted.data ?? (user.consent_terms ? null : terms.data)
  const reachable = useReachable()
  const [error, setError] = useState<string | null>(null)

  const onToggleEmails = async (consent_emails: boolean) => {
    setError(null)
    try {
      await update.mutateAsync({ consent_emails })
    } catch (err) {
      // The mutation opts out of the global toast, so this is the whole channel — and a route
      // change mid-write can take the card with it, which is what the toast fallback is for.
      if (reachable.current) setError(humanizeError(err))
      else toast.error(humanizeError(err))
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Consent</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="text-sm">
          <p className="font-medium">Terms of service</p>
          {user.consent_terms ? (
            <>
              <p className="text-muted-foreground text-xs">
                Accepted
                {user.accepted_terms && ` version ${user.accepted_terms.version}`}
                {user.terms_accepted_at &&
                  ` on ${new Date(user.terms_accepted_at).toLocaleString()}`}
                .
              </p>
              {/* The accepted document is the only one this branch shows, so its failure is the
                  one worth reporting — the current version is not on screen here. */}
              {accepted.isError && (
                <p role="alert" className="text-destructive text-xs">
                  {humanizeError(accepted.error)}
                </p>
              )}
            </>
          ) : (
            <>
              {terms.data ? (
                <p className="text-muted-foreground text-xs">
                  Not accepted yet — you will be asked before you can continue.
                </p>
              ) : (
                // Only claim nothing is published on a *successful* read; a failed one would turn a
                // transient 500 into a false statement about the platform's legal terms.
                terms.isSuccess && (
                  <p className="text-muted-foreground text-xs">
                    This platform has not published terms of service.
                  </p>
                )
              )}
              {terms.isError && (
                <p role="alert" className="text-destructive text-xs">
                  {humanizeError(terms.error)}
                </p>
              )}
            </>
          )}
          {readable && (
            <div className="mt-1 text-xs">
              <TermsDialogLink terms={readable} />
            </div>
          )}
        </div>

        <ConsentCheckbox
          id="consent_emails"
          label="Send me product email"
          hint="Optional. Account email — verification, password resets — is sent either way."
          checked={user.consent_emails}
          disabled={update.isPending}
          onChange={(event) => void onToggleEmails(event.target.checked)}
        />

        {error && (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        )}
      </CardContent>
    </Card>
  )
}

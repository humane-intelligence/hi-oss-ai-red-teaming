import { useEffect, useId, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { useEvaluationGroup } from './queries'
import { useCreateGroup, useSaveDraft, useSubmitGroup, useUpdateGroup } from './mutations'
import { useOrganizations } from '@/features/organizations/queries'
import { LicensePicker } from '@/features/licenses/license-picker'
import { findNoLicense } from '@/features/licenses/no-license'
import { useLicenses } from '@/features/licenses/queries'
import { useAiModels } from '@/features/ai-models/queries'
import { modelLabelSummary } from '@/features/ai-models/labels'
import type { EvaluationGroupDraftCreate } from '@/lib/api/types'
import { useAuth } from '@/lib/auth/auth-context'
import { usePermissions, useObjectPermissions } from '@/lib/auth/use-permissions'
import { NotAuthorized } from '@/lib/auth/not-authorized'
import { applyApiError } from '@/lib/api/form'
import { humanizeError } from '@/lib/api/problem'
import { useUnsavedGuard } from '@/lib/use-unsaved-guard'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { GroupIdentity } from './group-trail'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { PageHeader } from '@/components/shared/page-header'
import { FormField } from '@/components/shared/form-field'
import { MultiSearchSelect } from '@/components/shared/multi-search-select'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { PlainSelect } from '@/components/ui/plain-select'
import { Card, CardContent } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { METRICS_ACCESS_LABELS, METRICS_ACCESS_VALUES } from './metrics-access'

// Full validation — the gate for "Create" (a complete, ready group).
const schema = z
  .object({
    title: z.string().min(1, 'Required').max(255),
    description: z.string().min(1, 'Required'),
    access_level: z.enum(['public', 'organization', 'invitation_only']),
    metrics_access_during: z.enum(METRICS_ACCESS_VALUES),
    metrics_access_after: z.enum(METRICS_ACCESS_VALUES),
    // Empty string = no organization. Belonging is orthogonal to visibility, so a
    // public/invitation-only group may carry one too; only `organization` access
    // requires it (refine below, mirroring the backend's 400).
    organization_id: z.string(),
    start_date: z.string().min(1, 'Required'),
    end_date: z.string(),
    // '' = inherit the platform default; any other value is a licence id override.
    data_license_id: z.string(),
    // The group's allowed-model subset. The ≥1 requirement is enforced in `onCreate`
    // (only for a caller with `models:read`, who can actually see/pick models), not
    // here — an editor without that permission must still be able to save other
    // fields against a masked subset.
    allowed_model_ids: z.array(z.string()),
  })
  .superRefine((v, ctx) => {
    // `organization` access needs a target org (mirrors the backend invariant).
    if (v.access_level === 'organization' && !v.organization_id) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['organization_id'],
        message: 'Required for organization access',
      })
    }
  })
type FormValues = z.infer<typeof schema>

// "Save as draft" only needs a title — the rest is filled in later.
const draftSchema = z.object({
  title: z.string().trim().min(1, 'A title is required to save a draft'),
})

// Default to the safe (non-public) visibility so a quick draft isn't exposed by
// accident; the user opts into `public` explicitly (and confirms — see the modal).
const EMPTY: FormValues = {
  title: '',
  description: '',
  access_level: 'invitation_only',
  // Match the backend default: new groups let each member see their own metrics.
  metrics_access_during: 'members_personal_metrics',
  metrics_access_after: 'members_personal_metrics',
  organization_id: '',
  start_date: '',
  end_date: '',
  data_license_id: '',
  allowed_model_ids: [],
}

// Guided form section: a labeled group of related fields within the single page.
function FormSection({
  title,
  hint,
  children,
}: {
  title: string
  hint?: string
  children: ReactNode
}) {
  return (
    <section className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold tracking-tight">{title}</h3>
        {hint && <p className="text-muted-foreground text-xs">{hint}</p>}
      </div>
      {children}
    </section>
  )
}

// Draft body for the partial save — `POST /draft` on a create, PATCH on a draft edit. The
// optional fields travel as explicit `null` when empty rather than omitted: on the PATCH that is
// how clearing a previously-set field persists (omitted = unchanged), and on the POST it is
// equivalent to omitting — but `''` would be rejected either way (`description` is `min_length=1`,
// `start_date` parses as a date). `data_license_id` is the exception and goes through
// `createLicenseField`: on a create, omitted derives from the access level and `null` inherits the
// platform default; on an edit the field always travels.
// organization_id mirrors `onCreate`: kept only under `organization` access, so an org picked
// before the level was switched away isn't sent, and switching a draft off organization access
// actually clears it.
// `allowedModelIds` is threaded through the caller's `models:read` so a masked subset is never
// sent; an empty set is dropped, which the PATCH rejects and the POST defaults to anyway.
// `''` in the picker means "inherit the platform default" only when the console could actually offer
// the alternative. With the catalog unavailable — the request failed, or the operator submitted before
// it resolved — it cannot know the no-licence row, so it omits the field and lets the backend derive
// one from the access level. Sending `null` there would license a private engagement under the
// platform default, silently, which is the outcome this feature exists to prevent. On an edit the
// field always travels: there `null` is how a licence is cleared back to inherit.
function createLicenseField(
  value: string,
  sentinelKnown: boolean,
): { data_license_id?: string | null } {
  if (value) return { data_license_id: value }
  return sentinelKnown ? { data_license_id: null } : {}
}

// What the form says under the licence select, if anything. Split out because the answer depends on
// five inputs and the wrong sentence is worse than none: the access level and the licence can
// legitimately disagree (a later switch never rewrites the licence), and without the sentinel there
// is nothing to compare against — but the two ways of lacking it are not the same sentence. A failed
// request means the field may not even show the stored licence; a page that simply does not contain
// the entry (the list is one page of 100, ordered by name) leaves the picker honest about what it
// offers, and telling the operator to reload it would send them after nothing.
function licenseNoteFor({
  noLicenseId,
  catalog,
  isEdit,
  accessLevel,
  dataLicenseId,
}: {
  noLicenseId?: string
  catalog: 'pending' | 'failed' | 'loaded'
  isEdit: boolean
  accessLevel: string
  dataLicenseId: string
}): string | undefined {
  if (!noLicenseId) {
    if (catalog === 'pending') return undefined
    // The server derives from the access level, so name what *this* level gets: telling a public
    // group about the private default is the note being wrong in the state it exists for.
    const derived =
      accessLevel === 'invitation_only'
        ? 'private engagements get no license'
        : 'this one gets the platform default license'
    if (catalog === 'failed')
      return isEdit
        ? 'The license list could not be loaded, so this field may not show the license the group carries. Reload before changing it.'
        : `The license list could not be loaded. The server will apply the default for this access level — ${derived}.`
    return isEdit
      ? undefined
      : `This list does not include the “No license” entry, so the server will apply the default for this access level — ${derived}.`
  }
  if (accessLevel === 'invitation_only' && dataLicenseId !== noLicenseId)
    return 'Private engagements usually carry no data license — a client may not want their data shareable.'
  if (accessLevel !== 'invitation_only' && dataLicenseId === noLicenseId)
    return 'This engagement will carry no data license, so its data is not shareable.'
  return undefined
}

function draftBody(
  values: FormValues,
  allowedModelIds: string[] | undefined,
  sentinelKnown: boolean,
): EvaluationGroupDraftCreate {
  return {
    title: values.title,
    access_level: values.access_level,
    // Always sent: omitting them would default the POST and leave the stored value on the PATCH,
    // either way dropping the choice the user just made.
    metrics_access_during: values.metrics_access_during,
    metrics_access_after: values.metrics_access_after,
    description: values.description || null,
    start_date: values.start_date || null,
    end_date: values.end_date || null,
    organization_id: values.access_level === 'organization' ? values.organization_id || null : null,
    ...createLicenseField(values.data_license_id, sentinelKnown),
    ...(allowedModelIds && allowedModelIds.length > 0
      ? { allowed_model_ids: allowedModelIds }
      : {}),
  }
}

export function EvaluationGroupFormPage() {
  const { id } = useParams<{ id: string }>()
  const isEdit = Boolean(id)
  const navigate = useNavigate()
  const existing = useEvaluationGroup(id ?? '')
  const organizations = useOrganizations({ limit: 100, offset: 0, order_by: 'name' })
  const create = useCreateGroup()
  const update = useUpdateGroup(id ?? '')
  const submit = useSubmitGroup(id ?? '', { renderInline: true })
  const saveDraft = useSaveDraft()
  const { user } = useAuth()
  const { has } = usePermissions()
  // Edit authority is object-scoped: an in-group role grants `evaluation_groups:update`,
  // not the global permission. Mirror the server's per-object gate so the route guard
  // (coarse `:read`) can't let a non-editor through — create mode is gated globally on
  // `:create` at the route, so it needs no object check.
  const groupPerms = useObjectPermissions(existing.data?.user_permissions)
  const canEdit = !isEdit || groupPerms.has('evaluation_groups:update')
  // The backend lets a non-admin scope a group only to their *own* org (the
  // `evaluation_groups:manage` break-glass lifts it). Mirror that here so the
  // picker never offers an org the submit would 403 on; keep the group's current
  // org selectable in edit mode so an admin-parked value isn't silently dropped.
  const canManageOrgs = has('evaluation_groups:manage')
  // The allowed-model picker reads the registry (needs `models:read` — owners now
  // hold it). Without it the caller can't list models, so the picker is hidden and
  // the ≥1 rule is not enforced client-side (the group detail masks the subset for
  // them too); a full create then falls back to the backend's 422.
  // Reading the registry to curate the subset needs `models:read` — held globally,
  // or object-scoped on this group (an in-group owner, via `user_permissions`). When
  // relying on the group grant, the list call carries `for_group` to authorize.
  const canReadModelsGlobal = has('models:read')
  const canReadModelsViaGroup = isEdit && groupPerms.has('models:read')
  const canReadModels = canReadModelsGlobal || canReadModelsViaGroup
  const models = useAiModels(
    { limit: 100, offset: 0, ...(canReadModelsGlobal ? {} : id ? { for_group: id } : {}) },
    { enabled: canReadModels },
  )
  const [modelSearch, setModelSearch] = useState('')
  // `GET /ai-models` has no name filter, so narrow the (small) registry client-side.
  const modelOptions = useMemo(() => {
    const term = modelSearch.trim().toLowerCase()
    return (
      (models.data?.items ?? [])
        .filter((m) => !term || m.name.toLowerCase().includes(term))
        // Labels ride the `hint` slot so an operator sees "self-hosted" / "fine-tuning needed" while
        // picking, not after assigning; an empty set renders nothing. The hint joins the option's
        // accessible name, so only a summary goes there and the full set rides the tooltip.
        .map((m) => ({
          value: m.id,
          label: m.name,
          hint: modelLabelSummary(m.labels),
          hintTitle: m.labels.join(', '),
        }))
    )
  }, [models.data, modelSearch])
  // Same cached query the picker fetches, so this adds no request.
  const licenses = useLicenses()
  const noLicenseId = findNoLicense(licenses.data?.items ?? [])?.id
  const myOrg = user?.organization ?? null
  const currentOrgId = existing.data?.organization_id ?? null
  // Seed the caller's own org from `user.organization` (already on `MeResponse`)
  // so it's always selectable — otherwise a non-admin whose org sorts past the
  // fetched page (limit 100) gets an empty picker and can't submit at all.
  const selectableOrgs = useMemo(() => {
    const allOrgs = organizations.data?.items ?? []
    if (canManageOrgs) return allOrgs
    const byId = new Map<string, { id: string; name: string }>()
    if (myOrg) byId.set(myOrg.id, { id: myOrg.id, name: myOrg.name })
    for (const o of allOrgs) {
      if (o.id === myOrg?.id || o.id === currentOrgId) byId.set(o.id, o)
    }
    return [...byId.values()]
  }, [canManageOrgs, organizations.data, myOrg, currentOrgId])

  const {
    register,
    handleSubmit,
    reset,
    getValues,
    control,
    setValue,
    setError,
    clearErrors,
    formState,
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: EMPTY,
  })

  useEffect(() => {
    const g = existing.data
    if (!g) return
    // The group's fields are nullable on a partial draft — prefill safely.
    reset({
      title: g.title ?? '',
      description: g.description ?? '',
      access_level: g.access_level,
      metrics_access_during: g.metrics_access_during,
      metrics_access_after: g.metrics_access_after,
      organization_id: g.organization_id ?? '',
      start_date: g.start_date ?? '',
      end_date: g.end_date ?? '',
      data_license_id: g.data_license_id ?? '',
      // `null` when the subset is masked (no `models:read`) — prefill empty and
      // leave it untouched on save (see the update/draft bodies below).
      allowed_model_ids: (g.allowed_models ?? []).map((m) => m.model_id),
    })
  }, [existing.data, reset])

  // A new group starts private, which carries no licence — select it as soon as the catalog
  // resolves. Deliberately not `shouldDirty`: an untouched form must not trip the unsaved guard.
  useEffect(() => {
    if (isEdit || !noLicenseId) return
    if (getValues('access_level') !== 'invitation_only' || getValues('data_license_id') !== '')
      return
    setValue('data_license_id', noLicenseId)
  }, [isEdit, noLicenseId, getValues, setValue])

  useUnsavedGuard(formState.isDirty)

  const busy = create.isPending || update.isPending || submit.isPending || saveDraft.isPending
  const title = useWatch({ control, name: 'title' })
  const accessLevel = useWatch({ control, name: 'access_level' })
  const dataLicenseId = useWatch({ control, name: 'data_license_id' })
  const selectedModels = useWatch({ control, name: 'allowed_model_ids' })

  // The access level and the licence can legitimately disagree (a later switch never rewrites the
  // licence), so the form says so instead of silently reconciling. One string, one id: the note has
  // to reach the select through `aria-describedby`, not just sit under it. Without the catalog there
  // is no sentinel to compare against, so the note says that instead of going quiet — otherwise the
  // field would read "Platform default" while the server derives something else from the access level.
  const licenseNoteId = `${useId()}-license-note`
  const licenseNote = licenseNoteFor({
    noLicenseId,
    catalog: licenses.isPending ? 'pending' : licenses.isError ? 'failed' : 'loaded',
    isEdit,
    accessLevel,
    dataLicenseId,
  })
  const isDraft = existing.data?.status === 'draft'
  const originalAccess = existing.data?.access_level
  const [pendingSave, setPendingSave] = useState<(() => Promise<void>) | null>(null)
  // The submit refusal lists every gap at once and carries no field errors, so it is rendered on
  // the form rather than toasted: the fields are already saved by then, which makes the page
  // otherwise indistinguishable from a clean save.
  const [submitRefusal, setSubmitRefusal] = useState<string | null>(null)

  // Wait for the group before judging edit authority, then 403-equivalent if the
  // caller holds no in-group `evaluation_groups:update` (the visible Edit button
  // already mirrors this, but a direct URL must be gated too). A load failure is
  // surfaced as an error, not "no access" — a deleted/unreachable group isn't a
  // permission verdict.
  if (isEdit && existing.isPending) return <DetailSkeleton />
  if (isEdit && existing.isError)
    return (
      <div className="mx-auto max-w-2xl">
        <p className="text-destructive">{humanizeError(existing.error)}</p>
      </div>
    )
  if (!canEdit) return <NotAuthorized />

  // Confirm before exposing a group publicly: a fresh public create/draft, or an
  // edit that switches an invite/org group to public — but not when it was
  // already public (no change to confirm).
  const needsPublicConfirm = (accessLevel: FormValues['access_level']) =>
    accessLevel === 'public' && (!isEdit || originalAccess !== 'public')

  const guardPublic = (accessLevel: FormValues['access_level'], run: () => Promise<void>) => {
    if (needsPublicConfirm(accessLevel)) setPendingSave(() => run)
    else void run()
  }

  // "Create"/"Save": full validation, lands on the detail page. On a draft edit it also sends
  // the group to review, so a completed draft ends where a completed create does
  // (`pending_approval`) instead of silently staying a draft.
  const onCreate = handleSubmit((values) => {
    setSubmitRefusal(null)
    // A complete group needs ≥1 allowed model. Only enforce it for a caller who can
    // see the picker; without `models:read` the field is masked and left to the backend.
    if (canReadModels && values.allowed_model_ids.length === 0) {
      setError('allowed_model_ids', { message: 'Select at least one model' })
      return
    }
    // On a draft this press also submits, and the gate refuses a start date before today (UTC,
    // as the backend computes it). Checked here so a doomed transition never writes first — every
    // draft older than a day would otherwise PATCH, fail, and audit on each attempt.
    if (isDraft && values.start_date && values.start_date < new Date().toISOString().slice(0, 10)) {
      setError('start_date', { message: 'Must be today or later to submit for approval' })
      return
    }
    guardPublic(values.access_level, async () => {
      const base = {
        title: values.title,
        description: values.description,
        access_level: values.access_level,
        metrics_access_during: values.metrics_access_during,
        metrics_access_after: values.metrics_access_after,
        organization_id:
          values.access_level === 'organization' ? values.organization_id || null : null,
        start_date: values.start_date,
        end_date: values.end_date || null,
      }
      try {
        if (isEdit) {
          // PATCH: send the subset only when the caller can manage it, else leave the
          // (possibly masked) subset unchanged.
          const edited = { ...base, data_license_id: values.data_license_id || null }
          await update.mutateAsync(
            canReadModels ? { ...edited, allowed_model_ids: values.allowed_model_ids } : edited,
          )
          // Reset before the submit can fail: the fields are already saved, so leaving the form
          // dirty would have the unsaved-guard warn about work that landed.
          reset(values)
          // The submit gate is stricter than this form — beyond the start date checked above it
          // wants a scenario on every evaluation, which the form cannot see. A refusal leaves the
          // edit saved and says so on the form instead of navigating away.
          if (isDraft) {
            try {
              await submit.mutateAsync()
            } catch (err) {
              setSubmitRefusal(humanizeError(err))
              return
            }
          }
          navigate(`/evaluation-groups/${id}`)
        } else {
          // Create requires the field; a caller without `models:read` sends [] and
          // gets the backend's 422 surfaced on the field.
          const created = await create.mutateAsync({
            ...base,
            ...createLicenseField(values.data_license_id, Boolean(noLicenseId)),
            allowed_model_ids: values.allowed_model_ids,
          })
          navigate(`/evaluation-groups/${created.id}`)
        }
      } catch (err) {
        applyApiError(err, setError)
      }
    })
  })

  // "Save as draft": title-only validation. A new draft lands on its edit page so the owner
  // can keep filling it in; on a draft edit it persists progress the full schema would reject.
  const onSaveDraft = () => {
    const values = getValues()
    const parsed = draftSchema.safeParse(values)
    if (!parsed.success) {
      setError('title', { message: parsed.error.issues[0]?.message ?? 'A title is required' })
      return
    }
    const allowedModelIds = canReadModels ? values.allowed_model_ids : undefined
    guardPublic(values.access_level, async () => {
      try {
        if (isEdit) {
          // Always send the licence field on an edit — `null` is how a stored licence is cleared
          // back to inherit. Unlike a create, this does not depend on knowing the no-licence row:
          // the picker prefills from the group, so an unavailable catalog cannot turn a stored
          // licence into a null.
          await update.mutateAsync(draftBody(values, allowedModelIds, true))
          reset(values) // clear dirty so the unsaved-guard doesn't fire
          navigate(`/evaluation-groups/${id}`)
        } else {
          const created = await saveDraft.mutateAsync(
            draftBody(values, allowedModelIds, Boolean(noLicenseId)),
          )
          reset(values)
          navigate(`/evaluation-groups/${created.id}/edit`)
        }
      } catch (err) {
        applyApiError(err, setError)
      }
    })
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2"
        onClick={() => navigate(isEdit ? `/evaluation-groups/${id}` : '/evaluation-groups')}
      >
        <ArrowLeft className="size-4" /> Back
      </Button>
      <PageHeader
        title={isEdit ? (isDraft ? 'Edit draft' : 'Edit group') : 'New group'}
        breadcrumbs={
          isEdit && id ? (
            <GroupIdentity groupId={id} />
          ) : (
            <Breadcrumbs
              items={[{ label: 'Evaluation Groups', to: '/evaluation-groups' }, { label: 'New' }]}
            />
          )
        }
      />
      <Card>
        <CardContent className="pt-6">
          <form onSubmit={onCreate} className="space-y-8" noValidate>
            <FormSection title="Basics" hint="Name the engagement and describe what it covers.">
              <FormField label="Title" htmlFor="title" error={formState.errors.title?.message}>
                <Input id="title" {...register('title')} />
              </FormField>
              <FormField
                label="Description"
                htmlFor="description"
                error={formState.errors.description?.message}
              >
                <Textarea id="description" rows={4} {...register('description')} />
              </FormField>
            </FormSection>

            <FormSection title="Visibility" hint="Who can see this engagement.">
              <FormField
                label="Access level"
                htmlFor="access_level"
                error={formState.errors.access_level?.message}
              >
                <PlainSelect
                  id="access_level"
                  {...register('access_level', {
                    // Picking `public` opens the group platform-wide; default its analytics
                    // to `inherit_group_access` so the metrics audience follows suit. Only a
                    // smart default — the user can still narrow the two selects afterward.
                    onChange: (e) => {
                      const next = e.target.value
                      if (next === 'public') {
                        setValue('metrics_access_during', 'inherit_group_access', {
                          shouldDirty: true,
                        })
                        setValue('metrics_access_after', 'inherit_group_access', {
                          shouldDirty: true,
                        })
                      }
                      // Create-time smart default only: private carries no licence, any other level
                      // inherits the platform default. On an edit the stored licence stays put — a
                      // group whose delivered exports already name a licence must not have it
                      // rewritten by a visibility change. The backend re-derives on an access-level
                      // change only for a group that stores nothing *and* a caller that omits
                      // `data_license_id`; this form always sends the field, so that path serves API
                      // clients and an inheriting group edited here keeps the platform default. The
                      // mismatch note below is what speaks then, instead of a silent rewrite.
                      const license = getValues('data_license_id')
                      if (isEdit || !noLicenseId) return
                      if (next === 'invitation_only' && license === '') {
                        setValue('data_license_id', noLicenseId, { shouldDirty: true })
                      } else if (next !== 'invitation_only' && license === noLicenseId) {
                        setValue('data_license_id', '', { shouldDirty: true })
                      }
                    },
                  })}
                >
                  <option value="public">public</option>
                  <option value="organization">organization</option>
                  <option value="invitation_only">invitation only</option>
                </PlainSelect>
              </FormField>
              {accessLevel === 'organization' && (
                <FormField
                  label="Organization"
                  htmlFor="organization_id"
                  error={formState.errors.organization_id?.message}
                >
                  <PlainSelect id="organization_id" {...register('organization_id')}>
                    <option value="">Select an organization…</option>
                    {selectableOrgs.map((o) => (
                      <option key={o.id} value={o.id}>
                        {o.name}
                      </option>
                    ))}
                  </PlainSelect>
                  {selectableOrgs.length === 0 && !organizations.isPending ? (
                    <p className="text-muted-foreground mt-1 text-xs">
                      You aren&apos;t assigned to an organization.
                    </p>
                  ) : (
                    !canManageOrgs && (
                      <p className="text-muted-foreground mt-1 text-xs">
                        You can assign only your own organization.
                      </p>
                    )
                  )}
                </FormField>
              )}
            </FormSection>

            <FormSection
              title="Analytics visibility"
              hint="Who can view this group's metrics dashboards. The group owner and admins always can."
            >
              <div className="grid grid-cols-2 gap-4">
                <FormField
                  label="While active"
                  htmlFor="metrics_access_during"
                  error={formState.errors.metrics_access_during?.message}
                >
                  <PlainSelect id="metrics_access_during" {...register('metrics_access_during')}>
                    {METRICS_ACCESS_VALUES.map((value) => (
                      <option key={value} value={value}>
                        {METRICS_ACCESS_LABELS[value]}
                      </option>
                    ))}
                  </PlainSelect>
                </FormField>
                <FormField
                  label="After finish"
                  htmlFor="metrics_access_after"
                  error={formState.errors.metrics_access_after?.message}
                >
                  <PlainSelect id="metrics_access_after" {...register('metrics_access_after')}>
                    {METRICS_ACCESS_VALUES.map((value) => (
                      <option key={value} value={value}>
                        {METRICS_ACCESS_LABELS[value]}
                      </option>
                    ))}
                  </PlainSelect>
                </FormField>
              </div>
              <p className="text-muted-foreground text-xs">
                &ldquo;Everyone who can see the group&rdquo; follows the access level above —
                platform-wide for public, organization members for organization, invited members for
                invitation only. &ldquo;Members (own metrics only)&rdquo; shows each member (red
                teamers) the dashboards filtered to their own submissions; the owner and admins
                always see the full aggregate.
              </p>
            </FormSection>

            {canReadModels && (
              <FormSection
                title="Models"
                hint="Which models this group's evaluations may use. Pick at least one."
              >
                <MultiSearchSelect
                  label="Allowed models"
                  htmlFor="allowed_model_ids"
                  value={selectedModels}
                  onChange={(next) => {
                    setValue('allowed_model_ids', next, {
                      shouldDirty: true,
                      shouldValidate: false,
                    })
                    if (next.length > 0) clearErrors('allowed_model_ids')
                  }}
                  onSearchChange={setModelSearch}
                  options={modelOptions}
                  isPending={models.isPending}
                  isError={models.isError}
                  error={formState.errors.allowed_model_ids?.message}
                  placeholder="Search models…"
                  emptyLabel="No models available."
                />
              </FormSection>
            )}

            <FormSection title="Schedule" hint="When the engagement opens and (optionally) closes.">
              <div className="grid grid-cols-2 gap-4">
                <FormField
                  label="Start date"
                  htmlFor="start_date"
                  error={formState.errors.start_date?.message}
                >
                  <Input id="start_date" type="date" {...register('start_date')} />
                </FormField>
                <FormField
                  label="End date (optional)"
                  htmlFor="end_date"
                  error={formState.errors.end_date?.message}
                >
                  <Input id="end_date" type="date" {...register('end_date')} />
                </FormField>
              </div>
            </FormSection>

            <FormSection
              title="Licensing"
              hint="The data license applied to this engagement's data; its evaluations inherit it unless they override."
            >
              {/* Without the sentinel a create omits the field and the server derives from the access
                  level, so the empty option cannot claim the platform default — that is exactly what
                  a private group will not get. */}
              <LicensePicker
                field={register('data_license_id')}
                value={dataLicenseId}
                error={formState.errors.data_license_id?.message}
                inheritLabel={
                  isEdit || noLicenseId ? 'Platform default' : 'Default for this access level'
                }
                offerNoLicense
                describedBy={licenseNote ? licenseNoteId : undefined}
              />
              {licenseNote && (
                <p id={licenseNoteId} className="text-muted-foreground mt-1 text-xs">
                  {licenseNote}
                </p>
              )}
            </FormSection>

            {submitRefusal && (
              <p role="alert" className="text-destructive text-sm">
                Your changes were saved, but the group was not submitted for approval:{' '}
                {submitRefusal}
              </p>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" variant="outline" onClick={() => navigate(-1)}>
                Cancel
              </Button>
              {/* On a create it produces the draft; on a draft edit it persists progress the full
                  schema would reject. Hidden on any other status — a PATCH cannot change the
                  status, so there it saved without producing a draft — under title-only
                  validation, which let a cleared field null out what the full form requires. */}
              {(!isEdit || isDraft) && (
                <Button
                  type="button"
                  variant="secondary"
                  disabled={!title.trim() || busy}
                  onClick={onSaveDraft}
                >
                  Save as draft
                </Button>
              )}
              <Button type="submit" disabled={busy}>
                {busy ? 'Saving…' : isEdit ? (isDraft ? 'Save & submit' : 'Save') : 'Create'}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <ConfirmDialog
        open={pendingSave !== null}
        onOpenChange={(open) => {
          if (!open) setPendingSave(null)
        }}
        title="Make this group public?"
        description="A public group is visible to everyone on the platform — except while it's still a draft, which stays private to you until you complete it. You can change the access level later."
        confirmLabel="Save as public"
        pending={busy}
        onConfirm={() => {
          const run = pendingSave
          setPendingSave(null)
          void run?.()
        }}
      />
    </div>
  )
}

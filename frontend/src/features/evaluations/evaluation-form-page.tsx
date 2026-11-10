import { useEffect, useId, useState } from 'react'
import { useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { useEvaluation } from './queries'
import { CoverImageField } from './cover-image-field'
import { useCreateEvaluation, useUpdateEvaluation } from './mutations'
import { useEvaluationGroup, useEvaluationGroups } from '@/features/evaluation-groups/queries'
import {
  evaluationsBlockedHint,
  groupAcceptsEvaluations,
} from '@/features/evaluation-groups/lifecycle'
import { LicensePicker } from '@/features/licenses/license-picker'
import { findNoLicense } from '@/features/licenses/no-license'
import { useLicenses } from '@/features/licenses/queries'
import { useObjectPermissions } from '@/lib/auth/use-permissions'
import { NotAuthorized } from '@/lib/auth/not-authorized'
import { applyApiError } from '@/lib/api/form'
import { humanizeError } from '@/lib/api/problem'
import { useUnsavedGuard } from '@/lib/use-unsaved-guard'
import { Breadcrumbs } from '@/components/shared/breadcrumbs'
import { GroupIdentity } from '@/features/evaluation-groups/group-trail'
import { DetailSkeleton } from '@/components/shared/detail-skeleton'
import { PageHeader } from '@/components/shared/page-header'
import { FormField } from '@/components/shared/form-field'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { PlainSelect } from '@/components/ui/plain-select'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { InfoHint } from '@/components/shared/info-hint'
import { TAGS_HINT } from './tag-hint'
import { Field, FieldContent, FieldDescription, FieldLabel } from '@/components/ui/field'

const schema = z.object({
  title: z.string().min(1, 'Required').max(255),
  description: z.string(),
  evaluation_group_id: z.string(),
  cover_image: z.string(),
  mask_models_enabled: z.boolean(),
  tags_enabled: z.boolean(),
  // '' = inherit; any other value is a licence id override.
  data_license_id: z.string(),
})
type FormValues = z.infer<typeof schema>

export function EvaluationFormPage() {
  const { id } = useParams<{ id: string }>()
  const isEdit = Boolean(id)
  const [searchParams] = useSearchParams()
  const groupId = searchParams.get('group') ?? ''
  const fromGroup = !isEdit && groupId !== ''
  const navigate = useNavigate()
  const existing = useEvaluation(id ?? '')
  const create = useCreateEvaluation()
  const update = useUpdateEvaluation(id ?? '')
  // Picker options, server-filtered to groups that accept evaluations (the
  // lifecycle allowlist stays backend-owned). Skipped in from-group mode, where
  // the select is locked to the preset group and the list would go unused.
  const groups = useEvaluationGroups(
    { limit: 100, offset: 0, accepts_evaluations: true },
    { enabled: !fromGroup },
  )
  // The filtered list can't tell "no groups at all" from "none accepting yet", so
  // when it comes back empty, probe the unfiltered total (limit-1, count only) to
  // pick the right empty state.
  const anyGroups = useEvaluationGroups(
    { limit: 1, offset: 0 },
    { enabled: !isEdit && !fromGroup && groups.isSuccess && groups.data.items.length === 0 },
  )
  // Creating an evaluation is authorized per parent group (an in-group role, not the
  // global permission). When a group is preselected (?group=), resolve its
  // user_permissions and gate the form on object `evaluations:create`.
  const presetGroup = useEvaluationGroup(!isEdit ? groupId : '')
  // Editing an evaluation is also a page inside a group — it just learns the group from the
  // evaluation rather than from the route.
  const trailGroupId = isEdit ? (existing.data?.evaluation_group_id ?? '') : groupId
  const groupPerms = useObjectPermissions(presetGroup.data?.user_permissions)
  const canCreateHere = !fromGroup || groupPerms.has('evaluations:create')

  const { register, handleSubmit, reset, setError, setValue, control, formState } =
    useForm<FormValues>({
      resolver: zodResolver(schema),
      defaultValues: {
        title: '',
        description: '',
        evaluation_group_id: groupId,
        cover_image: '',
        mask_models_enabled: true,
        tags_enabled: true,
        data_license_id: '',
      },
    })

  useEffect(() => {
    const e = existing.data
    if (!e) return
    reset({
      title: e.title,
      description: e.description ?? '',
      evaluation_group_id: e.evaluation_group_id,
      cover_image: e.cover_image ?? '',
      mask_models_enabled: e.mask_models_enabled,
      tags_enabled: e.tags_enabled,
      data_license_id: e.data_license_id ?? '',
    })
  }, [existing.data, reset])

  const [coverUploading, setCoverUploading] = useState(false)
  const coverImage = useWatch({ control, name: 'cover_image' })
  const coverLabelId = `${useId()}-cover-label`

  // The form turns dirty only once the upload returns, so a pick in flight needs its own guard.
  useUnsavedGuard(formState.isDirty || coverUploading)

  // Resolve the parent group directly (its query key dedupes with the preset-group fetch)
  // so the "Inherit (…)" option reflects the group's real effective license. The scoped
  // groups list (limit 100) can omit it — notably in edit mode, where the group <select>
  // isn't rendered — which would otherwise silently degrade the label to the platform default.
  const evaluationGroupId = useWatch({ control, name: 'evaluation_group_id' })
  const maskModels = useWatch({ control, name: 'mask_models_enabled' })
  const tagsEnabled = useWatch({ control, name: 'tags_enabled' })
  // Subscribed here because its first use sits below this component's early returns.
  const evaluationLicenseId = useWatch({ control, name: 'data_license_id' })
  const licenseParentGroup = useEvaluationGroup(evaluationGroupId)
  // Hooks stay above this component's early returns. Same cached query the picker mounts, so reading
  // the catalog here adds no request — it is what names the sentinel among the options.
  const licenseCatalog = useLicenses()
  const licenseNoteId = `${useId()}-license-note`
  const tagsDescriptionId = `${useId()}-tags-description`

  // Gate a preselected-group create before rendering the form; the cross-group picker
  // path (no group yet) stays backend-enforced on submit. A failed group load is an
  // error, not "no access" — don't conflate an unreachable group with a permission verdict.
  if (fromGroup && presetGroup.isPending) return <DetailSkeleton />
  if (fromGroup && presetGroup.isError)
    return (
      <div className="mx-auto max-w-2xl">
        <p className="text-destructive">{humanizeError(presetGroup.error)}</p>
      </div>
    )
  if (!canCreateHere) return <NotAuthorized />
  // Lifecycle, not authority: a deep link to a non-accepting group would only 409 on
  // submit, so explain the state up front instead of rendering a doomed form.
  if (fromGroup && !groupAcceptsEvaluations(presetGroup.data?.status))
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <PageHeader title="New evaluation" />
        <p className="text-muted-foreground">{evaluationsBlockedHint(presetGroup.data?.status)}</p>
        <Button variant="outline" onClick={() => navigate(`/evaluation-groups/${groupId}`)}>
          Back to group
        </Button>
      </div>
    )

  const onSubmit = handleSubmit(async (values) => {
    if (!isEdit && !values.evaluation_group_id) {
      setError('evaluation_group_id', { message: 'Pick a group' })
      return
    }
    try {
      if (isEdit) {
        await update.mutateAsync({
          title: values.title,
          description: values.description || null,
          cover_image: values.cover_image || null,
          mask_models_enabled: values.mask_models_enabled,
          tags_enabled: values.tags_enabled,
          data_license_id: values.data_license_id || null,
        })
        navigate(`/evaluations/${id}`)
      } else {
        const created = await create.mutateAsync({
          title: values.title,
          description: values.description || null,
          evaluation_group_id: values.evaluation_group_id,
          cover_image: values.cover_image || null,
          mask_models_enabled: values.mask_models_enabled,
          tags_enabled: values.tags_enabled,
          tags_restricted: false, // opt-in later via the evaluation's Allowed tags card
          data_license_id: values.data_license_id || null,
        })
        navigate(`/evaluations/${created.id}`)
      }
    } catch (err) {
      applyApiError(err, setError)
    }
  })

  const groupItems = groups.data?.items ?? []

  // Clearing the override inherits the parent group's effective license (the group's own override,
  // else the platform default), not a bare platform default. Hand that resolved licence to the
  // picker so its "inherit" option labels + describes it; the picker fills the platform default
  // itself when no group is resolved yet.
  const inheritedGroupLicense = licenseParentGroup.data?.effective_license
  // The evaluation's licence and the engagement's can disagree, and both directions have legal
  // weight: overriding a closed engagement makes its data shareable, and carrying no licence under a
  // licensed one narrows what the client was promised. The group form says so for its own pair; this
  // is the same sentence one level down, since the write is accepted either way.
  //
  // Both arms need two facts — the sentinel's id and the engagement's own licence — and each stays
  // silent until it has them. Inferring "not the sentinel" from a catalog that failed to load would
  // make the first arm announce a shareable override on an evaluation that carries no licence
  // either; inferring "the engagement is licensed" from a group still loading would make the second
  // arm claim a divergence that does not exist.
  const noLicenseId = findNoLicense(licenseCatalog.data?.items ?? [])?.id
  const bothLicencesKnown = noLicenseId !== undefined && inheritedGroupLicense !== undefined
  const groupCarriesNoLicense = inheritedGroupLicense?.is_no_license === true
  const overrideIsNoLicense = evaluationLicenseId === noLicenseId
  const licenseNote = !bothLicencesKnown
    ? undefined
    : evaluationLicenseId !== '' && groupCarriesNoLicense && !overrideIsNoLicense
      ? "This engagement carries no data license — an override here makes this evaluation's data shareable."
      : overrideIsNoLicense && !groupCarriesNoLicense
        ? 'This evaluation will carry no data license, so its data is not shareable — unlike the rest of the engagement.'
        : undefined

  if (!isEdit && !fromGroup && groups.isSuccess && groupItems.length === 0) {
    if (anyGroups.isPending) return <DetailSkeleton />
    // A failed probe can't tell the two empty states apart — surface the error
    // (like the deep-link path above) rather than defaulting to "no groups yet",
    // which would mislead a viewer whose groups exist but are all non-accepting.
    if (anyGroups.isError)
      return (
        <div className="mx-auto max-w-2xl">
          <p className="text-destructive">{humanizeError(anyGroups.error)}</p>
        </div>
      )
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <PageHeader title="New evaluation" />
        {(anyGroups.data?.total ?? 0) > 0 ? (
          <>
            <p className="text-muted-foreground">
              Evaluations can be added only to an approved or published group, and none of your
              groups is there yet. Submit a group for approval first, then add evaluations to it.
            </p>
            <Button onClick={() => navigate('/evaluation-groups')}>View groups</Button>
          </>
        ) : (
          <>
            <p className="text-muted-foreground">
              Evaluations live inside an evaluation group, and there aren’t any yet. Create a group
              first, then add evaluations to it.
            </p>
            <Button onClick={() => navigate('/evaluation-groups/new')}>Create a group</Button>
          </>
        )}
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2"
        onClick={() =>
          navigate(
            isEdit
              ? `/evaluations/${id}`
              : groupId
                ? `/evaluation-groups/${groupId}`
                : '/evaluations',
          )
        }
      >
        <ArrowLeft className="size-4" /> Back
      </Button>
      <PageHeader
        title={isEdit ? 'Edit evaluation' : 'New evaluation'}
        breadcrumbs={
          trailGroupId ? (
            <GroupIdentity
              groupId={trailGroupId}
              path={
                isEdit
                  ? [
                      ...(existing.data
                        ? [{ label: existing.data.title, to: `/evaluations/${id}` }]
                        : []),
                      { label: 'Edit' },
                    ]
                  : [{ label: 'New evaluation' }]
              }
            />
          ) : (
            <Breadcrumbs
              items={[
                { label: 'Evaluations', to: '/evaluations' },
                { label: isEdit ? 'Edit' : 'New' },
              ]}
            />
          )
        }
      />
      <Card>
        <CardContent className="pt-6">
          <form onSubmit={onSubmit} className="space-y-4" noValidate>
            {!isEdit && (
              <FormField
                label="Group"
                htmlFor="evaluation_group_id"
                error={formState.errors.evaluation_group_id?.message}
                description={
                  fromGroup ? 'You’re adding to this group; it can’t be changed here.' : undefined
                }
                descriptionId="evaluation_group_id_hint"
              >
                <PlainSelect
                  id="evaluation_group_id"
                  disabled={fromGroup}
                  {...register('evaluation_group_id')}
                >
                  {fromGroup ? (
                    // Render the preset group's option directly: the async groups list may not be
                    // loaded at mount, which would leave the disabled select stuck on the placeholder.
                    <option value={groupId}>{presetGroup.data?.title ?? 'Selected group'}</option>
                  ) : (
                    <>
                      <option value="">— select group —</option>
                      {groupItems.map((g) => (
                        <option key={g.id} value={g.id}>
                          {g.title}
                        </option>
                      ))}
                    </>
                  )}
                </PlainSelect>
              </FormField>
            )}
            <FormField label="Title" htmlFor="title" error={formState.errors.title?.message}>
              <Input id="title" {...register('title')} />
            </FormField>
            <FormField
              label="Description (optional)"
              htmlFor="description"
              error={formState.errors.description?.message}
            >
              <Textarea id="description" rows={4} {...register('description')} />
            </FormField>
            <FormField
              label="Cover image (optional)"
              labelId={coverLabelId}
              error={formState.errors.cover_image?.message}
            >
              <CoverImageField
                labelId={coverLabelId}
                value={coverImage}
                onChange={(key) => setValue('cover_image', key, { shouldDirty: true })}
                onUploadingChange={setCoverUploading}
              />
            </FormField>
            <div>
              <LicensePicker
                field={register('data_license_id')}
                value={evaluationLicenseId}
                error={formState.errors.data_license_id?.message}
                inheritLabel="Inherit"
                inheritLicense={inheritedGroupLicense}
                offerNoLicense
                describedBy={licenseNote ? licenseNoteId : undefined}
              />
              {licenseNote && (
                <p id={licenseNoteId} className="text-muted-foreground mt-1 text-xs">
                  {licenseNote}
                </p>
              )}
            </div>
            <Field orientation="horizontal">
              <Checkbox
                id="mask_models_enabled"
                checked={maskModels}
                onCheckedChange={(next) =>
                  setValue('mask_models_enabled', next === true, { shouldDirty: true })
                }
              />
              <FieldLabel htmlFor="mask_models_enabled" className="font-normal">
                Mask assigned model identities on reads
              </FieldLabel>
            </Field>
            <Field orientation="horizontal">
              <Checkbox
                id="tags_enabled"
                aria-describedby={tagsDescriptionId}
                checked={tagsEnabled}
                onCheckedChange={(next) =>
                  setValue('tags_enabled', next === true, { shouldDirty: true })
                }
              />
              <FieldContent>
                {/* Beside the label, not inside it: a button in a label would toggle the box. */}
                <div className="flex items-center gap-1">
                  <FieldLabel htmlFor="tags_enabled" className="font-normal">
                    Custom tags
                  </FieldLabel>
                  <InfoHint text={TAGS_HINT} />
                </div>
                <FieldDescription id={tagsDescriptionId}>
                  Checking this box allows custom tags to be added to conversations in this
                  evaluation.
                </FieldDescription>
              </FieldContent>
            </Field>
            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" variant="outline" onClick={() => navigate(-1)}>
                Cancel
              </Button>
              <Button type="submit" disabled={formState.isSubmitting || coverUploading}>
                {formState.isSubmitting ? 'Saving…' : isEdit ? 'Save' : 'Create'}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}

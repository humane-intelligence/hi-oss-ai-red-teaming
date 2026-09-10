"""Closed-set enums for evaluation rows — surfaced both on the wire and in DB enum columns."""

from enum import StrEnum


class PublicationStatus(StrEnum):
    """Review + publish lifecycle state of an evaluation (and later, an evaluation group).

    Spans two concerns deliberately: a moderation workflow
    (`pending_approval` → `changes_requested` / `approved` / `not_approved`)
    and a publish lifecycle (`draft` → `published` → `inactive`). The set is
    closed — adding a state requires a migration (`ALTER TYPE ... ADD VALUE`).
    """

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    NOT_APPROVED = "not_approved"
    PUBLISHED = "published"
    INACTIVE = "inactive"


# Group lifecycle states that accept new evaluations (the add paths 409 on any
# other state — see the evaluations service for the allowlist/no-bypass rationale).
# Lives here, not in that service, so the group list filter can share it without a
# services.evaluation_groups ⇄ services.evaluations import cycle.
GROUP_STATUSES_ACCEPTING_EVALUATIONS = frozenset({PublicationStatus.APPROVED, PublicationStatus.PUBLISHED})


class EvaluationStatus(StrEnum):
    """Lifecycle state of a single `Evaluation`.

    One value per box on the engagement state diagram; the combined boxes map to
    a single value (`In Progress / Draft` → `draft`, `Closed / Completed` →
    `completed`). Distinct from `PublicationStatus` (used by `EvaluationGroup`):
    the two objects have different lifecycles and naming. The set is closed —
    adding a state requires a migration (`ALTER TYPE ... ADD VALUE`).

    Only the `under_review → approved` / `under_review → rejected` transitions
    are enforced today (admin approval); the rest of the graph lands with the
    owner submit/publish flows.
    """

    NEW = "new"
    DRAFT = "draft"
    UNDER_REVIEW = "under_review"
    REJECTED = "rejected"
    APPROVED = "approved"
    PUBLISHED = "published"
    COMPLETED = "completed"


class EvaluationGroupAccessLevel(StrEnum):
    """Who may reach a group: everyone, only the owning organization, or invitation-only.

    `organization` scopes visibility to members of the group's `organization_id`
    (a separate axis from *belonging*) — it requires a live org, and reads
    fail-closed if that org is soft-deleted. The set is closed — adding a value
    requires a migration (`ALTER TYPE ... ADD VALUE`).
    """

    PUBLIC = "public"
    ORGANIZATION = "organization"
    INVITATION_ONLY = "invitation_only"


class MetricsAccessLevel(StrEnum):
    """Who may read a group's metrics dashboards — a separate axis from group visibility.

    A group carries one value for while it runs (`metrics_access_during`) and one
    for after it finished (`metrics_access_after`, applied iff the group's status
    is `inactive`). Group visibility always gates first (an invisible group reads
    as 404 regardless of level), and the in-group `owner` and the
    `evaluation_groups:manage` break-glass see the *full* dashboards at every
    level. The levels widen from there: `owner_only` adds nobody;
    `members_personal_metrics` admits members holding
    `evaluation_groups:view_personal_metrics` (the `red_teamer` role) but scopes
    their dashboards to *their own* contributions only — a permission, not a
    role-name check, so future custom roles opt in by carrying it;
    `all_members` admits any member to the full dashboards; `inherit_group_access`
    admits whoever can see the group at all (the group's own `access_level` is the
    audience — never anonymous) to the full dashboards. The set is closed — adding
    a value requires a migration (`ALTER TYPE ... ADD VALUE`).
    """

    INHERIT_GROUP_ACCESS = "inherit_group_access"
    ALL_MEMBERS = "all_members"
    MEMBERS_PERSONAL_METRICS = "members_personal_metrics"
    OWNER_ONLY = "owner_only"


class MetricsScope(StrEnum):
    """The breadth of a granted metrics read — the output of the access policy.

    `full` is the whole-event aggregate (every member's contributions);
    `personal` is the same dashboards filtered to the caller's own submissions,
    conversations, and the reviews of their submissions. The policy
    (`resolve_metrics_scope`) returns one of these when access is granted, or
    `None` when it is denied (a 403). Surfaced on the metrics responses so the
    client can badge a personal view.
    """

    FULL = "full"
    PERSONAL = "personal"

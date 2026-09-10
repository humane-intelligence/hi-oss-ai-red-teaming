"""Catalog of audit actions.

`AuditAction` values are `domain.verb` strings, persisted as plain text in
`audit_logs.action` (not a Postgres enum) so the catalog can grow without a
migration — the same choice the repo makes for `OutboundEmail.template_name` and
`Role.permissions`. The StrEnum is the source-of-truth catalog on the write side.
"""

from enum import StrEnum


class AuditAction(StrEnum):
    """Sensitive actions and accesses worth auditing, grouped by domain."""

    AUTH_LOGIN = "auth.login"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_REGISTER = "auth.register"
    AUTH_EMAIL_VERIFIED = "auth.email_verified"
    # "credential" rather than "password" so secret-scanners (ruff S105 / bandit B105) don't
    # false-positive on the member name; these audit a credential reset, not a stored secret.
    AUTH_CREDENTIAL_RESET_REQUESTED = "auth.credential_reset_requested"
    AUTH_CREDENTIAL_RESET_CONFIRMED = "auth.credential_reset_confirmed"
    AUTH_CREDENTIAL_CHANGED = "auth.credential_changed"
    USER_UPDATE = "user.update"
    USER_DELETE = "user.delete"
    USER_RESTORE = "user.restore"
    USER_FORCE_LOGOUT = "user.force_logout"
    USER_STATUS_CHANGE = "user.status_change"
    USER_CREDENTIAL_RESET = "user.credential_reset"
    ROLE_CREATE = "role.create"
    ROLE_UPDATE = "role.update"
    ROLE_DELETE = "role.delete"
    ROLE_RESTORE = "role.restore"
    ORGANIZATION_CREATE = "organization.create"
    ORGANIZATION_UPDATE = "organization.update"
    ORGANIZATION_DELETE = "organization.delete"
    ORGANIZATION_RESTORE = "organization.restore"
    ORGANIZATION_MEMBER_ADD = "organization.member_add"
    ORGANIZATION_MEMBER_REMOVE = "organization.member_remove"
    MEMBER_ADD = "member.add"
    MEMBER_SET_ROLES = "member.set_roles"
    MEMBER_REMOVE = "member.remove"
    INVITATION_CREATE = "invitation.create"
    INVITATION_ACCEPT = "invitation.accept"
    INVITATION_RESEND = "invitation.resend"
    INVITATION_REVOKE = "invitation.revoke"

    EVALUATION_GROUP_CREATE = "evaluation_group.create"
    EVALUATION_GROUP_DRAFT = "evaluation_group.draft"
    EVALUATION_GROUP_DUPLICATE = "evaluation_group.duplicate"
    EVALUATION_GROUP_UPDATE = "evaluation_group.update"
    EVALUATION_GROUP_SUBMIT = "evaluation_group.submit"
    EVALUATION_GROUP_PUBLISH = "evaluation_group.publish"
    EVALUATION_GROUP_FINISH = "evaluation_group.finish"
    EVALUATION_GROUP_APPROVE = "evaluation_group.approve"
    EVALUATION_GROUP_REQUEST_CHANGES = "evaluation_group.request_changes"
    EVALUATION_GROUP_REJECT = "evaluation_group.reject"
    EVALUATION_GROUP_JOIN = "evaluation_group.join"

    EVALUATION_CREATE = "evaluation.create"
    EVALUATION_DUPLICATE = "evaluation.duplicate"
    EVALUATION_UPDATE = "evaluation.update"
    EVALUATION_DELETE = "evaluation.delete"
    EVALUATION_RESTORE = "evaluation.restore"
    EVALUATION_APPROVE = "evaluation.approve"
    EVALUATION_REJECT = "evaluation.reject"
    EVALUATION_TAG_KEY_ADD = "evaluation.tag_key_add"
    EVALUATION_TAG_KEY_REMOVE = "evaluation.tag_key_remove"
    EVALUATION_MODEL_ASSIGN = "evaluation.model_assign"
    EVALUATION_MODEL_UPDATE = "evaluation.model_update"
    EVALUATION_MODEL_UNASSIGN = "evaluation.model_unassign"
    EVALUATION_MODEL_RESTORE = "evaluation.model_restore"
    SCENARIO_CREATE = "scenario.create"
    SCENARIO_UPDATE = "scenario.update"
    SCENARIO_DELETE = "scenario.delete"
    SCENARIO_RESTORE = "scenario.restore"
    SCENARIO_REORDER = "scenario.reorder"
    TASK_CREATE = "task.create"
    TASK_UPDATE = "task.update"
    TASK_DELETE = "task.delete"
    TASK_RESTORE = "task.restore"
    AI_MODEL_CREATE = "ai_model.create"
    AI_MODEL_UPDATE = "ai_model.update"
    AI_MODEL_CREDENTIAL_SET = "ai_model.credential_set"
    AI_MODEL_CREDENTIAL_CLEAR = "ai_model.credential_clear"
    AI_MODEL_DELETE = "ai_model.delete"
    AI_MODEL_RESTORE = "ai_model.restore"

    # Only the tag write is audited on a conversation: tags are prompt context the model acts on,
    # so who changed them (and to what) is a governance question the transcript alone can't answer.
    CONVERSATION_TAGS_UPDATE = "conversation.tags_update"

    FLAG_CREATE = "flag.create"
    FLAG_UPDATE = "flag.update"
    FLAG_DELETE = "flag.delete"
    FLAG_RESTORE = "flag.restore"
    NOTE_CREATE = "note.create"
    NOTE_UPDATE = "note.update"
    NOTE_DELETE = "note.delete"
    NOTE_RESTORE = "note.restore"
    REVIEW_ASSIGN = "review.assign"
    REVIEW_VERDICT = "review.verdict"
    REVIEW_UNASSIGN = "review.unassign"
    REVIEW_RESTORE = "review.restore"

    # Annotations — no update action on purpose: the row has no editable content,
    # so the only writes are create / delete / restore. The `annotation.*` strings
    # were freed by the rename migration's audit backfill.
    ANNOTATION_CREATE = "annotation.create"
    ANNOTATION_DELETE = "annotation.delete"
    ANNOTATION_RESTORE = "annotation.restore"

    EXPORT_CREATE = "export.create"
    EXPORT_DELETE = "export.delete"
    EXPORT_DOWNLOAD = "export.download"
    EXPORT_READY = "export.ready"
    EXPORT_FAILED = "export.failed"
    AI_MODEL_CREDENTIAL_ACCESS = "ai_model.credential_access"
    DATA_READ = "data.read"

    PLATFORM_SETTINGS_UPDATE = "platform_settings.update"

    TERMS_PUBLISH = "terms.publish"
    TERMS_ACCEPT = "terms.accept"

    DATA_LICENSE_CREATE = "data_license.create"
    DATA_LICENSE_UPDATE = "data_license.update"
    DATA_LICENSE_DELETE = "data_license.delete"
    DATA_LICENSE_RESTORE = "data_license.restore"

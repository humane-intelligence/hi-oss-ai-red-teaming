"""v1 API router — every domain endpoint mounts under `/api/v1`."""

from fastapi import APIRouter

from app.api.v1.ai_models import router as ai_models_router
from app.api.v1.annotation_labels import router as annotation_labels_router
from app.api.v1.annotations import router as annotations_router
from app.api.v1.audit_logs import router as audit_logs_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.conversation_groups import router as conversation_groups_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.evaluation_group_annotators import router as evaluation_group_annotators_router
from app.api.v1.evaluation_group_invitations import router as evaluation_group_invitations_router
from app.api.v1.evaluation_group_members import router as evaluation_group_members_router
from app.api.v1.evaluation_group_metrics import router as evaluation_group_metrics_router
from app.api.v1.evaluation_groups import router as evaluation_groups_router
from app.api.v1.evaluation_metrics import router as evaluation_metrics_router
from app.api.v1.evaluations import router as evaluations_router
from app.api.v1.exports import router as exports_router
from app.api.v1.images import router as images_router
from app.api.v1.licenses import router as licenses_router
from app.api.v1.message_flags import router as message_flags_router
from app.api.v1.messages import router as messages_router
from app.api.v1.model_warmup import router as model_warmup_router
from app.api.v1.notes import router as notes_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.organizations import router as organizations_router
from app.api.v1.permissions import router as permissions_router
from app.api.v1.platform_settings import router as platform_settings_router
from app.api.v1.reviews import router as reviews_router
from app.api.v1.roles import router as roles_router
from app.api.v1.saved_views import router as saved_views_router
from app.api.v1.scenarios import router as scenarios_router
from app.api.v1.task_completions import router as task_completions_router
from app.api.v1.tasks import router as tasks_router
from app.api.v1.terms import router as terms_router

v1_router = APIRouter(prefix="/api/v1")
v1_router.include_router(auth_router)
v1_router.include_router(roles_router)
v1_router.include_router(permissions_router)
v1_router.include_router(organizations_router)
v1_router.include_router(ai_models_router)
v1_router.include_router(evaluations_router)
v1_router.include_router(evaluation_metrics_router)
v1_router.include_router(exports_router)
v1_router.include_router(evaluation_groups_router)
v1_router.include_router(evaluation_group_members_router)
v1_router.include_router(evaluation_group_invitations_router)
v1_router.include_router(evaluation_group_annotators_router)
v1_router.include_router(evaluation_group_metrics_router)
v1_router.include_router(scenarios_router)
v1_router.include_router(conversations_router)
v1_router.include_router(conversation_groups_router)
v1_router.include_router(messages_router)
v1_router.include_router(model_warmup_router)
v1_router.include_router(tasks_router)
v1_router.include_router(chat_router)
v1_router.include_router(notes_router)
v1_router.include_router(annotations_router)
v1_router.include_router(annotation_labels_router)
v1_router.include_router(message_flags_router)
v1_router.include_router(task_completions_router)
v1_router.include_router(reviews_router)
v1_router.include_router(licenses_router)
v1_router.include_router(images_router)
v1_router.include_router(platform_settings_router)
v1_router.include_router(audit_logs_router)
v1_router.include_router(saved_views_router)
v1_router.include_router(notifications_router)
v1_router.include_router(terms_router)

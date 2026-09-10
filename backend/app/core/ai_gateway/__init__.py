"""Provider-agnostic interface for external AI model providers.

Owns the model registry (`AiModel`), at-rest key encryption, and the dispatch
layer. Re-exports the litellm-free contract other domains may depend on: the
chat value types + `ProviderError`, and the cross-domain registry surface
(`AiModel`, `get_model`, `soft_delete_model`, `ProviderVendor`, the inference-
params toolkit). Import `dispatch_chat` / `dispatch_stream` from `.dispatch`
directly so this package's import path stays litellm-free for the CRUD code.
"""

from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatCompletion
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.enums import WarmupStatus
from app.core.ai_gateway.exceptions import ProviderError
from app.core.ai_gateway.inference_params import InferenceParams
from app.core.ai_gateway.inference_params import InferenceParamsMixin
from app.core.ai_gateway.inference_params import dump_inference_params
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.services.ai_models import get_model
from app.core.ai_gateway.services.ai_models import soft_delete_model

__all__ = [
    "AiModel",
    "ChatChunk",
    "ChatCompletion",
    "ChatMessage",
    "InferenceParams",
    "InferenceParamsMixin",
    "ProviderError",
    "ProviderVendor",
    "WarmupStatus",
    "dump_inference_params",
    "get_model",
    "soft_delete_model",
]

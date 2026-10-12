"""AI illustration generation (self-hosted open-source image model behind a provider interface)."""

from .base import ImageGenerationError, ImageGenerationProvider, ImageJobStatus, ImageRequest, ImageResult
from .planner import IllustrationPlan, apply_illustrations, plan_illustrations
from .service import IllustrationOutcome, IllustrationService, get_image_provider

__all__ = [
    "IllustrationOutcome", "IllustrationPlan", "IllustrationService", "ImageGenerationError",
    "ImageGenerationProvider", "ImageJobStatus", "ImageRequest", "ImageResult", "apply_illustrations",
    "get_image_provider", "plan_illustrations",
]

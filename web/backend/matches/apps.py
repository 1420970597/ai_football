import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class MatchesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'matches'

    def ready(self) -> None:  # pragma: no cover - startup hook
        try:
            from matches import runtime
            if runtime.should_start_services():
                runtime.start_runtime_services()
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to start runtime services", exc_info=exc)

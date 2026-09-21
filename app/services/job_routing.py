from __future__ import annotations

from app.config import get_settings
from app.models import ParserType

QUEUE_TELEGRAM_LIVE = "telegram_live"
QUEUE_TELEGRAM_BACKFILL = "telegram_backfill"
QUEUE_DARKNET = "darknet"
QUEUE_WEB = "web"
QUEUE_WHATSAPP = "whatsapp"


def _parser_type_value(parser_type: ParserType | str) -> str:
    if isinstance(parser_type, ParserType):
        return parser_type.value
    return str(parser_type or "").strip().lower()


def is_telegram_backfill_job_key(job_key: str | None) -> bool:
    key = str(job_key or "").strip().lower()
    return key.startswith("backfill:") or key.startswith("backfill-full:")


def resolve_job_queue(parser_type: ParserType | str, job_key: str | None = None, explicit_queue: str | None = None) -> str:
    if explicit_queue:
        return str(explicit_queue).strip().lower()

    parser_type_value = _parser_type_value(parser_type)
    if parser_type_value == ParserType.telegram.value:
        if is_telegram_backfill_job_key(job_key):
            return QUEUE_TELEGRAM_BACKFILL
        return QUEUE_TELEGRAM_LIVE
    if parser_type_value == ParserType.darknet.value:
        return QUEUE_DARKNET
    if parser_type_value == "web":
        return QUEUE_WEB
    if parser_type_value == "whatsapp":
        return QUEUE_WHATSAPP
    return parser_type_value or QUEUE_WEB


def default_max_attempts_for_queue(queue: str) -> int:
    settings = get_settings()
    normalized = str(queue or "").strip().lower()
    if normalized == QUEUE_TELEGRAM_BACKFILL:
        return max(int(settings.worker_telegram_backfill_max_attempts), 1)
    if normalized == QUEUE_TELEGRAM_LIVE:
        return max(int(settings.worker_telegram_live_max_attempts), 1)
    if normalized == QUEUE_DARKNET:
        return max(int(settings.worker_darknet_max_attempts), 1)
    if normalized == QUEUE_WEB:
        return max(int(settings.worker_web_max_attempts), 1)
    return max(int(settings.worker_default_max_attempts), 1)

from __future__ import annotations

from app.config import get_settings, validate_settings
from app.logging_config import configure_logging

_settings = get_settings()
configure_logging(_settings.log_level)
validate_settings(_settings)

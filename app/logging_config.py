from __future__ import annotations

import logging


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger once, for every entry point.

    `logging.basicConfig` only installs handlers if the root logger has
    none yet, so calling this more than once (app + cli both import `app`)
    is harmless.
    """
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

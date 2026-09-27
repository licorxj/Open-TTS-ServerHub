from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
    "{name}:{function}:{line} | {message}"
)


def configure_logging(
    *,
    level: str | None = None,
    log_file: str | os.PathLike[str] | None = None,
) -> None:
    resolved_level = (level or os.environ.get("DOTS_TTS_LOG_LEVEL") or DEFAULT_LOG_LEVEL).upper()
    logger.remove()
    logger.add(
        sys.stderr,
        level=resolved_level,
        format=DEFAULT_LOG_FORMAT,
        backtrace=True,
        diagnose=False,
        enqueue=False,
    )
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_path,
            level=resolved_level,
            format=DEFAULT_LOG_FORMAT,
            backtrace=True,
            diagnose=False,
            enqueue=False,
            encoding="utf-8",
        )

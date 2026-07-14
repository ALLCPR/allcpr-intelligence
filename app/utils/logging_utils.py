"""Shared logging setup."""
from __future__ import annotations

import logging
import os
import sys


def get_logger(name: str, level: int | None = None) -> logging.Logger:
    if level is None:
        env_level = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, env_level, logging.INFO)
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
                              datefmt="%H:%M:%S")
        )
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(level)
    return logger

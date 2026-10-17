"""Logging setup: one consistent console format for every module."""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT, datefmt=DATE_FORMAT, force=True)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

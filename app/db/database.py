"""Database initialisation helpers."""
from __future__ import annotations

from loguru import logger

from app.extensions import db


def init_db(app) -> None:
    """Create all tables if they do not exist."""
    with app.app_context():
        db.create_all()
        logger.info("Database tables verified / created.")
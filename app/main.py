"""
Flask application factory.
Import and call create_app() to get a configured Flask instance.
"""
from __future__ import annotations

from flask import Flask

from app.config import Config
from app.db.database import init_db
from app.extensions import db, socketio
from loguru import logger
import logging
import sys

def create_app() -> Flask:
    # Ensure directories exist before anything else
    Config.ensure_directories()

    logger.remove()

    logger.add(
        Config.LOG_PATH / "app_{time:YYYY-MM-DD}.log", 
        rotation="00:00", 
        retention="30 days", 
        encoding="utf-8", 
        level="DEBUG",
        enqueue=True
    )

    logger.add(
        sys.stdout,
        level="INFO",
        enqueue=True,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level:<8} | "
            "{message}"
        ),
    )

    setup_logging()

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config.from_object(Config)

    # Extensions
    db.init_app(app)
    socketio.init_app(
        app,
        async_mode=Config.SOCKETIO_ASYNC_MODE,
        cors_allowed_origins="*",
        logger=False,
        engineio_logger=False,
    )

    # Blueprints
    from app.routes.dashboard import bp as dashboard_bp
    from app.routes.crawler import bp as crawler_bp
    from app.routes.invoices import bp as invoices_bp
    from app.routes.export import bp as export_bp
    from app.routes.settings import bp as settings_bp
    from app.routes.snapshot import bp as snapshot_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(crawler_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(export_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(snapshot_bp)

    # Database tables
    init_db(app)

    # Restore auto-sync scheduler if it was active before restart.
    # Must be called AFTER init_db so DB models are ready.
    from app.services.crawler_service import restore_auto_sync_on_startup
    with app.app_context():
        restore_auto_sync_on_startup(app=app)

    return app

class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        logger.opt(
            exception=record.exc_info,
            depth=6,
        ).log(level, record.getMessage())


def setup_logging() -> None:
    intercept_handler = InterceptHandler()

    for name in (
        "werkzeug",
        "flask.app",
        "engineio",
        "socketio",
    ):
        log = logging.getLogger(name)
        log.handlers = [intercept_handler]
        log.propagate = False
        log.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    root_logger.handlers = [intercept_handler]
    root_logger.setLevel(logging.INFO)
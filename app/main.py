"""
Flask application factory.
Import and call create_app() to get a configured Flask instance.
"""
from __future__ import annotations

from flask import Flask

from app.config import Config
from app.db.database import init_db
from app.extensions import db, socketio
from app.utils.logger import setup_logging


def create_app() -> Flask:
    # Ensure directories exist before anything else
    Config.ensure_directories()

    # Logging
    setup_logging(Config.LOG_PATH)

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

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(crawler_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(export_bp)
    app.register_blueprint(settings_bp)

    # Database tables
    init_db(app)

    return app
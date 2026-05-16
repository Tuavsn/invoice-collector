"""Application entry point."""
from __future__ import annotations

from app.config import Config
from app.extensions import socketio
from app.main import create_app

app = create_app()

if __name__ == "__main__":
    socketio.run(
        app,
        host=Config.HOST,
        port=Config.PORT,
        debug=Config.DEBUG,
        use_reloader=False,  # Disabled — reloader breaks the background asyncio loop
        log_output=True,
    )
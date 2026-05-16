"""
Flask extension instances — imported by app factory and blueprints.
Avoids circular imports by creating extensions without binding to an app.
"""
from __future__ import annotations

from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
socketio = SocketIO()
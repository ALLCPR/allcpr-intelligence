"""ASGI entrypoint for production hosting.

Render starts this module directly. The FastAPI app itself lives in
``web_app.py`` to preserve the existing local development and test imports.
"""
from web_app import app


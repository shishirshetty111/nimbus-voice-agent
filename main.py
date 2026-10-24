"""Vercel entrypoint. Vercel looks for a FastAPI `app` in main.py at the project root and runs it as
one function that serves the whole site: pages, API and the voice WebSocket.

Locally, run the same app with:  uvicorn backend.app:app --port 8093
"""

from backend.app import app  # noqa: F401

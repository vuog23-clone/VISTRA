"""ASGI entry point for hosting the developer API (for example, on Vercel)."""

from api.server import create_app


app = create_app()

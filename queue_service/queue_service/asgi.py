"""ASGI entry point for the queue service — the only entry point.

There is no wsgi.py by design. SSE is a long-lived streaming response, and under WSGI every
waiter would occupy a worker thread or process for the whole of their wait. That is precisely
the resource exhaustion this service exists to prevent, so the synchronous door is not built.

Run locally:   uvicorn queue_service.asgi:application --port 8001
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "queue_service.settings")

application = get_asgi_application()

"""Conservative defaults for the 512 MB Render service.

Loaded automatically by the existing `gunicorn app:app --timeout 120` command.
One process avoids duplicating the app, AI SDKs and background scheduler. Two
threads keep navigation available while one request waits for an AI response.
"""
workers = 1
worker_class = "gthread"
threads = 2
preload_app = False

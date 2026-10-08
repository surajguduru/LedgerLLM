"""Tenant portal: email + password sign-in, self-service API keys, per-key and daily usage.

Served by the API itself (one deployable, D14): JSON endpoints under /app/api and plain HTML pages
under /app. Sessions are cookie-based; see app/portal/sessions.py for the security model (D24).
"""

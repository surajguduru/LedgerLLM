FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY app ./app
COPY config ./config
COPY prompts ./prompts
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 app && chown app /app
USER app
EXPOSE 8000
# Render/HF Spaces inject PORT; default to 8000 locally.
# Behind a proxy, request.client is the proxy unless uvicorn trusts its X-Forwarded-For; the portal's
# per-IP sign-in throttle needs the real client. FORWARDED_ALLOW_IPS lists the trusted proxies.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]

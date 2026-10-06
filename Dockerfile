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
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

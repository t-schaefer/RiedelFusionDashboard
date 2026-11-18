# Public demo of the Fusion Dashboard: simulated devices only, nothing real is touched.
# Build from the repository root:  docker build -f deploy/Dockerfile -t fusion-demo .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 demo
WORKDIR /app
COPY fusion-dashboard-service/ /app/
RUN chown -R demo /app
USER demo

EXPOSE 8190
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8190/api/mode', timeout=4)" || exit 1

# --reset-every: the demo exits after 30 minutes and Docker (restart: always) starts a fresh one.
CMD ["python", "server.py", "--demo", "--host", "0.0.0.0", "--port", "8190", "--reset-every", "30"]

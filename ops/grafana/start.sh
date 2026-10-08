#!/bin/sh
# Entrypoint for the deployed Grafana (ops/grafana/Dockerfile).
set -e

# Render and Hugging Face Spaces inject PORT; Grafana reads GF_SERVER_HTTP_PORT.
export GF_SERVER_HTTP_PORT="${PORT:-3000}"

if [ -z "$GF_SECURITY_ADMIN_PASSWORD" ]; then
  echo "refusing to start: GF_SECURITY_ADMIN_PASSWORD is unset." >&2
  echo "These dashboards expose every tenant's spend; this must not run on admin/admin." >&2
  exit 1
fi

# The operational dashboard queries Prometheus, which is not deployed (the app's counters are
# in-process and the free instance sleeps, so a scrape history would be mostly resets). Without a
# Prometheus to talk to, those panels would render as a wall of datasource errors -- so drop that
# dashboard and its datasource unless PROMETHEUS_URL actually points somewhere.
if [ -z "$PROMETHEUS_URL" ]; then
  rm -f /var/lib/grafana/dashboards/ledgerllm.json \
        /etc/grafana/provisioning/datasources/prometheus.yml
  echo "PROMETHEUS_URL unset: serving the Admin dashboard only." >&2
fi

if [ -z "$GRAFANA_DB_HOST" ]; then
  echo "warning: GRAFANA_DB_HOST unset -- the Admin dashboard will show a datasource error." >&2
fi

exec /run.sh "$@"

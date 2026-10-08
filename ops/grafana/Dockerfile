# Grafana with our datasources and dashboards baked in, for deployment as a Render web service.
#
# Why an image rather than mounted volumes: Render has no host filesystem to mount from, and
# Grafana's own config database is ephemeral on a free instance -- wiped on every restart. File
# provisioning is what makes that survivable: datasources and dashboards are recreated from these
# files on each boot, so losing Grafana's internal state costs nothing.
#
# Secrets come from the environment at run time (see render.yaml):
#   GF_SECURITY_ADMIN_PASSWORD            - required; start.sh refuses to boot without it
#   GRAFANA_DB_HOST/_USER/_NAME/_PASSWORD - the Neon READ REPLICA endpoint
#   PROMETHEUS_URL                        - optional; unset drops the Prometheus dashboard (see start.sh)
FROM grafana/grafana:11.3.0

# Anonymous access is ON in docker-compose for local convenience. It must be OFF here: these
# dashboards show every tenant's spend and the Render URL is public.
ENV GF_AUTH_ANONYMOUS_ENABLED=false \
    GF_USERS_ALLOW_SIGN_UP=false \
    GF_SECURITY_COOKIE_SECURE=true \
    GF_ANALYTICS_REPORTING_ENABLED=false \
    GF_ANALYTICS_CHECK_FOR_UPDATES=false

USER root
COPY provisioning /etc/grafana/provisioning
COPY dashboards /var/lib/grafana/dashboards
COPY start.sh /usr/local/bin/start.sh
# start.sh prunes provisioning at boot, so these must be writable by the grafana user.
RUN chmod +x /usr/local/bin/start.sh \
 && chown -R grafana:root /etc/grafana/provisioning /var/lib/grafana/dashboards
USER grafana
ENTRYPOINT ["/usr/local/bin/start.sh"]

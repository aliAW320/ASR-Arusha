FROM nginx:1.29-alpine

COPY Docker/ui.nginx.conf /etc/nginx/conf.d/default.conf
COPY ui/ /usr/share/nginx/html/

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD wget -qO- http://127.0.0.1/ui-health >/dev/null || exit 1

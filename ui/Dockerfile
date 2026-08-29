FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html styles.css app.js /usr/share/nginx/html/
COPY scripts /usr/share/nginx/html/scripts
COPY styles /usr/share/nginx/html/styles

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --retries=10 CMD wget -qO- http://127.0.0.1/health || exit 1

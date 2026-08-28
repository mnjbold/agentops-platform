FROM nginx:alpine
LABEL org.opencontainers.image.source="https://github.com/mybijouai-creator/agentops"
LABEL org.opencontainers.image.description="agentops — cloud telephony PWA shell"
# PWA assets must land alongside index.html so the root scope serves them.
COPY index.html      /usr/share/nginx/html/index.html
COPY campaigns.js    /usr/share/nginx/html/campaigns.js
COPY manifest.json   /usr/share/nginx/html/manifest.json
COPY sw.js           /usr/share/nginx/html/sw.js
COPY icons/          /usr/share/nginx/html/icons/
COPY nginx.conf      /etc/nginx/conf.d/default.conf
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
CMD ["nginx", "-g", "daemon off;"]

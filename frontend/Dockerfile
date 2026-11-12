# Agent-X web image (Phase 8): builds the static UI, then serves it with unprivileged nginx, which also reverse-proxies
# /api and /health to the API container (same origin, so no CORS is needed). No secret is baked in: the UI holds no
# backend credential and talks only to /api on its own origin. TLS terminates in front of this (docs/DEPLOYMENT.md).

FROM node:20-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY proxy_params.inc /etc/nginx/conf.d/proxy_params.inc
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/nginx-health || exit 1

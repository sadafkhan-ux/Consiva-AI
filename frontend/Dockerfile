# Consiva Consent Agent — frontend (React 19 + Vite 8, built with npm).
# Multi-stage: Node only builds; the runtime image is nginx serving static files, so
# neither Node nor any devDependency ships to production.

# ---------- build stage ----------
FROM node:22-alpine AS build

WORKDIR /app

# package.json + lockfile first so `npm ci` is cached until dependencies actually
# change. `npm ci` (not `npm install`) because package-lock.json is committed and a
# reproducible install is the point of a production build.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite inlines env vars at BUILD time, not runtime -- so the API base URL has to be
# fixed here, as a build arg, not an environment variable on the running container.
#
# The default is deliberately an EMPTY string: src/api/client.ts composes requests as
# `${BASE_URL}${path}`, so "" yields same-origin relative URLs (/api/v1/...) which
# nginx then proxies to the backend. That is the correct choice for compose, because
# these requests are issued by the user's browser and could never resolve a Docker
# service hostname. Override only when the API is served from a genuinely different
# public origin, e.g. --build-arg VITE_API_BASE_URL=https://api.example.com
#
# (client.ts falls back to http://127.0.0.1:8000 via `??`, which triggers on
# undefined but NOT on "" -- so an empty value here stays empty, as intended.)
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

# The project's real build script: "tsc -b && vite build" -> outputs to dist/
RUN npm run build

# ---------- runtime stage ----------
# nginx-unprivileged is the official image built for exactly this: it already runs as
# UID 101, already listens on 8080, and every path nginx needs to write (the
# client_temp/proxy_temp/fastcgi_temp/uwsgi_temp/scgi_temp cache dirs and the pidfile)
# is already owned by that user.
#
# This replaced a hand-rolled `chown + sed + USER nginx` on the stock nginx:alpine
# image, which crash-looped on startup (container stuck "Restarting (1)"): the stock
# config and its docker-entrypoint expect root, so an unprivileged user hits a write
# it cannot perform and nginx exits immediately. Chasing that with more chowns is
# fragile -- using the image purpose-built for it is not.
FROM nginxinc/nginx-unprivileged:1.29-alpine AS runtime

# --chown so the served files are readable by the unprivileged runtime user rather
# than landing as root-owned (COPY writes as UID 0 by default).
COPY --from=build --chown=101:101 /app/dist /usr/share/nginx/html
COPY --chown=101:101 nginx.conf /etc/nginx/conf.d/default.conf

# 8080, not 80 -- an unprivileged process cannot bind a port below 1024.
EXPOSE 8080

# busybox wget ships in the alpine base, so no extra package is needed to health-check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:8080/ || exit 1

CMD ["nginx", "-g", "daemon off;"]

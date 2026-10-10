# Production web image: Caddy (public HTTPS entry) + the built frontend. Spec: docs/design/deployment.md §2, §5, §6
# Build context = the repo root:  docker build -f frontend/Dockerfile -t parking-web .
# The app is built for the same origin as the API (VITE_BASE=/, empty VITE_API_BASE), so one image
# fits any hostname: the hostname is PUBLIC_HOST at run time (deploy/Caddyfile).
FROM --platform=$BUILDPLATFORM node:22-alpine AS build
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# the admin slot/line editor is imported from here (vite.config.ts, @slot-editor)
COPY tools/slot-editor/ /src/tools/slot-editor/
ENV VITE_BASE=/ VITE_API_BASE=
RUN npm run build

FROM caddy:2.11-alpine AS web
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /src/frontend/dist /srv
RUN caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

# syntax=docker/dockerfile:1

# CardDemo frontend image: builds the Next.js 16 application and serves its standalone output
# with `node server.js` on port 3000. Built by the docker-compose.yml service `frontend`
# (profile `full`, context ./frontend) as AAP 0.11.1 defines it.
#
# Next.js inlines NEXT_PUBLIC_* values into the browser bundle during `npm run build`, so the
# backend origin is a build argument; setting it when the container starts has no effect. The
# browser runs on the host, so the value is the host-visible origin, not the Compose hostname.
#
# Build and run from the repository root:
#
#     docker build -t carddemo-frontend \
#         --build-arg NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 frontend
#     docker run --rm -p 127.0.0.1:3000:3000 carddemo-frontend

# Stage deps: install the exact lockfile set, development dependencies included, because
# `next build` needs TypeScript and the type definitions.
FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# Stage builder: compile the application into .next/standalone. The context is filtered by
# frontend/.dockerignore, so no host node_modules or .next and no real .env file reaches this
# stage; the .env*.example templates are the only .env* files copied. The build downloads
# Roboto through next/font/google and therefore needs network access.
FROM node:22-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# The backend origin only, never a secret: the value ships to every browser.
ARG NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
RUN test -n "$NEXT_PUBLIC_API_URL" \
    || { echo "NEXT_PUBLIC_API_URL must not be empty" >&2; exit 1; }
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# Stage runner: the standalone server plus the static assets and public files it does not
# bundle, the same layout as the native `cp -rT .next/static .next/standalone/.next/static`
# and `cp -rT public .next/standalone/public` steps, run as the image's non-root user.
# The server listens on every interface; Compose publishes the port on 127.0.0.1 only.
FROM node:22-slim AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]

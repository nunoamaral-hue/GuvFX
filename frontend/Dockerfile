FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* pnpm-lock.yaml* yarn.lock* ./
RUN \
  if [ -f package-lock.json ]; then npm ci; \
  elif [ -f pnpm-lock.yaml ]; then corepack enable && pnpm i --frozen-lockfile; \
  elif [ -f yarn.lock ]; then yarn --frozen-lockfile; \
  else npm i; fi

FROM node:20-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
# Build provenance (IPR Area G). .git is dockerignored, so the commit comes from a build-arg. The
# `prebuild` emitter (scripts/emit-build-info.mjs) reads these from the env and bakes a static,
# self-surfacing public/build-info.json (served at /build-info.json). Both NON-SECRET, default
# "unknown" when absent (local dev).
ARG GIT_COMMIT=unknown
ARG BUILD_TIMESTAMP=unknown
ENV GIT_COMMIT=$GIT_COMMIT \
    BUILD_TIMESTAMP=$BUILD_TIMESTAMP
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=build /app ./
EXPOSE 3000
CMD ["npm","run","start","--","-p","3000"]

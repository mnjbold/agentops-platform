# =====================================================================
# agentops frontend — multi-stage Dockerfile
# Stage 1: build the Vite bundle
# Stage 2: serve with nginx (small, fast, production-grade)
# =====================================================================

# --- Stage 1: build ---
FROM node:20-alpine AS builder
WORKDIR /app

# Install deps (cache-friendly: copy package files first)
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

# Copy source
COPY . .

# Build with optional API base envs
ARG VITE_API_BASE_PRIMARY=https://bkjr-api.getbijou.xyz
ARG VITE_API_BASE_FALLBACK=https://bk-jr-api.aixlabs.fun
ENV VITE_API_BASE_PRIMARY=$VITE_API_BASE_PRIMARY
ENV VITE_API_BASE_FALLBACK=$VITE_API_BASE_FALLBACK

RUN npm run build

# --- Stage 2: serve ---
FROM nginx:alpine AS runner
LABEL org.opencontainers.image.title="agentops-frontend" \
      org.opencontainers.image.description="AI agentic telephony PWA" \
      org.opencontainers.image.source="https://github.com/mnjbold/agentops-platform"

# nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Static build
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O- http://localhost/healthz || exit 1
CMD ["nginx", "-g", "daemon off;"]

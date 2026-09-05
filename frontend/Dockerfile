# ============================================================
# AI Finance Controller — Frontend Dockerfile
# ============================================================
# Two-stage build:
#   Stage 1 (builder) — Node 20 Alpine: npm ci + vite build
#   Stage 2           — Nginx Alpine:   serve static files + proxy /api
# ============================================================

# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

# Install dependencies (ci ensures exact lock-file versions)
COPY package.json package-lock.json ./
RUN npm ci

# Copy source and build production bundle
COPY . .
RUN npm run build

# ── Stage 2: Serve ────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS production

# Copy Vite production build
COPY --from=builder /app/dist /usr/share/nginx/html

# Replace default Nginx config with our custom config
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]

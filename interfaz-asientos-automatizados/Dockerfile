FROM node:20-alpine AS base

# Instalar pnpm
RUN corepack enable && corepack prepare pnpm@latest --activate

FROM base AS deps
WORKDIR /app

# Copiar archivos de dependencias
COPY package.json pnpm-lock.yaml ./

# Instalar dependencias
RUN pnpm install --frozen-lockfile

FROM base AS builder
WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Build de la aplicación
RUN pnpm build

FROM base AS runner
WORKDIR /app

ENV NODE_ENV=production

# Versión inyectada por la GitHub Action en build time.
# Disponible para server components vía process.env.AIWAF_VERSION.
# (No exponer como NEXT_PUBLIC_*: el bundle JS cliente NO la usa — la versión
# real la sirve el backend pipeline-api desde su propia ENV. Setearla aquí
# como NEXT_PUBLIC_ tampoco funcionaría: Next.js inlinea esas vars en build,
# y el ARG llega después de pnpm build.)
ARG AIWAF_VERSION=0.0.0-dev
ENV AIWAF_VERSION=${AIWAF_VERSION}

# Crear usuario non-root
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Copiar archivos necesarios
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]

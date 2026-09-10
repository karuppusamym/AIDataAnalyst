FROM node:22-alpine AS build

WORKDIR /app

# Vite embeds VITE_* values at build time. The container default uses the live
# API; developers can still opt into fixtures with
# UI_NEXT_USE_FIXTURES=1 when building the Compose service.
ARG VITE_USE_FIXTURES=0
ENV VITE_USE_FIXTURES=${VITE_USE_FIXTURES}
ARG VITE_DEV_PRINCIPAL_ID=
ENV VITE_DEV_PRINCIPAL_ID=${VITE_DEV_PRINCIPAL_ID}
ARG VITE_DEV_ROLES=
ENV VITE_DEV_ROLES=${VITE_DEV_ROLES}

# How a request proves who is making it (review 2026-09-05, F06). Unset means
# "development", which is the only mode that works without further
# configuration -- and the app says so on screen rather than implying a
# deployment choice nobody made. `oidc` additionally needs an issuer and a
# client id, without which the shell blocks with an explanation instead of
# offering a sign-in button that could not work.
ARG VITE_AUTH_MODE=
ENV VITE_AUTH_MODE=${VITE_AUTH_MODE}
ARG VITE_OIDC_ISSUER=
ENV VITE_OIDC_ISSUER=${VITE_OIDC_ISSUER}
ARG VITE_OIDC_CLIENT_ID=
ENV VITE_OIDC_CLIENT_ID=${VITE_OIDC_CLIENT_ID}
ARG VITE_OIDC_SCOPE=
ENV VITE_OIDC_SCOPE=${VITE_OIDC_SCOPE}
ARG VITE_OIDC_REDIRECT_PATH=
ENV VITE_OIDC_REDIRECT_PATH=${VITE_OIDC_REDIRECT_PATH}

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --retries=10 CMD wget -qO- http://127.0.0.1/health || exit 1

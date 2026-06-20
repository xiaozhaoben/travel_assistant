FROM node:22-alpine AS build

WORKDIR /app

ARG VITE_API_BASE_URL=
ARG VITE_API_TIMEOUT_MS=300000
ARG VITE_BASE_PATH=/
ARG VITE_AMAP_WEB_JS_KEY=
ARG VITE_AMAP_SECURITY_JS_CODE=

ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_API_TIMEOUT_MS=${VITE_API_TIMEOUT_MS} \
    VITE_BASE_PATH=${VITE_BASE_PATH} \
    VITE_AMAP_WEB_JS_KEY=${VITE_AMAP_WEB_JS_KEY} \
    VITE_AMAP_SECURITY_JS_CODE=${VITE_AMAP_SECURITY_JS_CODE}

COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]

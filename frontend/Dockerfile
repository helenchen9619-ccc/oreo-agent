# 生产前端镜像：node 构建 dist → nginx 托管；站点配置由 compose 挂 deploy/nginx.conf
FROM node:20-alpine AS build
# 国内服务器构建走 npmmirror（compose 生产编排已内置该 arg）
ARG NPM_REGISTRY=""
WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi && (npm ci || npm install)
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80 443

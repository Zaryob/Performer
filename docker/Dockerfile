# syntax=docker/dockerfile:1

# Build the same self-contained HTML used by the file:// viewer. No Node.js,
# package registry, or application API is needed in the running image.
FROM node:24-alpine AS viewer-build
WORKDIR /app
COPY viewer/package.json viewer/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY viewer/ ./
RUN npm run build

FROM busybox:1.37
COPY --from=viewer-build /app/dist/index.html /www/index.html
USER 65534:65534
EXPOSE 8080
CMD ["busybox", "httpd", "-f", "-p", "8080", "-h", "/www"]

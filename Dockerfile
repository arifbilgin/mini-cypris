# syntax=docker/dockerfile:1

# Stage 1: build the Angular app
FROM node:24-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npx ng build --configuration production

# Stage 2: Flask serves the API and the built app on one port
FROM python:3.13-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FRONTEND_DIST=/srv/static \
    DATABASE_PATH=/data/cache.db
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY backend/wsgi.py ./
COPY --from=web /web/dist/frontend/browser ./static
RUN mkdir -p /data
VOLUME /data
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "wsgi:app"]

FROM node:22-bookworm-slim AS frontend-build

WORKDIR /workspace/FrontEnd
COPY FrontEnd/package.json FrontEnd/package-lock.json ./
RUN npm ci
COPY FrontEnd/ ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY Backend/requirements.txt Backend/requirements.txt
RUN pip install --no-cache-dir -r Backend/requirements.txt

COPY Backend/ Backend/
COPY --from=frontend-build /workspace/FrontEnd/dist/ FrontEnd/dist/

EXPOSE 10000
CMD ["sh", "-c", "gunicorn --chdir Backend app:app --bind 0.0.0.0:${PORT:-10000}"]

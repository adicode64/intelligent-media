# Frontend — Intelligent Media Processing Pipeline

React 18 + TypeScript + Vite client for the FastAPI pipeline.

## Run

```bash
cd frontend
npm install
cp .env.example .env      # point VITE_API_BASE_URL at your API
npm run dev               # http://localhost:5173
```

Backend must be running (`uvicorn app.main:app --reload`) with Redis, Postgres and the
Celery worker up, otherwise uploads stay `pending`.

## CORS

The API allows `http://localhost:5173` by default. Change it via `CORS_ORIGINS` in the
backend `.env` (comma-separated), or set `DEBUG=true` to allow any origin in development.

## What it does

- Drag-and-drop / browse upload with local preview and size + type hints
- `POST /api/v1/images/upload`, then polls `GET /api/v1/images/{id}/results` every 1.5 s
- Renders the summary (overall verdict, confidence, pass/warn/fail/uncertain counts, notes),
  the extracted vehicle number, and every individual check with score and confidence
- Surfaces API errors verbatim (413 too large, 415 unsupported, 422 corrupt, 503 queue down)

## Build

```bash
npm run build     # dist/ — serve statically behind any web server
```

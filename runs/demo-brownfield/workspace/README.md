# URL shortener

A small FastAPI + SQLite URL shortener with SSRF-hardened target validation,
fixed-window rate limiting in ASGI middleware, and per-API-key creation quotas.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Endpoints

| Method | Path                        | Notes                                              |
|--------|-----------------------------|----------------------------------------------------|
| GET    | `/health`                   | Liveness only, never touches user data, not limited |
| POST   | `/api/links`                | Creates a link. Rate limited (per key or per IP).   |
| GET    | `/{code}`                   | 307 redirect to the stored target. Per-IP limited.  |
| GET    | `/api/links/{code}/stats`   | Click statistics. Never rate limited.               |

All errors use the stable envelope `{"error": {"code": ..., "message": ...}}`.

## Configuration

| Variable                        | Default                | Purpose |
|---------------------------------|------------------------|---------|
| `LINKS_DB_PATH`                 | `./data/links.db`      | SQLite file (WAL). |
| `LINKS_BASE_URL`                | `http://localhost:8000`| Origin for `short_url`; trailing slash stripped. |
| `LINKS_CODE_LENGTH`             | `7`                    | Base62 code length, 4..16 else 7. |
| `LINKS_CODE_MAX_ATTEMPTS`       | `5`                    | Insert attempts on code collision. |
| `LINKS_MAX_URL_LENGTH`          | `2048`                 | Max accepted target url length. |
| `LINKS_DEFAULT_TTL_SECONDS`     | `0`                    | 0 means never expires. |
| `LINKS_MAX_TTL_SECONDS`         | `31536000`             | Upper bound for `ttl_seconds`. |
| `LINKS_RATE_LIMIT_ENABLED`      | `true`                 | Master switch for the limiter. |
| `LINKS_RATE_LIMIT_MAX`          | `10`                   | Per-IP creation budget; redirects get x100. |
| `LINKS_RATE_LIMIT_WINDOW_SECONDS`| `60`                  | Fixed window length and Retry-After clamp. |
| `LINKS_MAX_TRACKED_KEYS`        | `10000`                | LRU cap, applied per bucket map. |
| `LINKS_LOG_LEVEL`               | `INFO`                 | Log level. |
| `SHORTENER_API_KEYS`            | *(empty)*              | `name:quota` pairs, comma separated. |

API key values are never persisted, never logged and never echoed in a response.

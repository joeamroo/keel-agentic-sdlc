# link-shortener

A small FastAPI + SQLite URL shortener with SSRF-hardened target validation and
a single, centrally enforced sliding-window rate limiter that understands
per-API-key creation quotas.

## Run

```
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Endpoints

| Method | Path                        | Purpose                                   |
| ------ | --------------------------- | ----------------------------------------- |
| POST   | `/api/links`                | Create a short link                        |
| GET    | `/{code}`                   | 307 redirect to the stored target          |
| GET    | `/api/links/{code}/stats`   | Click statistics (reports `expired`)       |
| GET    | `/health`                   | Liveness; touches no user data             |

Every error uses one shape:

```json
{"error": {"code": "rate_limited", "message": "Rate limit exceeded. Please retry later."}}
```

## Configuration

| Variable                                | Default                  | Purpose |
| --------------------------------------- | ------------------------ | ------- |
| `LINKS_DB_PATH`                         | `./links.db`             | SQLite file path |
| `LINKS_BASE_URL`                        | `http://localhost:8000`  | Origin used for `short_url` (trailing slash stripped) |
| `LINKS_CODE_LENGTH`                     | `7`                      | Base62 code length; values outside 4..16 log and fall back |
| `LINKS_CODE_MAX_ATTEMPTS`               | `5`                      | Insert retries on code collision before 503 |
| `LINKS_MAX_TTL_SECONDS`                 | `31536000`               | Upper bound for `expires_in_seconds` |
| `LINKS_RATE_LIMIT_ENABLED`              | `true`                   | Master switch; `false/0/no/off` disable |
| `LINKS_RATE_LIMIT_MAX`                  | `10`                     | Per-IP creation allowance per window |
| `LINKS_RATE_LIMIT_WINDOW_SECONDS`       | `60`                     | Sliding window length |
| `LINKS_RATE_LIMIT_REDIRECT_MULTIPLIER`  | `100`                    | Read allowance = max * multiplier |
| `SHORTENER_API_KEYS`                    | (empty)                  | `name:quota` pairs, comma separated |
| `LOG_LEVEL`                             | `INFO`                   | Root log level |

API keys are credentials: they are hashed with a per-boot salt on arrival and
never written to the database, a log record or a response body.

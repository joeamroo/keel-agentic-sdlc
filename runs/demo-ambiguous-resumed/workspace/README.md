# links — public URL shortening service

A small FastAPI + SQLite service that issues short redirect links.

* `POST /links` — create a short link (unauthenticated, per-IP rate limited).
* `GET /{code}` — 302 redirect to the stored destination (re-validated at read time).
* `GET /healthz` (alias `GET /health`) — liveness, touches no user data.

## Running

```
pip install -r requirements.txt
python -m app.main
```

## Configuration (environment variables)

| Name | Default | Purpose |
| --- | --- | --- |
| `LINKS_DB_PATH` | `./links.db` | SQLite file path; `:memory:` accepted for ephemeral runs. |
| `LINKS_BIND_HOST` | `127.0.0.1` | Bind interface. |
| `LINKS_BIND_PORT` | `8080` | Bind port (plain HTTP behind a TLS-terminating proxy). |
| `LINKS_PUBLIC_BASE_URL` | `https://short.example.com` | Origin used to build `short_url`. |
| `LINKS_DEFAULT_EXPIRY_DAYS` | `30` | Expiry applied when the caller omits `expires_in_days`. |
| `LINKS_MAX_EXPIRY_DAYS` | `365` | Inclusive upper bound for `expires_in_days`. |
| `LINKS_MAX_URL_LENGTH` | `2048` | Max characters of the submitted destination. |
| `LINKS_CODE_LENGTH` | `7` | Base62 characters per generated code. |
| `LINKS_CODE_MAX_ATTEMPTS` | `5` | Retries on code collision before 503. |
| `LINKS_RATE_LIMIT_MAX` | `20` | `POST /links` requests per IP per window. |
| `LINKS_RATE_LIMIT_WINDOW_SECONDS` | `60` | Fixed window length in seconds. |
| `LINKS_TRUST_PROXY_HEADER` | `false` | Take client IP from last `X-Forwarded-For` entry. |
| `LINKS_DNS_TIMEOUT_MS` | `2000` | Per-host A/AAAA resolution timeout. |
| `LINKS_DNS_CACHE_TTL_SECONDS` | `0` | Resolution cache TTL; 0 disables caching. |
| `LINKS_ALLOW_PRIVATE_DESTINATIONS` | `false` | Test-only escape hatch; must stay false in production. |
| `LINKS_LOG_LEVEL` | `info` | `debug`, `info`, `warn`, `error`. |

The service never issues an application-layer request to a user supplied
destination: validation is limited to parsing and name resolution.

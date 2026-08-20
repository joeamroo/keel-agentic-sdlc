# URL Shortener

Public JSON API that turns a caller supplied public `http(s)` URL into an
unguessable base62 short code, redirects visitors until the link expires and
exposes per-link click analytics that contain no visitor IP addresses.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| POST | `/api/links` | Create a short link (`{"url": "...", "expires_at": "..."}`) |
| GET | `/api/links/{code}/stats` | Click analytics (`limit`, `offset`) |
| GET | `/health` | Liveness; `{"status":"ok"}` or 503 `{"status":"degraded"}` |
| GET | `/{code}` | 307 redirect to the stored destination |

Errors always use `{"error": {"code": "...", "message": "..."}}`.

## Configuration

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `LINKS_DB_PATH` | `./links.db` | SQLite database file |
| `LINKS_BASE_URL` | `http://localhost:8000` | Origin used for `short_url` |
| `LINKS_DEFAULT_TTL_DAYS` | `30` | Default link lifetime |
| `LINKS_MAX_URL_LENGTH` | `2048` | Maximum accepted URL length |
| `LINKS_CODE_LENGTH` | `7` | Base62 code length |
| `LINKS_CODE_MAX_ATTEMPTS` | `5` | Insert attempts on code collision |
| `LINKS_RATE_LIMIT_MAX` | `10` | Creations per client per window |
| `LINKS_RATE_LIMIT_WINDOW_SECONDS` | `60` | Rolling window length |
| `LINKS_RATE_LIMIT_ENABLED` | `true` | `false`/`0`/`no` disables the limiter |
| `LINKS_TRUST_FORWARDED_FOR` | `false` | Use first `X-Forwarded-For` entry |
| `LINKS_STATS_DEFAULT_LIMIT` | `50` | Default clicks page size |
| `LINKS_STATS_MAX_LIMIT` | `500` | Maximum clicks page size |
| `LINKS_DNS_RESOLUTION_ENABLED` | `true` | Resolve and denylist-check hosts |
| `LINKS_LOG_LEVEL` | `INFO` | Root log level |

No secrets are read or stored by the service.

## Security notes

* Scheme allow-list (`http`, `https`) plus an address denylist covering
  loopback, private, link-local (including `169.254.169.254`), unique-local,
  CGNAT, multicast, unspecified and reserved ranges, with IPv4-mapped,
  IPv4-compatible, 6to4, Teredo and decimal/octal/hex IPv4 literals decoded.
* The service never performs an outbound HTTP request to a destination.
* Redirect targets come only from the validated, stored row.
* Rate limiting keys on a per-boot salted hash of the client address; no IP is
  written to the database, to a file or to a log.

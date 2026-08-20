# URL Shortener

A small FastAPI + SQLite URL shortener: create short links, follow them, read click
statistics. Link creation is rate limited; every other endpoint is not.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness probe. Touches no user data and no database. |
| `POST` | `/api/links` | Create a short link. Rate limited (see below). |
| `GET` | `/api/links/{code}/stats` | Click statistics for a code, including `expired`. |
| `GET` | `/{code}` | 307 redirect to the stored target URL (not listed in OpenAPI). |

### Create a link

```
POST /api/links
Content-Type: application/json

{"url": "https://example.com/a/very/long/path", "expires_in_seconds": 3600}
```

`expires_in_seconds` is optional (omit it for a link that never expires) and `code`
is an optional custom short code (`[A-Za-z0-9_-]{3,32}`). The response is `201`:

```json
{
  "code": "aB3dE7f",
  "short_url": "http://localhost:8000/aB3dE7f",
  "target_url": "https://example.com/a/very/long/path",
  "created_at": "2024-01-01T00:00:00.000000Z",
  "expires_at": "2024-01-01T01:00:00.000000Z"
}
```

### Expired links

Expiry is enforced on the read path. `GET /{code}` for an expired code returns the
exact same `404` body and headers as a code that never existed, so the redirect
endpoint cannot be used as an enumeration oracle. Only
`GET /api/links/{code}/stats` reveals expiry, via `200` with `"expired": true`.

### Errors

Every error uses one envelope and never contains a stack trace, a database message
or a filesystem path:

```json
{"error": {"code": "not_found", "message": "No such link."}}
```

## Configuration

All configuration comes from environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LINKS_DB_PATH` | `./links.db` | Filesystem path to the SQLite database file (WAL mode). This is the single canonical name; `DATABASE_PATH` is never read. |
| `LINKS_BASE_URL` | `http://localhost:8000` | Origin used to build the absolute `short_url` in the create response. Trailing slashes are stripped. |
| `LINKS_RATE_LIMIT_ENABLED` | `true` | Master switch for all limiting. When false there is no per-IP bucket, no per-key bucket, no `429`, and `SHORTENER_API_KEYS` has no effect. |
| `LINKS_RATE_LIMIT_MAX` | `10` | Maximum `POST /api/links` requests per window for the per-IP bucket. Applies only to callers without a recognised API key. |
| `LINKS_RATE_LIMIT_WINDOW_SECONDS` | `60` | Fixed-window length in seconds, shared by the per-IP bucket and every per-key bucket. Also the upper clamp for `Retry-After`. |
| `LINKS_TRUST_FORWARDED_FOR` | `false` | When true the per-IP identity is the leftmost `X-Forwarded-For` entry, otherwise the transport peer address. No influence on keyed callers. |
| `SHORTENER_API_KEYS` | `""` (empty) | Comma-separated `key:quota` pairs, e.g. `alpha:100,beta:20`, giving per-window `POST /api/links` quotas. Read once at startup. Unset or empty means no key is ever recognised. |

### Secrets

This service **does** read one secret from the environment: `SHORTENER_API_KEYS`
contains API key material. (An earlier revision of this README claimed the service
reads no secrets; that is no longer true.) Handling rules, enforced in code:

* API keys are never written to SQLite, never logged, never placed in an exception
  message and never echoed in a response body \* Limiter state is keyed by
  `blake2b(key, key=BOOT_SALT)` where `BOOT_SALT` is 32 random bytes generated once
  per process, so no plaintext key sits in an in-memory map.
* Recognition is a constant-work hash plus a dict lookup, confirmed with
  `hmac.compare_digest`; there is no early-exit comparison loop over configured keys.
* No key material is accepted at runtime through any admin route \enforced entirely
  from the process environment.

## Rate limiting

Limiting runs as pure ASGI middleware in front of routing and body parsing, scoped
to exactly `POST /api/links`, so a throttled request never opens a database
connection and never writes a row. `/health`, `/{code}` and the stats endpoint are
never refused with `429`.

* **Recognised `X-API-Key`** (byte-exact, case-sensitive match against a configured
  key, surrounding whitespace stripped): only that key's bucket is consulted, using
  its configured quota. The per-IP bucket is neither read nor written, and the key's
  counter is shared across every client address.
* **No key, empty/whitespace-only key, or unknown key**: the existing per-IP bucket
  and `LINKS_RATE_LIMIT_MAX` apply, exactly as before. Unknown keys allocate no
  per-key bucket, so random header values cannot grow memory or buy extra allowance.

Refusals return `429` with `{"error": {"code": "rate_limited", ...}}`, an integer
`Retry-After` header (at least 1, at most the window length) and `Cache-Control:
no-store`.

Malformed entries in `SHORTENER_API_KEYS` (`alpha` with no colon, `beta:abc`,
`gamma:0`, `gamma:-5`, `:5`, empty segments) are ignored at startup; well formed
entries in the same string still take effect. Whitespace around keys, quotas and
separators is stripped. If a key appears twice, the last occurrence wins.

Limiter state is in-process and lost on restart.

## URL safety

Target URLs are validated before they are stored, and the stored value is what is
later served in `Location`:

* `http` and `https` only \* every other scheme (`javascript:`, `data:`, `file:`, ...)
  is rejected by allow-list, not by blocking known bad names.
* Embedded credentials (`https://user:pass@host/`) are rejected.
* The host is resolved and every resulting address is rejected if it is loopback,
  private, link-local, multicast, reserved or unspecified. `169.254.169.254` (the
  cloud metadata endpoint) is denied explicitly.
* Hosts that do not resolve are rejected.
* Maximum length 2048 characters; control characters and whitespace are rejected.

Redirect destinations only ever come from the validated, stored row \* never from a
query parameter, header or path segment of the incoming request.

## Running

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

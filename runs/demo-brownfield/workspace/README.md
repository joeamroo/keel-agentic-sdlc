# URL Shortener

Public JSON API that turns a caller supplied public http(s) URL into an unguessable base62 short code, redirects visitors until the link expires and exposes per-link click analytics that contain no visitor IP addresses.

## Quickstart

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Create a short link:

```bash
curl -X POST http://localhost:8000/api/links \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/page?a=1"}'
```

Response:

```json
{
  "code": "Abc1De2",
  "short_url": "http://localhost:8000/Abc1De2",
  "url": "https://example.com/page?a=1",
  "created_at": "2024-01-31T12:00:00.000000Z",
  "expires_at": "2024-02-29T12:00:00.000000Z"
}
```

Follow the short link:

```bash
curl -L http://localhost:8000/Abc1De2
```

Response: HTTP 307 redirect with Location header set to the stored destination.

## Endpoints

### POST /api/links

Create a short link.

Request body:

```json
{
  "url": "https://example.com/page",
  "expires_at": "2024-12-31T23:59:59Z"
}
```

Fields:
- `url` (required, string): public http(s) destination URL. Max length determined by LINKS_MAX_URL_LENGTH. No credentials are allowed in the URL.
- `expires_at` (optional, string): RFC3339 timestamp in the future. If omitted, LINKS_DEFAULT_TTL_DAYS is added to creation time.

Response (201 Created):

```json
{
  "code": "Abc1De2",
  "short_url": "http://localhost:8000/Abc1De2",
  "url": "https://example.com/page",
  "created_at": "2024-01-31T12:00:00.000000Z",
  "expires_at": "2024-12-31T23:59:59.000000Z"
}
```

Status codes:
- 201: Link created.
- 400: unsupported_scheme when scheme is not http or https; invalid_url when URL is malformed, too long, or contains invalid characters; blocked_destination when host is private, loopback, link-local, or unresolvable; invalid_expiry when expires_at is not a valid RFC3339 timestamp or is in the past.
- 422: validation_error when url is missing, empty, not a string, or exceeds ABSOLUTE_MAX_URL_LENGTH; when expires_at is not a string or exceeds MAX_EXPIRES_AT_LENGTH.
- 429: rate_limited when caller has exceeded the per-minute quota (see rate limiting section).
- 503: code_generation_failed when no unique short code could be generated after LINKS_CODE_MAX_ATTEMPTS attempts.

Error response format:

```json
{
  "error": {
    "code": "invalid_url",
    "message": "The url must include a host."
  }
}
```

### GET /api/links/{code}/stats

Return per-link click analytics.

Path parameters:
- `code`: the short code returned at creation.

Query parameters:
- `limit` (optional, integer): number of clicks per page. Defaults to LINKS_STATS_DEFAULT_LIMIT. Must be between 1 and LINKS_STATS_MAX_LIMIT inclusive.
- `offset` (optional, integer): number of clicks to skip. Defaults to 0. Must be >= 0.

Response (200 OK):

```json
{
  "code": "Abc1De2",
  "url": "https://example.com/page",
  "created_at": "2024-01-31T12:00:00.000000Z",
  "expires_at": "2024-12-31T23:59:59.000000Z",
  "total_clicks": 42,
  "clicks": [
    {
      "timestamp": "2024-02-01T12:15:30.123456Z",
      "referrer": "https://news.example.com/",
      "user_agent": "Mozilla/5.0"
    }
  ]
}
```

Status codes:
- 200: Stats retrieved. Returns data for expired links as well.
- 404: not_found when the code does not exist.
- 422: validation_error when limit is out of range or offset is negative.

### GET /health

Liveness probe. Returns 200 with status ok when the database answers a SELECT 1 query, otherwise 503 with status degraded.

Response (200 OK):

```json
{
  "status": "ok"
}
```

Response (503 Service Unavailable):

```json
{
  "status": "degraded"
}
```

### GET /{code}

Redirect to the stored destination.

Path parameters:
- `code`: the short code returned at creation.

Response (307 Temporary Redirect):
- Location header contains the stored destination URL.
- Cache-Control: no-store
- Referrer-Policy: no-referrer
- Records a click if the link exists and has not expired.

Status codes:
- 307: Link exists and has not expired. Redirect succeeds and click is recorded.
- 404: not_found when the code does not exist or is malformed (wrong length or character set).
- 410: link_expired when the link exists but expires_at is in the past.

## Configuration

| Variable | Default | Effect |
| -------- | ------- | ------ |
| LINKS_DB_PATH | ./links.db | Filesystem path to the SQLite database file. Parent directory is created if missing. |
| LINKS_BASE_URL | http://localhost:8000 | Origin used in the short_url field of POST /api/links and GET /api/links/{code}/stats responses. Trailing slashes are stripped. |
| LINKS_DEFAULT_TTL_DAYS | 30 | Default link lifetime in days when expires_at is omitted. Applied at creation time. |
| LINKS_MAX_URL_LENGTH | 2048 | Maximum accepted length in characters of the target URL in request bodies. Absolute hard limit is 8192. |
| LINKS_CODE_LENGTH | 7 | Number of base62 characters in a generated short code. Must be between 4 and 32. |
| LINKS_CODE_MAX_ATTEMPTS | 5 | Bounded number of insert attempts when a generated code collides with the UNIQUE constraint before returning 503 code_generation_failed. Must be between 1 and 100. |
| LINKS_RATE_LIMIT_ENABLED | true | Set to false, 0 or no (case-insensitive) to disable both per-IP and per-key rate limiting. When disabled, no rate limit state is created. |
| LINKS_RATE_LIMIT_MAX | 10 | Per-IP request allowance for POST /api/links inside LINKS_RATE_LIMIT_WINDOW_SECONDS. Also applied to redirects multiplied by REDIRECT_LIMIT_MULTIPLIER (100). Unkeyed, blank-keyed and unknown-keyed creation requests use this budget. Must be between 1 and 1000000. |
| LINKS_RATE_LIMIT_WINDOW_SECONDS | 60 | Width in seconds of the per-IP sliding window for POST /api/links and GET /{code}. Per-API-key window is always fixed at 60 seconds. Must be between 1 and 86400. |
| LINKS_TRUST_FORWARDED_FOR | false | Set to true, 1 or yes (case-insensitive) to use the first X-Forwarded-For header entry as the client address for rate limiting. Otherwise the peer address is used. |
| LINKS_STATS_DEFAULT_LIMIT | 50 | Default value for the limit query parameter of GET /api/links/{code}/stats when omitted. Must be between 1 and 10000. |
| LINKS_STATS_MAX_LIMIT | 500 | Maximum accepted value for the limit query parameter of GET /api/links/{code}/stats. Must be between 1 and 10000. |
| LINKS_DNS_RESOLUTION_ENABLED | true | When true, destination hostnames are resolved and every resulting address is checked against the denylist. When false, only literal IP addresses in the URL are checked. |
| LINKS_LOG_LEVEL | INFO | Root log level. Accepted values: CRITICAL, ERROR, WARNING, INFO, DEBUG, NOTSET. |
| SHORTENER_API_KEYS | (empty string) | Comma-separated NAME:QUOTA pairs declaring recognised API keys and their per-minute POST /api/links quota. Example: "alpha:100,beta:20". Each entry is parsed as: split on comma, strip whitespace around the name and quota, accept only entries with exactly one colon separator, non-empty name after stripping, and quota that parses as an integer >= 1. Entries that do not meet these criteria are silently skipped without raising. On duplicate names the last declaration wins. Parsed once at process configuration time into an immutable mapping. Unknown keys, blank keys and keys that do not match any entry byte-exactly behave identically to omitting the header. |

## Rate Limiting

Rate limiting applies in-process on POST /api/links and GET /{code} when LINKS_RATE_LIMIT_ENABLED is true. No rate limit state is persisted; all buckets are lost on restart.

When a recognised API key is presented via the X-API-Key header on POST /api/links, the request is throttled against that key's per-minute quota declared in SHORTENER_API_KEYS instead of the per-IP budget. Unknown keys, blank keys and unrecognised keys fall through to the per-IP limiter. The key bucket uses a fixed 60 second window independent of LINKS_RATE_LIMIT_WINDOW_SECONDS.

Per-IP creation requests use LINKS_RATE_LIMIT_MAX requests per LINKS_RATE_LIMIT_WINDOW_SECONDS. Redirects (GET /{code}) use LINKS_RATE_LIMIT_MAX times REDIRECT_LIMIT_MULTIPLIER (100) per LINKS_RATE_LIMIT_WINDOW_SECONDS.

When a request is throttled, the response is 429 rate_limited with a Retry-After header indicating the minimum seconds to wait before the next request is accepted.

Client identification uses a per-boot salted SHA-256 hash of the client address. The raw address is never stored, logged or persisted to disk. Rate limit state exists only in process memory.

## Security Posture

Destination URLs are validated before storage:
- Scheme is allow-listed to http and https only.
- Hosts are checked for embedded credentials (username and password in the URL are rejected).
- Literal IPv4 and IPv6 addresses are parsed including non-canonical forms (decimal, octal, hex, and IPv4-mapped/IPv4-compatible/6to4/Teredo variants).
- All addresses (literal or resolved) are checked against a denylist covering loopback (127.0.0.0/8, ::1), private (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fc00::/7), link-local (169.254.0.0/16, fe80::/10), CGNAT (100.64.0.0/10), cloud metadata (169.254.169.254, fd00:ec2::254), multicast, unspecified and reserved ranges.
- When DNS resolution is enabled (LINKS_DNS_RESOLUTION_ENABLED=true), every resulting address is checked against the denylist. When resolution fails or no addresses are returned, the request is rejected.

The service never makes outbound HTTP requests to a destination. Redirect destinations always come from the validated, stored database row.

Click analytics contain no client IP address, no client identifiers and no user identity. The clicks table has no column capable of storing an address.

No secrets are read or stored by the service.

## Known Limits

Rate limiter tracks up to MAX_TRACKED_KEYS (20000) distinct buckets across per-IP creation, per-IP redirect and per-API-key creation namespaces. When this limit is exceeded, least-recently-seen entries are evicted. Headers with unrecognised keys never create rate limit buckets, so arbitrary header values cannot exhaust memory.

The database uses SQLite with WAL journalling enabled. Concurrent writes are serialised; a click insert does not block concurrent redirect reads. One connection per request is opened and closed.

Short code generation uses a cryptographically secure random source. Collisions are retried up to LINKS_CODE_MAX_ATTEMPTS times before returning 503 code_generation_failed.

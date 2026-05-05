# promem-openai-proxy

Cloudflare Worker that lets Promem desktop agents call OpenAI without
shipping a per-user OpenAI key. Verifies a Supabase JWT, then forwards
the request to `api.openai.com` with the centralized OpenAI key
(stored as an encrypted Worker secret).

## Routes

| Method | Path        | Auth          | Behavior                                         |
|--------|-------------|---------------|--------------------------------------------------|
| GET    | `/health`   | none          | `{"ok": true, "service": "promem-openai-proxy"}` |
| ANY    | `/v1/<...>` | Bearer JWT    | Forwarded to `https://api.openai.com/v1/<...>`   |
| ANY    | other       | —             | `404`                                            |

## One-time deploy

```bash
cd Prome/cloudflare-worker
npm install
wrangler login                          # opens browser, saves Cloudflare creds
wrangler secret put OPENAI_API_KEY      # paste the centralized OpenAI key
wrangler deploy                         # outputs the workers.dev URL
```

After deploy, note the URL (something like
`https://promem-openai-proxy.<account>.workers.dev`).

## Smoke test

```bash
WORKER_URL="https://promem-openai-proxy.<account>.workers.dev"

# Health (no auth)
curl "$WORKER_URL/health"
# expect: {"ok":true,"service":"promem-openai-proxy"}

# Bad auth
curl "$WORKER_URL/v1/chat/completions"
# expect: 401 {"error":"missing Authorization: Bearer <Supabase JWT>"}

# Real call (use a fresh Supabase access_token; e.g. from
#   `python3 -m promem_agent.oauth refresh`)
JWT="eyJ..."
curl "$WORKER_URL/v1/chat/completions" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"say hi"}],"max_tokens":5}'
# expect: standard OpenAI completion JSON
```

## Updating the OpenAI key

```bash
wrangler secret put OPENAI_API_KEY      # prompts for new value, replaces old
```

No code change or redeploy needed — Workers re-reads secrets on next request.

## Logs

```bash
wrangler tail                           # live tail of Worker invocations
```

Free tier: 100K requests/day and 10K log entries/day. Plenty for early-access.

## Security notes

- The Supabase anon key is **public** (it's in every Supabase frontend bundle). It does NOT grant access to user data — RLS at the DB layer enforces tenant isolation. The Worker rejects any request without a valid user JWT.
- The OpenAI key is stored as a Cloudflare Worker secret (encrypted at rest, never readable post-set, only injected at request time).
- The Worker forwards the user's `sub` claim back as `X-Promem-Proxy-User` for debugging — don't use this header for trust decisions.

## Updating the Worker code

```bash
wrangler deploy        # builds + uploads
```

Atomic per-deploy. Existing in-flight requests complete on the old version, new requests hit the new version.

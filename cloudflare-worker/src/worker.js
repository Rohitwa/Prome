// promem-openai-proxy — Cloudflare Worker that authenticates Promem agent
// clients (via Supabase JWT) and forwards their /v1/* requests to OpenAI
// using a centralized OpenAI API key stored as a Worker secret. Lets us
// ship the desktop tracker / orchestrator without baking per-user OpenAI
// keys into the install.
//
// Routes:
//   GET  /health         no auth, returns {"ok": true}
//   ANY  /v1/<path>      requires Authorization: Bearer <Supabase JWT>;
//                        forwards to https://api.openai.com/v1/<path> with
//                        the real OPENAI_API_KEY substituted.
//   ANY  other           404
//
// Streaming preserved: upstream.body is a ReadableStream and is piped back
// to the client unchanged, so OpenAI Server-Sent Events (chat completions
// stream=true) work without buffering.

import { jwtVerify, createRemoteJWKSet } from 'jose';

const OPENAI_BASE = 'https://api.openai.com';

// Lazily initialize the JWKS fetcher so we have access to env vars.
let JWKS = null;
function jwks(env) {
  if (!JWKS) {
    JWKS = createRemoteJWKSet(new URL(env.SUPABASE_JWKS_URL));
  }
  return JWKS;
}

const CORS = {
  'Access-Control-Allow-Origin':  '*',
  'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type, OpenAI-Beta, Accept',
  'Access-Control-Max-Age':       '86400',
};

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...CORS },
  });
}

async function verifyJWT(token, env) {
  const { payload } = await jwtVerify(token, jwks(env), {
    audience: 'authenticated',
  });
  return payload;
}

export default {
  async fetch(request, env, ctx) {
    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS });
    }

    const url = new URL(request.url);

    // Health check (no auth required)
    if (url.pathname === '/health') {
      return jsonResponse({ ok: true, service: 'promem-openai-proxy' });
    }

    // Only proxy /v1/* paths to OpenAI; everything else is 404.
    if (!url.pathname.startsWith('/v1/')) {
      return jsonResponse({ error: `unknown path: ${url.pathname}` }, 404);
    }

    // Verify Supabase JWT
    const auth = request.headers.get('Authorization');
    if (!auth || !auth.startsWith('Bearer ')) {
      return jsonResponse({ error: 'missing Authorization: Bearer <Supabase JWT>' }, 401);
    }
    const token = auth.slice(7).trim();

    let userId;
    try {
      const payload = await verifyJWT(token, env);
      userId = payload.sub;
    } catch (e) {
      return jsonResponse({ error: `invalid token: ${e.message}` }, 401);
    }

    if (!env.OPENAI_API_KEY) {
      return jsonResponse({ error: 'OPENAI_API_KEY secret not set on Worker' }, 500);
    }

    // Build upstream request — strip the Supabase Bearer, swap in real OpenAI key.
    const openaiUrl = OPENAI_BASE + url.pathname + url.search;
    const upstreamHeaders = new Headers();
    upstreamHeaders.set('Authorization', `Bearer ${env.OPENAI_API_KEY}`);
    for (const [k, v] of request.headers.entries()) {
      const kl = k.toLowerCase();
      if (kl === 'content-type' || kl === 'openai-beta' || kl === 'accept') {
        upstreamHeaders.set(k, v);
      }
    }

    let upstream;
    try {
      upstream = await fetch(openaiUrl, {
        method:  request.method,
        headers: upstreamHeaders,
        body:    ['POST', 'PUT', 'PATCH'].includes(request.method) ? request.body : undefined,
      });
    } catch (e) {
      return jsonResponse({ error: `upstream fetch failed: ${e.message}` }, 502);
    }

    // Pass response back, preserving streaming. Add CORS + a debug header.
    const responseHeaders = new Headers(upstream.headers);
    for (const [k, v] of Object.entries(CORS)) responseHeaders.set(k, v);
    responseHeaders.set('X-Promem-Proxy-User', userId);

    // Tee the body: one branch streams to the client unchanged, the other is
    // consumed in the background to extract `usage` from the OpenAI response
    // and log it per-user. Tee buffers internally so a slow client doesn't
    // hold up logging and vice versa.
    const [bodyForClient, bodyForLog] = upstream.body
      ? upstream.body.tee()
      : [null, null];

    const meta = {
      type:   'usage',
      user:   userId,
      method: request.method,
      path:   url.pathname,
      status: upstream.status,
    };

    if (bodyForLog && upstream.ok) {
      ctx.waitUntil(logUsage(bodyForLog, upstream.headers, meta));
    } else {
      // Non-2xx or empty body: log status only, no token counts.
      console.log(JSON.stringify(meta));
    }

    return new Response(bodyForClient, {
      status:     upstream.status,
      statusText: upstream.statusText,
      headers:    responseHeaders,
    });
  },
};

// ── Usage extraction ─────────────────────────────────────────────────────
// Reads the upstream body to completion, extracts `usage` from either a
// JSON response (chat/completions, embeddings) or an SSE stream
// (chat/completions stream=true, only when client sets
// stream_options.include_usage=true), and emits one log line per request.
async function logUsage(stream, upstreamHeaders, meta) {
  const ct = (upstreamHeaders.get('content-type') || '').toLowerCase();
  const isSSE = ct.includes('text/event-stream');
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let usage = null;
  let model = null;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      if (isSSE) {
        let idx;
        while ((idx = buf.indexOf('\n')) !== -1) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (!line.startsWith('data: ')) continue;
          const payload = line.slice(6);
          if (payload === '[DONE]') continue;
          try {
            const obj = JSON.parse(payload);
            if (obj.usage) usage = obj.usage;
            if (obj.model && !model) model = obj.model;
          } catch (_) { /* SSE frame not JSON; ignore */ }
        }
      }
    }
    buf += decoder.decode();

    if (!isSSE) {
      try {
        const obj = JSON.parse(buf);
        if (obj.usage) usage = obj.usage;
        if (obj.model) model = obj.model;
      } catch (_) { /* not JSON (likely error); leave usage null */ }
    }
  } catch (e) {
    console.log(JSON.stringify({ ...meta, error: e.message }));
    return;
  }

  console.log(JSON.stringify({
    ...meta,
    model:             model || null,
    prompt_tokens:     usage?.prompt_tokens     ?? null,
    completion_tokens: usage?.completion_tokens ?? null,
    total_tokens:      usage?.total_tokens      ?? null,
  }));
}

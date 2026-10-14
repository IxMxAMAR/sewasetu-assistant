# Sewa Setu, with an assistant

The department's old-age pension portal, plus two MCP servers so that a citizen's
assistant and a block officer's assistant can actually use it.

Nothing about the portal's rules changed. The assistant follows the same
validations the counter does, writes to the same tables, and leaves the same audit
trail — an application filed by the assistant is indistinguishable from one filed
at the counter, except that the audit trail says `assistant:` instead of
`applicant:`.

## Live now

| | |
|---|---|
| Portal | https://dijidwl4ht6is5-8000.proxy.runpod.net |
| Citizen MCP | https://dijidwl4ht6is5-8101.proxy.runpod.net/mcp |
| Officer MCP | https://dijidwl4ht6is5-8102.proxy.runpod.net/mcp |
| Eval dashboard | https://dijidwl4ht6is5-8000.proxy.runpod.net/eval-dashboard |
| Eval results (JSON) | https://dijidwl4ht6is5-8000.proxy.runpod.net/eval-dashboard.json |

Officer sign-in for the portal: **`admin` / `S3wa-S3tu-pilot-2026`**.

Citizens sign in with a mobile number and an OTP; the OTP is delivered to the
local SMS gateway the department already runs, and you can read it back at
`https://dijidwl4ht6is5-8000.proxy.runpod.net/__gateway/api/messages?to=<mobile>`.

## Connecting your agent

The two servers are separate on purpose. They have different audiences, different
tools, and different tokens; a citizen's token cannot be used against the officer
server and vice versa.

```bash
claude mcp add --transport http sewasetu-citizen \
  https://dijidwl4ht6is5-8101.proxy.runpod.net/mcp

claude mcp add --transport http sewasetu-officer \
  https://dijidwl4ht6is5-8102.proxy.runpod.net/mcp
```

Both endpoints answer `401` with a `WWW-Authenticate` header pointing at
`/.well-known/oauth-protected-resource`, so any client that understands MCP's
authorization spec will walk the sign-in on its own: dynamic client registration,
PKCE, the department's own login page, consent, then a token bound to that
server's audience.

If your client does not do that automatically, the flow is the ordinary one:

```
GET  /.well-known/oauth-protected-resource   on the MCP host
GET  /.well-known/oauth-authorization-server on the portal
POST /oauth/register                         -> client_id
GET  /oauth/authorize?response_type=code&client_id=…&redirect_uri=…&scope=…&resource=…&code_challenge=…&code_challenge_method=S256
POST /oauth/login     mobile=<number>        -> OTP sent
POST /oauth/login     otp=<code>             -> consent
POST /oauth/consent   decision=allow         -> 302 back to your redirect_uri with ?code=…&iss=…&state=…
POST /oauth/token     grant_type=authorization_code&code=…&code_verifier=…&resource=…
```

Scopes are `pension.read` and `pension.write` for a citizen, `officer.read` and
`officer.write` for an officer. The token is an RS256 JWT; the portal publishes
its JWKS at `/.well-known/jwks.json`.

## What the assistant may and may not do

**The pilot is two blocks, Sonari and Namti.** The Director asked for a subset,
so that is what this is. It applies to *writes* only:

- Reading is open. Anybody who can sign in can ask about their own applications,
  and an officer can see the whole queue.
- Filing and withdrawing are refused outside Sonari and Namti. The refusal names
  the blocks that are covered and points at the counter, so the assistant can tell
  the truth rather than improvise.
- `check_eligibility` returns `assistant_may_file_here` up front, so the assistant
  knows before the applicant has told it everything.

The block list is one environment variable, `AGENT_PILOT_BLOCKS`. Widening the
pilot is a config change, not a code change.

**Officers decide, the assistant does not.** `record_decision` requires a reason
and records it against the officer's name. The officer server's instructions say
in as many words: never decide a case she has not asked you to decide.

**The MCP servers never touch the database.** They hold no connection string and
import nothing from the portal. Every tool call is an HTTP request to
`/api/v1/...` carrying the *caller's own token*. The portal decides. That means
the audience check, the pilot check, the validation and the audit trail all live
in one place — the place that already had them.

## Repository layout

```
portal/           the department's app, vendored byte for byte
  app.py            untouched
  oauth.py          untouched
  api.py            our machine-facing JSON API, /api/v1
  evals.py          the eval dashboard
  wsgi.py           the entrypoint that mounts all three
mcp/              two MCP servers, one package, two entry points
  sewasetu_mcp/citizen.py   seven tools for a citizen's assistant
  sewasetu_mcp/officer.py   six tools for an officer's assistant
  tests/                    audience separation, offline
evals/            the harness that produced the dashboard
  personas.yaml     nine people we invented
  harness/          sign-in, MCP client, conversation, assertions
deploy/           how the pod builds itself from this repository
docs/             the brief, the Director's note, the design note
```

## Running it

On a Runpod CPU pod, nothing needs to be installed first. The container's start
command clones this repository and runs `deploy/bootstrap.sh`, which provisions
Postgres, three virtualenvs, and supervisor, then starts the portal, both MCP
servers and the SMS gateway. State — the database, uploads, logs — lives under
`/opt/sewasetu/state`, so a restart of the *container* keeps it; only a full
recreate re-provisions from scratch.

Locally, with Docker:

```bash
docker compose up --build
# portal        http://localhost:8000
# citizen MCP   http://localhost:8101/mcp
# officer MCP   http://localhost:8102/mcp
```

The first boot loads the seed data, which is the department's `seed.sql`. It is
not in this repository (it is 35 MB and it is the department's data); the pod
fetches it from the handout at boot. See `seed/README.md`.

## The evals

`/eval-dashboard` is the result of `evals/harness/run.py`: nine personas — six
applicants and three officers — each signing in for real and holding a
conversation through a real MCP client, after which the harness reads the portal's
database to see what actually happened. It asserts on rows, not on vibes: that an
application exists, that it is in the right status, that the audit trail names the
right actor, that the age proof is on disk, that a case the officer was not asked
about is still untouched.

`evals/lessons.md` is what running it changed. That file is the honest part of
this submission.

To run it yourself you need an OpenAI-shaped model endpoint and the database:

```bash
pip install -r evals/requirements.txt
OPENROUTER_API_KEY=... python evals/harness/run.py \
    --base-url    https://<host> \
    --citizen-mcp https://<host>-8101.proxy.runpod.net/mcp \
    --officer-mcp https://<host>-8102.proxy.runpod.net/mcp \
    --admin-password ...
```

`EVAL_LLM_BASE` points it at a different gateway if you have one; `--model` picks
the model.

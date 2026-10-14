# Design note

## The shape of it

```
citizen's assistant ──MCP──> citizen server ──┐
                                              ├──HTTP + caller's own token──> portal /api/v1 ──> Postgres
officer's assistant ──MCP──> officer server ──┘
```

Four pieces, and the important one is the arrow in the middle. **The MCP servers
never touch the database.** They hold no connection string and import nothing from
the portal. Every tool call is an HTTP request to the department's own API,
carrying the caller's own access token, and the portal decides. Audience check,
pilot check, validation, audit trail — all of it stays in the one place that
already had it, and the assistant cannot be a way around any of it.

That is also why there is a machine-facing API at all rather than the servers
calling `app.py`'s functions directly. A tool call and a browser form post go
through the same code path, so "the assistant follows the same rules as the
counter" is a property of the architecture rather than a promise in a README.

## Two servers, not one

The obvious alternative is a single server whose tool list is filtered by the
token's role. We did not do that, for three reasons.

An officer's token and a citizen's token are for different resources, and MCP's
authorization spec has a way to say so: RFC 8707 resource indicators, RFC 9728
protected-resource metadata, and an audience claim the resource server verifies.
Two servers make that the default rather than something to remember. A token
minted for the citizen server is rejected by the officer server because its
audience is wrong — and there is a test for that, offline, in `mcp/tests/`.

The two audiences want different things. A citizen's assistant should not be able
to see a queue even by accident, and an officer's assistant should not be able to
file on somebody's behalf. Separate tool lists make the accidental version
impossible instead of merely forbidden.

And the instructions differ. The citizen server tells the model to read the scheme
from the tool and never from memory, because applicants are old and a confidently
wrong figure costs them a trip. The officer server tells it never to decide a case
she was not asked to decide. Those are different jobs.

## The pilot is on writes only

The Director asked for the assistant to work for a subset of blocks. Reads are
open — a citizen can always ask about her own applications, and an officer can see
the whole queue, because hiding rows from an officer is not a safety feature.

Writes are restricted to **Sonari and Namti**, two of the six blocks, and the
restriction is enforced in the portal, not in the assistant. A refusal is not an
error: it names the blocks that are covered and points at the counter, so the
assistant can tell the applicant something true. `check_eligibility` also returns
`assistant_may_file_here` before anything is attempted, so the conversation can
end early rather than after she has recited her life story.

The list is `AGENT_PILOT_BLOCKS`. Widening the pilot is a config change.

## Small decisions that came from watching it fail

**Two steps to upload an age proof.** `submit_application` originally took the
document inline. A small model holding a form, a base64 image and a tool schema in
one call dropped fields. `upload_age_proof` now returns a short reference and
`submit_application` takes the reference. The second call is small enough to get
right, and it gives the assistant a natural place to stop if the person has no
document to hand.

**A reason is required to decide.** `record_decision` will not accept an approval
or a rejection without one, and the portal writes it against the officer's name in
`audit_log`. The Director's words were "for the reason recorded against her name";
making the reason optional would have made that a suggestion.

**The audit trail says who did it.** When the assistant files, the portal rewrites
the actor from `applicant:<mobile>` to `assistant:<mobile>`. The application is
otherwise identical. We can tell, after the fact, how many applications came
through the assistant and for whom, without a second table.

**The department's `app.py` is untouched.** It is vendored byte for byte from the
handout. `wsgi.py` imports it, registers two blueprints beside it, and hands the
result to gunicorn. If the department ships a change to `app.py`, it drops in.

**Three virtualenvs.** The portal pins Flask 2.2.5; the MCP SDK wants a different
`httpx`. Rather than compromise the portal's pins to satisfy a new component, the
portal keeps its own environment and the servers get theirs.

## What we did not build

- **No identity verification.** The assistant takes the applicant's word for her
  name and date of birth. The portal's own validation (age, block, duplicate,
  document type) is all that runs. A real deployment needs Aadhaar or a
  certificate from the panchayat, and that is a policy decision, not a code one.
- **No automatic decisions.** The officer assistant drafts nothing and decides
  nothing on its own. The deemed-approval rule stays where it was, in the cron job.
- **No new tables for assistant applications.** They are ordinary applications.
- **No vector store, no RAG.** There is one scheme and it has four fields. A tool
  that reads them from the database is more accurate than a retrieval pipeline and
  cannot go stale.

## What the evals changed

`evals/lessons.md` has the detail; the short version is that every one of the
changes above was found by running the evals, not by thinking about them. The
dashboard at `/eval-dashboard` is the raw output: nine personas, the transcript of
each call, and the database row that proves what happened.

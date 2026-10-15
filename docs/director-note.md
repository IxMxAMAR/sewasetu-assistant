# The Director's note

The brief arrived as a three-minute audio note. The full transcript is in
[director-note-transcript.md](director-note-transcript.md); this is what we took
from it, line by line, and where each answer lives.

> *"Make it possible for any AI assistant to help a citizen apply for the pension
> and follow it up."*

Two MCP servers, on open standards, that any assistant can connect to — not a
chatbot we built and control.

> *"The portal remains the system of record."*

The MCP servers hold no database connection. Every tool call is an HTTP request
to the portal's own API carrying the caller's own token, so the portal writes
every row and keeps every rule. See [DESIGN-NOTE.md](../DESIGN-NOTE.md).

> *"My IT team says AI will hallucinate, raise privacy concerns, skip
> validations."*

Three separate answers. Hallucination: the servers' instructions forbid any
figure, age limit or deadline that did not come from a tool, and the evals assert
that the scheme tool was actually called. Privacy: a citizen's token can only ever
reach her own record, and a tool that returns nothing and a tool that returns
"not yours" are deliberately the same answer. Validation: the assistant calls the
same endpoint the web form posts to, so there is no second set of rules to drift.

> *"Let's make the agent only work for a subset of blocks of the portal."*

Sonari and Namti, two of six. Reads stay open; **writes** are pilot-scoped, and the
check is in the portal, not in the assistant. One environment variable widens it.

> *"The assistant must never get anyone a pension the counter or paper form would
> not."*

That is the sentence the whole architecture is built around. It is also the
sentence the evals test: every write assertion is a row read back from the
database, not a claim the assistant made.

> *"An application made from either place must be visible in both."*

There is one table. An application filed by the assistant appears in the officer's
queue, on the citizen's page, and in the same audit trail — with the actor
recorded as `assistant:<mobile>` rather than `applicant:<mobile>`.

> *"Our applicants are old. They share phones. They sit at kiosks. The assistant
> must work for them, not for an engineer in a demo."*

This is why the personas are who they are: a woman on her grandson's phone, a
daughter filing for her mother on her own number, a widow who does not know her
application number, a man of 54 who should be told no. `anjali-das` and
`padumi-saikia` exist only because of that paragraph.

> *"She should see what is waiting for her, look at her case, and approve or
> reject it for the reason recorded against her name."*

The officer server has the queue, the case, and a decision tool that will not
accept an approval or a rejection without a reason. The reason is written to
`audit_log` against the officer's username.

> *"That is a different design."*

It is, and it is why there are two servers rather than one with a role check
inside. Different audience, different tools, different instructions.

> *"I will test it by pointing my own AI assistant at what you build."*

Both endpoints answer `401` with a pointer to their protected-resource metadata,
so a client that speaks MCP's authorization spec signs in on its own. See the
README for the two commands.

> *"Before you build, sit at a counter for an hour in your head. Invent these
> people in detail, test with them, and set up evals."*

`evals/personas.yaml` — nine people, written as people. `/eval-dashboard` is what
came back.

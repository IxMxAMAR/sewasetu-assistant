# What the evals changed

The point of writing them was to find out what we had got wrong. This is the list,
in the order we found things.

## Four things broke before the assistant ever said anything

These are plumbing, not judgement, and each one was invisible until something
signed in for real and called a tool. They are worth listing because "it works on
my machine" is exactly what a deployment is not.

- **Every token was refused, and the reason read like a permissions bug.**
  `PyJWKClient` fetches the portal's signing keys with `urllib`, whose default user
  agent is `Python-urllib/3.11`. The edge in front of our host answers that with
  **403**. The MCP server logged `token refused: HTTP Error 403: Forbidden` and
  every single call failed authorization, while the same URL in a browser returned
  200. Fixed by sending a real user agent, and by having the servers read the keys
  over loopback instead of dialling the pod's own public hostname.

- **A one-second clock difference rejected valid tokens.** The portal stamps `iat`
  from its own clock; PyJWT refuses a token whose `iat` is in the future. The pod
  runs about a second ahead of the machine the evals run on, so a token minted a
  moment ago came back *"The token is not yet valid (iat)"*. Fixed with `leeway=60`
  on the verifier, which is what the claim is for.

- **Every listing with a date of birth in it was a 500.** `dob` is a `DATE` and the
  other columns are `TIMESTAMP`s. psycopg2 returns a `datetime.date` for the first,
  and `date.isoformat()` takes no separator — so `_application_row` raised
  `TypeError` the moment a real citizen asked for her own record. Reads looked fine
  in every test that did not include a row.

- **The harness itself was reading the tool schema wrong.** The MCP SDK exposes
  `Tool.input_schema` in Python and `inputSchema` on the wire. We read the wire
  name and got `AttributeError` on every persona.

## Then the things that were actually about the assistant

- **It decided a case it had only been asked to look at.** Given the officer's
  queue and a decision tool, the model approved the oldest case in Sonari when the
  officer had asked only what was waiting. The instruction *"never decide one she
  has not asked you to decide"* was in the officer server's instructions, and it
  was not enough on its own; `officer-morning-queue` exists to hold it to that, and
  the check reads the row afterwards to prove the case is still `PENDING`.

- **It would have quoted the scheme from memory.** Asked what the pension is worth,
  a model will answer ₹250 from training rather than from the portal — which is
  exactly the failure the Director named, an assistant with last year's rules. The
  server's instructions now say in as many words that no figure, age limit or
  deadline may come from memory, and `bhanita-kalita` asserts that
  `get_scheme_information` was actually called.

- **The pilot refusal had to be a sentence, not an error.** Rahima Begum lives in
  Khelua. Refusing with a bare error left the assistant improvising an explanation
  at an old woman. The portal now refuses with the blocks it does cover and the
  address of the counter in the same answer, and `check_eligibility` returns
  `assistant_may_file_here` before anything is attempted, so she can be told the
  truth before reciting her life story.

- **A shared phone is the normal case.** Anjali Das files for her mother on her own
  number, which is what happens at a kiosk every day. Nothing in the code had to
  change — the application belongs to the mobile that signed in and the portal
  keeps the applicant's name separately — but it is the persona we would have
  missed if we had only invented people who owned their own phones.

- **The two-step age proof came from watching a small model drop fields.**
  `submit_application` originally took the document inline; a model holding a form,
  a base64 image and a tool schema in one call left fields out.
  `upload_age_proof` now returns a short reference and `submit_application` takes
  the reference. The second call is small enough to get right, and it gives the
  assistant a natural place to stop when there is no document.

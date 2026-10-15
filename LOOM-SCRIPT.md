# Loom script

Three minutes, face and screen. Read this once, then talk — do not read it on
camera. Times are a guide; the two places worth slowing down are marked.

**Before you hit record**

- Browser tabs, in order: the portal home, `/eval-dashboard`, and a second portal
  tab already signed in as `admin`.
- Terminal in the repo root, font big enough to read, scrolled clear.
- Citizen MCP and officer MCP already added to Claude Code, *not* yet signed in,
  so the sign-in is visible on camera. `claude mcp list` should show them as
  needing authentication.
- Phone face-down. Notifications off.

---

### 0:00 — 0:20 · Face

> "The Director's note describes a woman of seventy in Sonari who came to the
> counter with her grandson. He'd asked an AI assistant to apply for her pension
> on the bus. It couldn't — and it told them the wrong amount. This is what I
> built so that next time, it can.
>
> The rule I built it around is one sentence from that note: the assistant must
> never get anyone a pension that the counter wouldn't."

### 0:20 — 0:50 · Screen: the portal

Portal home → sign in as a citizen with a mobile number → show the OTP arrive in
the gateway → the application form.

> "This is the department's portal, unchanged. Same login, same OTP, same form,
> same validations. I didn't touch it. Everything I added sits beside it."

Do not linger. This segment is here to prove the portal is the real one.

### 0:50 — 1:25 · Screen: terminal — the citizen assistant

`claude mcp list` → the sign-in opens in the browser → consent → back to the
terminal.

> "Two MCP servers, one for a citizen's assistant and one for an officer's. They're
> separate servers, not one server with a role check inside — different addresses,
> different tokens, different audiences. A citizen's token is refused by the
> officer server because the audience is wrong, and there's an offline test for
> exactly that.
>
> Signing in is the department's own login. My server never sees a password."

**Slow down here.** Then, in the chat:

> "I want to apply for the old age pension."

Let the assistant ask for details, and answer as Rupali would. Let it call
`check_eligibility`, then `upload_age_proof`, then `submit_application`.

> "It's calling the portal's API with her own token, over the same endpoints the
> web form posts to. There is no second set of rules for the assistant to drift
> away from."

### 1:25 — 1:55 · Screen: the portal, as the officer

Reload the officer tab → the applications list → open the new one → the audit
trail.

> "There it is in the portal, with an application number — filed by the assistant
> and completely ordinary. The one thing that differs is the audit trail: it says
> `assistant:` instead of `applicant:`. So the department can always tell how many
> applications came through an assistant, without a second table."

### 1:55 — 2:25 · Screen: terminal — the officer assistant

Second MCP server, officer sign-in → "what's waiting for me in Sonari?" → it
lists the queue → "approve the oldest one, note says verified against the register"
→ show the tool refusing a decision without a reason first, then accepting one.

> "She sees her queue and her cases. She cannot get a decision recorded without a
> reason, and the reason goes into the audit trail against her name — that was
> the Director's phrasing, 'for the reason recorded against her name'. And the
> assistant can't decide anything she didn't ask it to decide; one of my personas
> exists just to check that."

### 2:25 — 2:50 · Screen: `/eval-dashboard`

Scroll once, slowly, through a persona card — the conversation, then the checks.

> "Nine personas: a woman on her grandson's phone, a daughter filing for her
> mother, a widow who doesn't know her application number, a man of fifty-four who
> should be told no, and three officers. Each one signs in for real, has the
> conversation, and then the harness reads the database to see what actually
> happened — the row, the status, the audit entry, whether the age proof is on
> disk, whether a case nobody asked about is still pending.
>
> Running these found four things that were broken before the assistant ever said
> a word — including one where every single token was being refused, and the error
> said '403 Forbidden', which looks like a permissions problem and was actually a
> user-agent filter. The full list is in `evals/lessons.md`."

### 2:50 — 3:00 · Face

> "The pilot covers two blocks, Sonari and Namti, and it's one environment
> variable to widen. Reads are open; only writes are restricted. Both endpoints
> are live and will stay up. Thank you."

---

## If something goes wrong on camera

- **Sign-in hangs.** It is the OTP. Read it from
  `/__gateway/api/messages?to=<mobile>` in another tab and paste it. Do not
  restart the take; just say "the OTP comes through the department's gateway".
- **The model does something odd.** Do not fix it on camera. Say what it did and
  move on — the evals exist precisely because models do odd things.
- **A tool errors.** That is worth showing, not hiding. Say what the portal
  refused and why.

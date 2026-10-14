"""The block officer's assistant: a second MCP server, at a second address, for
the people who decide applications rather than the people who make them.

It is a different server and not a mode of the first one on purpose. A citizen's
assistant must not be able to enumerate the officer's tools, and a citizen's
token must not be usable here even if it were pasted in by hand: the two servers
have different token audiences, so the portal's own signature check refuses it
before any tool runs.

The tools are shaped around what the Director asked for: see what is waiting,
look at the case, and decide it with the reason recorded against the officer's
name.
"""

from mcp.server.mcpserver import MCPServer

from .portal import PortalError
from .runtime import build_server, caller_portal, serve
from .settings import load

INSTRUCTIONS = """
You are assisting a block officer of the Department of Social Welfare who
decides Old Age Pension applications. She has signed in with her departmental
login, and every action you take is recorded against her name.

How to work with her:

- Start with get_queue_summary. She wants to know what is waiting before she
  wants anything else, and the summary also tells her what has passed the
  15-day statutory window and will be approved by the system if nobody acts.
- Before she decides anything, show her the case: get_application gives the
  applicant's details, the bank account, the document on file and the full
  history of what has been done to it.
- record_decision needs a reason. Ask her for it if she has not given one, in
  her words, and pass it as `reason`. It is written into the audit trail beside
  her name and it is the only record of why the decision was made.
- Never decide an application on the applicant's word, and never decide one she
  has not asked you to decide. The decision is hers; you are doing the typing.
- Only a PENDING application can be decided. If a tool says otherwise, tell her
  the current status rather than trying again.
- This assistant covers the assistant pilot blocks only. If she asks about an
  application outside them, tell her to use the departmental portal.
- Do not describe the scheme's rules from memory; get_scheme_information reads
  them from the portal.
"""


def build() -> tuple[MCPServer, object]:
    settings = load(role="officer", default_port=8102)
    server = build_server(
        settings,
        title="Sewa Setu - block officer assistant",
        instructions=INSTRUCTIONS.strip(),
    )
    portal = lambda: caller_portal(settings)          # noqa: E731 - a per-call handle
    _register(server, portal)
    return server, settings


async def _resolve(portal, application_no: str):
    """Find one application by its number. Returns (row, error_result)."""
    wanted = (application_no or "").strip().upper()
    if not wanted:
        return None, {"ok": False, "message": "An application number is needed, for "
                                              "example SSP26123456."}
    listing = await portal.get("/api/v1/officer/applications",
                               {"application_no": wanted, "status": "ANY", "per_page": 1})
    rows = listing.get("applications") or []
    if not rows:
        return None, {"ok": False, "message":
                      "No application %s is in the assistant pilot blocks (%s). It may "
                      "be in another block, or the number may be wrong."
                      % (wanted, ", ".join(listing.get("blocks") or []))}
    return rows[0], None


def _register(server: MCPServer, portal) -> None:

    @server.tool()
    async def get_queue_summary() -> dict:
        """What is waiting for her, at a glance.

        Counts of applications by status and by block, how many are still inside
        the 15-day statutory window, and how many have passed it and will be
        deemed approved by the nightly job if nobody decides. Start here.
        """
        try:
            return {"ok": True, **(await portal().get("/api/v1/officer/summary"))}
        except PortalError as exc:
            return exc.as_result()

    @server.tool()
    async def list_applications(
        status: str = "PENDING",
        block: str | None = None,
        name: str | None = None,
        mobile: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> dict:
        """The queue itself, oldest first, so the longest-waiting case comes up first.

        `status` is PENDING, APPROVED, REJECTED, WITHDRAWN, DEEMED_APPROVED or
        ANY. `block` narrows to one pilot block. `name` searches the applicant's
        name and `mobile` an exact mobile number. Each row carries the
        application number, applicant, block, status and when it was submitted.
        """
        params = {"status": status, "page": page, "per_page": per_page}
        if block:
            params["block"] = block
        if name:
            params["name"] = name
        if mobile:
            params["mobile"] = mobile
        try:
            return {"ok": True, **(await portal().get("/api/v1/officer/applications", params))}
        except PortalError as exc:
            return exc.as_result()

    @server.tool()
    async def list_overdue() -> dict:
        """Applications past the 15-day statutory window, oldest first.

        These are the ones the law will decide for her. If any are waiting, they
        are the first thing she should look at.
        """
        try:
            return {"ok": True, **(await portal().get("/api/v1/officer/overdue"))}
        except PortalError as exc:
            return exc.as_result()

    @server.tool()
    async def get_application(application_no: str) -> dict:
        """One case in full: the applicant, the bank account, the document on
        file, and the history of everything done to it so far.

        Read this out or summarise it for her before she decides. The history
        shows who did what -- SUBMIT, WITHDRAW, APPROVED, REJECTED -- and the
        reason recorded at the time.
        """
        try:
            handle = portal()
            row, problem = await _resolve(handle, application_no)
            if problem:
                return problem
            return {"ok": True, **(await handle.get(
                "/api/v1/officer/applications/%d" % row["id"]))}
        except PortalError as exc:
            return exc.as_result()

    @server.tool()
    async def record_decision(application_no: str, decision: str, reason: str) -> dict:
        """Approve or reject a pending application, with the reason on the record.

        `decision` is "approve" or "reject". `reason` is required and is written
        into the audit trail beside her name -- if she has not given one, ask her
        for it first and pass her words.

        Only a pending application can be decided, and only once. Confirm with
        her before calling this, then tell her the application number and the new
        status exactly as the answer gives them.
        """
        try:
            handle = portal()
            row, problem = await _resolve(handle, application_no)
            if problem:
                return problem
            return {"ok": True, **(await handle.post(
                "/api/v1/officer/applications/%d/decision" % row["id"],
                {"decision": decision, "reason": reason}))}
        except PortalError as exc:
            return exc.as_result()

    @server.tool()
    async def get_scheme_information() -> dict:
        """The scheme rules as the portal holds them: the monthly amount, the
        minimum age, the closing date, the blocks, the required fields and
        documents, and the deemed-approval rule.

        Use this rather than memory when she asks what the scheme requires.
        """
        try:
            return {"ok": True, **(await portal().get("/api/v1/officer/scheme"))}
        except PortalError as exc:
            return exc.as_result()


def main() -> None:
    server, settings = build()
    serve(server, settings)

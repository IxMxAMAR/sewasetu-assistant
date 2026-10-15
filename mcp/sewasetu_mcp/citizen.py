"""The citizen assistant: the MCP server an AI assistant connects to in order to
help one person apply for the Old Age Pension, or follow up an application they
already made.

It publishes nothing about the officer's work. That is not a permission check
inside a shared server -- it is a different server, at a different address, with
a different token audience, and an assistant connected here cannot enumerate or
call anything that is not in this file.
"""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .portal import PortalError
from .runtime import build_server, caller_portal, serve
from .settings import load

# Field descriptions live on the schema as well as in the docstring. A small model
# reads the schema first, and the difference between "dob" and "dob in DD/MM/YYYY"
# is the difference between an application and an error message.
DateOfBirth = Annotated[str, Field(description="Date of birth as DD/MM/YYYY, e.g. 04/02/1960")]
BlockName = Annotated[str, Field(description="One of the block names from get_scheme_information")]
BankAccount = Annotated[str, Field(description="Bank account number, digits only")]
Ifsc = Annotated[str, Field(description="Bank IFSC code, 11 characters, e.g. SBIN0001234")]

INSTRUCTIONS = """
You are helping one person, who is 60 or older, with the Old Age Pension of the
Government of Purvanchal. She may be applying for the first time, or asking what
happened to an application she already made.

How to work with her:

- Somebody has already signed in on the portal, on the mobile number the
  application will be filed against. That person is usually the applicant, but
  she may equally be a son, a daughter, a grandson or a kiosk operator helping
  her -- that is how it works at the counter and it works here too. Ask whose
  pension it is and put *her* name in applicant_name; the mobile number stays the
  caller's. Say once, plainly, that the application will be in the applicant's
  name and the caller's number will be the contact.
- Never ask for an OTP, a password or a bank PIN, and never offer to sign in on
  anybody's behalf.
- Ask for one thing at a time, in her language, in short sentences. Old people
  share phones and sit at kiosks; assume she is reading this on a small screen.
- Never fill in a field she did not tell you. If you do not have her date of
  birth, village, block, bank account number or IFSC code, ask for it. Guessing
  any of these wastes her time: the portal will refuse the application.
- Read get_scheme_information before you describe the scheme. Never state the
  pension amount, the age limit or the closing date from your own memory.
- Call check_eligibility before you file. It costs nothing and it writes
  nothing, and it tells you what is still missing.
- Never tell her an application has been filed unless a tool returned an
  application number. If a tool refused, read her what it said, in her language,
  and tell her what to do next.
- The assistant is running as a pilot in a few blocks only. If she lives
  somewhere else, say so plainly and send her to the portal or the block
  counter, where she can still apply exactly as before.
"""


def build() -> tuple[MCPServer, object]:
    settings = load(role="citizen", default_port=8101)
    server = build_server(
        settings,
        title="Sewa Setu - citizen assistant",
        instructions=INSTRUCTIONS.strip(),
    )
    portal = lambda: caller_portal(settings)          # noqa: E731 - a per-call handle
    _register(server, portal)
    return server, settings


def _register(server: MCPServer, portal) -> None:

    @server.tool()
    async def get_scheme_information() -> dict:
        """The current rules of the Old Age Pension, read live from the portal.

        Use this before describing the scheme to anybody: the monthly amount,
        the minimum age, the closing date, the valid blocks and the list of
        documents are all in the answer. Do not answer any of those questions
        from memory -- an assistant that quotes last year's rules is worse than
        no assistant.
        """
        try:
            return {"ok": True, **(await portal().get("/api/v1/scheme"))}
        except PortalError as exc:
            return exc.as_result()

    @server.tool()
    async def check_eligibility(
        applicant_name: str,
        dob: DateOfBirth,
        village: str,
        block: BlockName,
        bank_account: BankAccount,
        ifsc: Ifsc,
        gender: str | None = None,
        marital_status: str | None = None,
        husband_name: str | None = None,
    ) -> dict:
        """Check whether an application would be accepted, without filing it.

        Writes nothing. Call this once you have her details and before you file.
        `dob` is DD/MM/YYYY. `block` must be one of the blocks named by
        get_scheme_information. The answer lists every problem in plain words;
        read them to her and collect what is missing.

        An age proof is not part of this check -- the portal asks for it at
        filing time. Use upload_age_proof for that.
        """
        try:
            return {"ok": True, **(await portal().post("/api/v1/citizen/eligibility", {
                "applicant_name": applicant_name, "dob": dob, "village": village,
                "block": block, "bank_account": bank_account, "ifsc": ifsc,
                "gender": gender, "marital_status": marital_status,
                "husband_name": husband_name,
            }))}
        except PortalError as exc:
            return exc.as_result()

    @server.tool()
    async def upload_age_proof(filename: str, content_base64: str) -> dict:
        """Hand the portal her age proof and get back a short reference to it.

        Ask her to send a photo or a PDF of her Aadhaar, voter card or birth
        certificate, read the file, and pass its contents base64-encoded here
        together with the file name (for example "aadhaar.jpg"). The portal
        accepts JPG, PNG and PDF up to 5 MB.

        The answer contains `document_ref`. Pass that to submit_application. If
        you have no document, do not invent one and do not call this: tell her
        the application cannot be filed without it and that she can bring it to
        the counter.
        """
        try:
            return {"ok": True, **(await portal().post("/api/v1/citizen/documents", {
                "filename": filename, "content_base64": content_base64,
            }))}
        except PortalError as exc:
            return exc.as_result()

    @server.tool()
    async def submit_application(
        applicant_name: str,
        dob: DateOfBirth,
        village: str,
        block: BlockName,
        bank_account: BankAccount,
        ifsc: Ifsc,
        document_ref: str,
        gender: str | None = None,
        marital_status: str | None = None,
        husband_name: str | None = None,
    ) -> dict:
        """File the application. This is the only tool here that creates anything.

        `dob` is DD/MM/YYYY. `document_ref` comes from upload_age_proof and must
        be the reference for *her* document. Call check_eligibility first.

        Only report success if the answer contains an `application_no`. If it
        does not, nothing was filed: read her the `message` and do not tell her
        she has applied.
        """
        try:
            return {"ok": True, **(await portal().post("/api/v1/citizen/applications", {
                "applicant_name": applicant_name, "dob": dob, "village": village,
                "block": block, "bank_account": bank_account, "ifsc": ifsc,
                "document_ref": document_ref, "gender": gender,
                "marital_status": marital_status, "husband_name": husband_name,
            }))}
        except PortalError as exc:
            return exc.as_result()

    @server.tool()
    async def get_my_applications() -> dict:
        """Every application on her mobile number, newest first.

        Use this to answer "what happened to my application?" and to find the
        application number or id that the other tools need. Each entry carries
        its status: PENDING, APPROVED, REJECTED, WITHDRAWN or DEEMED_APPROVED.
        DEEMED_APPROVED means the 15-day statutory window passed with no
        decision and the law approved it.
        """
        try:
            return {"ok": True, **(await portal().get("/api/v1/citizen/applications"))}
        except PortalError as exc:
            return exc.as_result()

    @server.tool()
    async def get_my_application(application_no: str | None = None) -> dict:
        """One application in full, with the history of what was done to it.

        Pass her application number (it starts with SSP). With no argument this
        answers for her most recent application, which is what she usually
        wants. The history names only "you" and "the department" -- the officer
        who decided is not hers to see.
        """
        try:
            handle = portal()
            listing = await handle.get("/api/v1/citizen/applications")
            rows = listing.get("applications") or []
            if not rows:
                return {"ok": True, "application": None,
                        "message": "There is no application on this mobile number."}
            chosen = None
            if application_no:
                wanted = application_no.strip().upper()
                chosen = next((r for r in rows if r["application_no"].upper() == wanted), None)
                if chosen is None:
                    return {"ok": False, "message":
                            "No application %s belongs to this mobile number. Her "
                            "applications are: %s."
                            % (application_no, ", ".join(r["application_no"] for r in rows))}
            else:
                chosen = rows[0]
            return {"ok": True, **(await handle.get(
                "/api/v1/citizen/applications/%d" % chosen["id"]))}
        except PortalError as exc:
            return exc.as_result()

    @server.tool()
    async def withdraw_application(application_no: str) -> dict:
        """Withdraw a PENDING application so she can apply again.

        Only a pending application can be withdrawn. Use this when something on
        the application is wrong and she wants to file a corrected one -- a
        mobile number may hold only one live application at a time. Confirm with
        her before calling it: this ends the application she has.
        """
        try:
            handle = portal()
            listing = await handle.get("/api/v1/citizen/applications")
            wanted = application_no.strip().upper()
            chosen = next((r for r in (listing.get("applications") or [])
                           if r["application_no"].upper() == wanted), None)
            if chosen is None:
                return {"ok": False, "message":
                        "No application %s belongs to this mobile number." % application_no}
            return {"ok": True, **(await handle.post(
                "/api/v1/citizen/applications/%d/withdraw" % chosen["id"]))}
        except PortalError as exc:
            return exc.as_result()


def main() -> None:
    server, settings = build()
    serve(server, settings)

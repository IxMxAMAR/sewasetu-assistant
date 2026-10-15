# -*- coding: utf-8 -*-
"""The machine-facing door to Sewa Setu.

The two MCP servers in ../mcp hold no scheme rules of their own. They
authenticate the citizen or the officer, then call this module with that
person's own access token and let the portal decide. Every write below goes
through the same functions the web form uses -- validate_application(),
create_application(), write_audit() -- so an application filed by an assistant
is subject to exactly the checks a counter application is, and lands in exactly
the same table, with the same audit trail.

Two things this module deliberately does not do:

  * It never trusts the caller for identity. The subject is read out of the
    signed token, never out of the request body, so an assistant cannot file
    as somebody else's mobile number however it phrases the request.

  * It never writes outside the pilot blocks (PILOT_BLOCKS below). The
    department is running the assistant in two blocks first; an assistant may
    read a citizen her own record anywhere, but it may only *create* or
    *change* something inside the pilot.
"""

import base64
import binascii
import json
import os
import secrets
from datetime import datetime, timedelta

import jwt
import psycopg2
from flask import Blueprint, jsonify, request

import app as portal
import oauth

api = Blueprint("api", __name__, url_prefix="/api/v1")


# ---------------------------------------------------------------------------
# Policy knobs. Both are deployment decisions, not code, so they live in the
# environment and can be changed without a release.
# ---------------------------------------------------------------------------

# The blocks the assistant is allowed to act in. Read-only access to a
# citizen's own record is not restricted: the risk the department raised was an
# assistant *creating* or *changing* the wrong thing, and the portal already
# shows a citizen her own application on any block.
PILOT_BLOCKS = [b.strip() for b in
                os.environ.get("AGENT_PILOT_BLOCKS", "Sonari,Namti").split(",") if b.strip()]

# The audiences the portal will honour on this API: the canonical URL of each
# MCP server. A token minted for anything else is refused here, so a token
# leaked from some other application cannot be replayed against the portal.
MCP_RESOURCES = [u.strip().rstrip("/") for u in
                 os.environ.get("MCP_RESOURCES", "").split(",") if u.strip()]

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
DOCUMENT_TYPES = ("pdf", "jpg", "jpeg", "png")

# The monthly pension amount. Read from the portal's own configuration so that
# the figure an assistant quotes comes from the department, not from the model.
PENSION_AMOUNT = portal.config.getint("pension", "amount_rupees", fallback=250)


def _now():
    return portal.now_ist()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _decode(token):
    """Verify a token this portal issued. Audience is checked separately."""
    kid, priv = oauth.signing_key()
    try:
        return jwt.decode(token, priv.public_key(), algorithms=["RS256"],
                          issuer=oauth.ISSUER, options={"verify_aud": False})
    except jwt.PyJWTError:
        return None


def _audience_ok(claims):
    aud = claims.get("aud")
    if not MCP_RESOURCES:
        # Nothing configured: refuse rather than fall open. A portal that has
        # not been told which assistants may call it should serve none.
        return False
    candidates = aud if isinstance(aud, list) else [aud]
    return any(isinstance(a, str) and a.rstrip("/") in MCP_RESOURCES for a in candidates)


def _unauthorized(reason):
    # The MCP servers turn this into the citizen's next sentence, so it is
    # written to be read aloud, not to be grepped.
    resp = jsonify({"error": "unauthorized", "message": reason})
    resp.status_code = 401
    resp.headers["WWW-Authenticate"] = 'Bearer realm="sewasetu"'
    return resp


def _refused(message, status=403, **extra):
    body = {"error": "refused", "message": message}
    body.update(extra)
    return jsonify(body), status


def _bad_request(message, **extra):
    body = {"error": "invalid_request", "message": message}
    body.update(extra)
    return jsonify(body), 400


def require_role(role):
    """Bind an endpoint to a signed-in citizen or officer.

    The token is the only source of identity here. `sub` is `citizen:<mobile>`
    or `officer:<username>`, which is the same shape the portal writes into its
    own sign-in tokens, so nothing new has to be trusted.
    """
    def decorate(fn):
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return _unauthorized("This action needs the citizen to sign in first.")
            claims = _decode(header[7:].strip())
            if claims is None:
                return _unauthorized("This sign-in is no longer valid. Please sign in again.")
            if not _audience_ok(claims):
                return _unauthorized("This sign-in was not issued for the assistant service.")
            if claims.get("role") != role or claims.get("scope") != role:
                return _refused("This sign-in is not for %s actions." % role, status=403)
            subject = claims.get("sub") or ""
            if not subject.startswith(role + ":"):
                return _refused("This sign-in is not for %s actions." % role, status=403)
            return fn(claims, subject.split(":", 1)[1], *args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorate


def _db():
    return portal.get_db()


# ---------------------------------------------------------------------------
# Scheme facts. The assistant must answer questions about the scheme from here
# and not from its own memory -- the note from the Director is explicit that an
# assistant gave an applicant out-of-date scheme information.
# ---------------------------------------------------------------------------

def _scheme():
    hours = portal.deadline_remaining()
    return {
        "name": "Old Age Pension (Seva Setu)",
        "department": "Department of Social Welfare, Government of Purvanchal",
        "amount_rupees_per_month": PENSION_AMOUNT,
        "min_age": portal.MIN_AGE,
        "sla_days": portal.SLA_DAYS,
        "scheme_deadline": portal.SCHEME_DEADLINE.strftime("%Y-%m-%d %H:%M"),
        "scheme_open": hours is not None,
        "hours_to_deadline": hours,
        "blocks": list(portal.BLOCKS),
        "assistant_blocks": list(PILOT_BLOCKS),
        "required_documents": ["age proof: Aadhaar, voter card, or birth certificate "
                               "(JPG, PNG or PDF, up to 5 MB)"],
        "required_fields": [
            "applicant_name", "dob (DD/MM/YYYY)", "village", "block",
            "bank_account (9-18 digits)", "ifsc (11 characters, e.g. SBIN0003077)",
        ],
        "one_application_per_mobile": "A mobile number may hold only one application "
                                      "that is pending, approved or deemed approved. A "
                                      "pending application can be withdrawn, after which "
                                      "the same number may apply again.",
        "deemed_approval": "If no officer decides within %d days the application is "
                           "approved automatically by the system." % portal.SLA_DAYS,
    }


@api.get("/scheme")
@require_role("citizen")
def scheme_for_citizen(claims, who):
    return jsonify({"scheme": _scheme(), "as": "citizen:%s" % who})


@api.get("/officer/scheme")
@require_role("officer")
def scheme_for_officer(claims, who):
    return jsonify({"scheme": _scheme(), "as": "officer:%s" % who})


# ---------------------------------------------------------------------------
# Reading a citizen's own record
# ---------------------------------------------------------------------------

APPLICATION_COLUMNS = ("id, application_no, applicant_name, mobile, dob, village, block, "
                       "bank_account, ifsc, status, submitted_at, decided_at, decided_by")


def _stamp(value):
    """A date or a timestamp as the string the portal uses everywhere else.

    `dob` is a DATE and the others are TIMESTAMPs, and psycopg2 hands back a
    datetime.date for the first and a datetime.datetime for the rest. Only the
    second takes a separator, so calling isoformat(sep=...) on a date of birth
    raises TypeError -- which is how every listing that included one turned into
    a 500 the first time a real citizen asked for her own record.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return value.isoformat()


def _application_row(row):
    keys = [c.strip() for c in APPLICATION_COLUMNS.split(",")]
    out = dict(zip(keys, row))
    for k in ("dob", "submitted_at", "decided_at"):
        out[k] = _stamp(out.get(k))
    return out


def _citizen_applications(mobile):
    """Every application on one mobile number, newest first.

    Shared by the two routes below rather than one route calling the other: both
    of those are wrapped by require_role, and a wrapped endpoint invoked from
    another one gets its claims passed in twice.
    """
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT %s FROM applications WHERE mobile = %%s "
                "ORDER BY submitted_at DESC" % APPLICATION_COLUMNS, (mobile,))
    rows = [_application_row(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"mobile": mobile, "applications": rows,
            "scheme": {"amount_rupees_per_month": PENSION_AMOUNT}}


@api.get("/citizen/me")
@require_role("citizen")
def citizen_me(claims, mobile):
    return jsonify(_citizen_applications(mobile))


@api.get("/citizen/applications")
@require_role("citizen")
def citizen_applications(claims, mobile):
    return jsonify(_citizen_applications(mobile))


@api.get("/citizen/applications/<int:app_id>")
@require_role("citizen")
def citizen_application(claims, mobile, app_id):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT %s FROM applications WHERE id = %%s AND mobile = %%s"
                % APPLICATION_COLUMNS, (app_id, mobile))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        # Deliberately the same answer as "does not exist": an assistant must
        # not be able to use this endpoint to discover other people's ids.
        return _refused("No application with that number belongs to this mobile number.",
                        status=404)
    out = _application_row(row)
    cur.execute("SELECT at, action, actor, note FROM audit_log WHERE application_id = %s "
                "ORDER BY at ASC", (app_id,))
    out["history"] = [
        {"at": a.isoformat(sep=" ", timespec="seconds"),
         "action": action,
         # The audit trail records who did what. A citizen is told the actions
         # on her own file but not the officer's username.
         "by": "you" if actor.startswith("applicant:") else "the department",
         "note": note or ""}
        for (a, action, actor, note) in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return jsonify({"application": out})


# ---------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------

DRAFT_FIELDS = ("applicant_name", "dob", "village", "block", "gender", "marital_status",
                "husband_name", "bank_account", "ifsc")


def _draft_from_body():
    body = request.get_json(silent=True) or {}
    # Accept the draft either nested under "applicant" or flat, because a small
    # model will produce one or the other and neither is wrong.
    nested = body.get("applicant") if isinstance(body.get("applicant"), dict) else {}
    draft = {}
    for field in DRAFT_FIELDS:
        value = nested.get(field, body.get(field))
        if value is not None:
            draft[field] = str(value)
    return body, draft


@api.post("/citizen/eligibility")
@require_role("citizen")
def citizen_eligibility(claims, mobile):
    """Answer 'would this application be accepted?' without writing anything.

    This exists so an assistant can tell a citizen what is missing before she
    has committed to anything, instead of discovering it at submission time and
    leaving her with a half-finished conversation.
    """
    body, draft = _draft_from_body()
    block = (draft.get("block") or "").strip()
    errors, cleaned = portal.validate_application(draft, "placeholder-age-proof")

    pilot_ok = block in PILOT_BLOCKS
    if block and not pilot_ok:
        errors = [e for e in errors if not e.startswith("block must be")]
        errors.append(
            "the assistant can only file applications for %s at present; for %s "
            "please use the portal at %s/apply or the block counter"
            % (", ".join(PILOT_BLOCKS), block or "this block", oauth.ISSUER))

    conn = _db()
    cur = conn.cursor()
    existing = portal.active_application(cur, mobile)
    cur.close()
    conn.close()
    if existing:
        errors.append("this mobile number already has application %s (%s); a pending "
                      "application can be withdrawn and then applied for again"
                      % (existing[1], existing[2]))

    return jsonify({
        "eligible": not errors,
        "problems": errors,
        "assistant_may_file_here": pilot_ok,
        "assistant_blocks": list(PILOT_BLOCKS),
        "already_applied": bool(existing),
        "existing_application": ({"id": existing[0], "application_no": existing[1],
                                  "status": existing[2]} if existing else None),
        "normalised": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                       for k, v in cleaned.items() if k != "doc_path"},
    })


def _store_document(mobile, filename, content):
    name = (filename or "").strip().lower()
    if "." not in name or name.rsplit(".", 1)[-1] not in DOCUMENT_TYPES:
        return None, "the age proof must be a JPG, PNG or PDF file"
    if len(content) > MAX_DOCUMENT_BYTES:
        return None, "the age proof is larger than 5 MB"
    os.makedirs(portal.UPLOAD_DIR, exist_ok=True)
    path = os.path.join(portal.UPLOAD_DIR, "%s_%s" % (mobile, name))
    with open(path, "wb") as out:
        out.write(content)
    return path, None


@api.post("/citizen/documents")
@require_role("citizen")
def citizen_document(claims, mobile):
    """Receive an age proof and hand back a reference to it.

    Two steps rather than one so that the assistant never has to hold a
    document and a half-filled form in the same call: upload, get a short
    reference, then file with that reference.
    """
    body = request.get_json(silent=True) or {}
    raw = body.get("content_base64") or ""
    try:
        content = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return _bad_request("the document was not valid base64")
    path, problem = _store_document(mobile, body.get("filename"), content)
    if problem:
        return _bad_request(problem)

    ref = secrets.token_urlsafe(18)
    conn = _db()
    cur = conn.cursor()
    cur.execute("INSERT INTO agent_documents (ref, mobile, filename, path, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (ref, mobile, os.path.basename(path), path, _now()))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"document_ref": ref, "filename": os.path.basename(path),
                    "bytes": len(content)}), 201


def _resolve_document(mobile, body):
    """Turn whatever the caller sent into a path on disk, or a problem."""
    ref = (body.get("document_ref") or "").strip()
    if ref:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT mobile, path FROM agent_documents WHERE ref = %s", (ref,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None, "that document reference is not known to the portal"
        if row[0] != mobile:
            return None, "that document belongs to a different mobile number"
        if not os.path.exists(row[1]):
            return None, "that document is no longer on file; please send it again"
        return row[1], None

    raw = body.get("content_base64")
    if raw:
        try:
            content = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return None, "the document was not valid base64"
        return _store_document(mobile, body.get("document_filename"), content)

    return None, ("an age proof is required: send the document first with "
                  "upload_age_proof, then file with the reference it returns")


@api.post("/citizen/applications")
@require_role("citizen")
def citizen_create_application(claims, mobile):
    body, draft = _draft_from_body()
    block = (draft.get("block") or "").strip()

    if block and block not in PILOT_BLOCKS:
        return _refused(
            "The assistant is running as a pilot in %s only. For %s, please apply "
            "through the portal or at the block counter."
            % (", ".join(PILOT_BLOCKS), block),
            assistant_blocks=list(PILOT_BLOCKS))

    doc_path, problem = _resolve_document(mobile, body)
    if problem:
        return _bad_request(problem)

    errors, cleaned = portal.validate_application(draft, doc_path)
    if errors:
        return _bad_request("This application cannot be accepted yet: "
                            + "; ".join(errors) + ".", problems=errors)

    conn = _db()
    cur = conn.cursor()
    try:
        existing = portal.active_application(cur, mobile)
        if existing:
            conn.rollback()
            return _refused(
                "This mobile number already has application %s (%s). A pending "
                "application can be withdrawn from the status portal, after which "
                "you may apply again." % (existing[1], existing[2]),
                existing_application={"id": existing[0], "application_no": existing[1],
                                      "status": existing[2]})
        new_id, app_no = portal.create_application(cur, mobile, cleaned)
        # The audit trail says which door the application came through. The
        # portal's own form writes "applicant:<mobile>"; an assistant writes
        # "assistant:<mobile>" so the department can tell the channels apart
        # without the two ever diverging in any other way.
        cur.execute("UPDATE audit_log SET actor = %s WHERE application_id = %s "
                    "AND action = 'SUBMIT' AND actor = %s",
                    ("assistant:%s" % mobile, new_id, "applicant:%s" % mobile))
        conn.commit()
    except psycopg2.Error as exc:
        conn.rollback()
        return jsonify({"error": "portal_error",
                        "message": "the portal could not record the application: %s" % exc}), 500
    finally:
        cur.close()
        conn.close()

    portal.send_sms(mobile, "Sewa Setu: application %s received. Track at the status "
                            "portal with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return jsonify({"id": new_id, "application_no": app_no, "status": "PENDING",
                    "submitted_at": _now().isoformat(sep=" ", timespec="seconds"),
                    "block": cleaned["block"]}), 201


@api.post("/citizen/applications/<int:app_id>/withdraw")
@require_role("citizen")
def citizen_withdraw(claims, mobile, app_id):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT block, status FROM applications WHERE id = %s AND mobile = %s",
                (app_id, mobile))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return _refused("No application with that number belongs to this mobile number.",
                        status=404)
    if row[0] not in PILOT_BLOCKS:
        cur.close()
        conn.close()
        return _refused("The assistant can only manage applications in %s. This one is "
                        "in %s; please use the status portal."
                        % (", ".join(PILOT_BLOCKS), row[0]))
    cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, "
                "decided_by = 'APPLICANT' WHERE id = %s AND mobile = %s "
                "AND status = 'PENDING' RETURNING application_no",
                (datetime.now(), app_id, mobile))
    done = cur.fetchone()
    if done:
        portal.write_audit(cur, app_id, "WITHDRAW", "assistant:%s" % mobile)
        conn.commit()
    cur.close()
    conn.close()
    if not done:
        return _refused("Only a pending application can be withdrawn; this one is %s."
                        % row[1].lower())
    return jsonify({"id": app_id, "application_no": done[0], "status": "WITHDRAWN"})


# ---------------------------------------------------------------------------
# The block officer
# ---------------------------------------------------------------------------

OFFICER_COLUMNS = ("id, application_no, applicant_name, mobile, dob, gender, marital_status, "
                   "village, block, bank_account, ifsc, doc_path, status, submitted_at, "
                   "decided_at, decided_by")


def _officer_row(row, with_doc=False):
    keys = [c.strip() for c in OFFICER_COLUMNS.split(",")]
    out = dict(zip(keys, row))
    for k in ("dob", "submitted_at", "decided_at"):
        out[k] = _stamp(out.get(k))
    if not with_doc:
        out.pop("doc_path", None)
    return out


@api.get("/officer/me")
@require_role("officer")
def officer_me(claims, username):
    return jsonify({"officer": username, "blocks": list(PILOT_BLOCKS),
                    "note": "This assistant covers the assistant pilot blocks only."})


@api.get("/officer/summary")
@require_role("officer")
def officer_summary(claims, username):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications WHERE block = ANY(%s) "
                "GROUP BY status ORDER BY count(*) DESC", (PILOT_BLOCKS,))
    by_status = {s: n for s, n in cur.fetchall()}
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "AND block = ANY(%s) GROUP BY block ORDER BY count(*) DESC", (PILOT_BLOCKS,))
    by_block = {b: n for b, n in cur.fetchall()}
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND block = ANY(%s) AND submitted_at < %s",
                (PILOT_BLOCKS, datetime.now() - timedelta(days=portal.SLA_DAYS)))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND block = ANY(%s) AND submitted_at >= %s",
                (PILOT_BLOCKS, datetime.now() - timedelta(days=portal.SLA_DAYS)))
    within_sla = cur.fetchone()[0]
    cur.close()
    conn.close()
    return jsonify({"blocks": list(PILOT_BLOCKS), "by_status": by_status,
                    "pending_by_block": by_block, "pending_total": by_status.get("PENDING", 0),
                    "pending_within_sla": within_sla, "pending_overdue": overdue,
                    "sla_days": portal.SLA_DAYS})


@api.get("/officer/applications")
@require_role("officer")
def officer_applications(claims, username):
    args = request.args
    status = (args.get("status") or "PENDING").strip().upper()
    block = (args.get("block") or "").strip()
    mobile = (args.get("mobile") or "").strip()
    name = (args.get("name") or "").strip()
    application_no = (args.get("application_no") or "").strip().upper()
    try:
        page = max(1, int(args.get("page") or 1))
        per_page = min(50, max(1, int(args.get("per_page") or 20)))
    except ValueError:
        return _bad_request("page and per_page must be whole numbers")

    if status not in ("PENDING", "APPROVED", "REJECTED", "WITHDRAWN", "DEEMED_APPROVED", "ANY"):
        return _bad_request("status must be one of PENDING, APPROVED, REJECTED, "
                            "WITHDRAWN, DEEMED_APPROVED, ANY")
    if block and block not in PILOT_BLOCKS:
        return _refused("This assistant covers %s only." % ", ".join(PILOT_BLOCKS),
                        assistant_blocks=list(PILOT_BLOCKS))

    where = ["block = ANY(%s)"]
    params = [PILOT_BLOCKS]
    if status != "ANY":
        where.append("status = %s")
        params.append(status)
    if block:
        where.append("block = %s")
        params.append(block)
    if mobile:
        where.append("mobile = %s")
        params.append(mobile)
    if name:
        where.append("applicant_name ILIKE %s")
        params.append("%" + name + "%")
    if application_no:
        where.append("application_no = %s")
        params.append(application_no)
    clause = " AND ".join(where)

    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM applications WHERE " + clause, params)
    total = cur.fetchone()[0]
    cur.execute("SELECT %s FROM applications WHERE %s "
                "ORDER BY submitted_at ASC LIMIT %%s OFFSET %%s" % (OFFICER_COLUMNS, clause),
                params + [per_page, (page - 1) * per_page])
    rows = [_officer_row(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify({"applications": rows, "total": total, "page": page,
                    "per_page": per_page, "status": status,
                    "blocks": list(PILOT_BLOCKS)})


@api.get("/officer/applications/<int:app_id>")
@require_role("officer")
def officer_application(claims, username, app_id):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT %s FROM applications WHERE id = %%s" % OFFICER_COLUMNS, (app_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return _refused("No application with id %d." % app_id, status=404)
    out = _officer_row(row, with_doc=True)
    if out["block"] not in PILOT_BLOCKS:
        cur.close()
        conn.close()
        return _refused("Application %s is in %s, outside the assistant pilot (%s). "
                        "Use the departmental portal." % (out["application_no"], out["block"],
                                                          ", ".join(PILOT_BLOCKS)))
    cur.execute("SELECT at, action, actor, note FROM audit_log WHERE application_id = %s "
                "ORDER BY at ASC", (app_id,))
    out["history"] = [
        {"at": a.isoformat(sep=" ", timespec="seconds"), "action": action,
         "actor": actor, "note": note or ""}
        for (a, action, actor, note) in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return jsonify({"application": out})


@api.post("/officer/applications/<int:app_id>/decision")
@require_role("officer")
def officer_decision(claims, username, app_id):
    """Approve or reject, with the reason recorded against the officer's name.

    The reason is required. The portal's own button treats the note as
    optional; the Director asked for the decision to be recorded with its
    reason, so through this door it always is.
    """
    body = request.get_json(silent=True) or {}
    decision = (body.get("decision") or "").strip().lower()
    reason = (body.get("reason") or "").strip()
    if decision not in ("approve", "reject"):
        return _bad_request("decision must be 'approve' or 'reject'")
    if not reason:
        return _bad_request("a reason is required for the record; ask the officer why "
                            "and send it as 'reason'")
    if len(reason) > 500:
        reason = reason[:500]

    new_status = "APPROVED" if decision == "approve" else "REJECTED"
    actor = username
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT block, status, application_no FROM applications WHERE id = %s",
                (app_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return _refused("No application with id %d." % app_id, status=404)
    block, current, app_no = row
    if block not in PILOT_BLOCKS:
        cur.close()
        conn.close()
        return _refused("Application %s is in %s, outside the assistant pilot (%s)."
                        % (app_no, block, ", ".join(PILOT_BLOCKS)))
    cur.execute("UPDATE applications SET status = %s, decided_at = %s, decided_by = %s "
                "WHERE id = %s AND status = 'PENDING' RETURNING application_no",
                (new_status, datetime.now(), actor, app_id))
    updated = cur.fetchone()
    if updated:
        portal.write_audit(cur, app_id, new_status, "officer:%s" % actor, reason)
        conn.commit()
    cur.close()
    conn.close()
    if not updated:
        return _refused("Application %s is %s, and only a pending application can be "
                        "decided." % (app_no, current.lower()))
    return jsonify({"id": app_id, "application_no": app_no, "status": new_status,
                    "decided_by": actor, "reason": reason,
                    "decided_at": _now().isoformat(sep=" ", timespec="seconds")})


@api.get("/officer/overdue")
@require_role("officer")
def officer_overdue(claims, username):
    """Applications that the 15-day clock is about to decide for the officer."""
    conn = _db()
    cur = conn.cursor()
    cutoff = datetime.now() - timedelta(days=portal.SLA_DAYS)
    cur.execute("SELECT %s FROM applications WHERE status = 'PENDING' AND block = ANY(%%s) "
                "AND submitted_at < %%s ORDER BY submitted_at ASC LIMIT 50"
                % OFFICER_COLUMNS, (PILOT_BLOCKS, cutoff))
    rows = [_officer_row(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify({"overdue": rows, "total": len(rows), "sla_days": portal.SLA_DAYS,
                    "note": "These have passed the statutory %d-day window and will be "
                            "deemed approved by the nightly job if nobody decides."
                            % portal.SLA_DAYS})

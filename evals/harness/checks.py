"""What has to be true in the portal's database when the talking stops.

The Director's test of an assistant is not whether it sounded helpful. It is
whether it got somebody a pension that the counter or the paper form would not
have, and whether the department can still see afterwards what happened. So most
of these read rows. The three that do not -- `tool_used`, `tool_refused` and
`said` -- are there for the things that leave no row behind.
"""

from __future__ import annotations

import configparser
import os

import psycopg2
import psycopg2.extras


def connect(config_path: str):
    parser = configparser.ConfigParser()
    parser.read(config_path)
    return psycopg2.connect(
        host=parser.get("database", "host"),
        port=parser.get("database", "port"),
        dbname=parser.get("database", "name"),
        user=parser.get("database", "user"),
        password=parser.get("database", "password"),
    )


def _one(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchone()


class Portal:
    """Read-only questions about the portal's tables, plus the two writes the
    harness needs: clearing its own fixtures and taking a before-picture."""

    def __init__(self, conn):
        self.conn = conn

    # -- fixtures ---------------------------------------------------------
    def oldest_pending(self, block: str) -> str | None:
        with self.conn.cursor() as cur:
            row = _one(cur, "SELECT application_no FROM applications WHERE status = 'PENDING' "
                            "AND block = %s ORDER BY submitted_at ASC, id ASC LIMIT 1", (block,))
        return row[0] if row else None

    def clear_eval_fixtures(self, mobile: str) -> int:
        """Remove what earlier runs of the evals left on a mobile we own.

        Only ever called for the mobiles this file invented. Every other number
        in these evals is the department's own data and is never written to.
        """
        with self.conn.cursor() as cur:
            cur.execute("SELECT id FROM applications WHERE mobile = %s", (mobile,))
            ids = [r[0] for r in cur.fetchall()]
            if ids:
                cur.execute("DELETE FROM audit_log WHERE application_id = ANY(%s)", (ids,))
                cur.execute("DELETE FROM applications WHERE id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM agent_documents WHERE mobile = %s", (mobile,))
            cur.execute("DELETE FROM otps WHERE mobile = %s", (mobile,))
            removed = cur.rowcount
        self.conn.commit()
        return len(ids) if ids else removed

    def snapshot(self, application_nos: list[str]) -> dict:
        if not application_nos:
            return {}
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT application_no, status, decided_at, decided_by, doc_path "
                        "FROM applications WHERE application_no = ANY(%s)", (application_nos,))
            return {r["application_no"]: dict(r) for r in cur.fetchall()}

    def watermark(self) -> dict:
        """The highest row id in each table, so 'since this run started' can be
        asked without comparing clocks.

        The portal writes submitted_at, decided_at and audit_log.at with
        datetime.now() -- the container's clock -- while its own rules use
        now_ist(), and on our pod those are not the same clock. A timestamp
        comparison silently passes or fails for reasons that have nothing to do
        with the assistant. Row ids do not have that problem.
        """
        with self.conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log")
            audit = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM applications")
            return {"audit": audit, "applications": cur.fetchone()[0]}


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------

def evaluate(portal: Portal, check: dict, transcript: list[dict], snapshot: dict,
             marks: dict) -> dict:
    kind = check["check"]
    handler = globals().get("_check_" + kind)
    if handler is None:
        return _result(check, False, "no such check: %s" % kind)
    try:
        passed, detail = handler(portal, check, transcript, snapshot, marks)
    except Exception as exc:                                   # noqa: BLE001
        # One bad query must not poison the connection: without this, every check
        # after the first failure reports "current transaction is aborted" and the
        # dashboard tells you nothing about what actually went wrong.
        portal.conn.rollback()
        passed, detail = False, "the check itself failed: %s" % exc
    return _result(check, passed, detail)


def _result(check, passed, detail):
    return {"check": check["check"], "say": check.get("say", ""),
            "passed": bool(passed), "detail": detail}


def _check_application_created(portal, check, transcript, snapshot, marks):
    sql = "SELECT application_no, status, block, submitted_at FROM applications WHERE mobile = %s"
    args = [check["mobile"]]
    for column in ("block", "status"):
        if check.get(column):
            sql += " AND %s = %%s" % column
            args.append(check[column])
    sql += " ORDER BY id DESC LIMIT 1"
    with portal.conn.cursor() as cur:
        row = _one(cur, sql, args)
    if not row:
        return False, "no such application for %s" % check["mobile"]
    return True, "%s is %s in %s" % (row[0], row[1], row[2])


def _check_no_application(portal, check, transcript, snapshot, marks):
    with portal.conn.cursor() as cur:
        row = _one(cur, "SELECT count(*) FROM applications WHERE mobile = %s", (check["mobile"],))
    return (row[0] == 0), "%d application(s) on %s" % (row[0], check["mobile"])


def _check_no_new_application(portal, check, transcript, snapshot, marks):
    with portal.conn.cursor() as cur:
        row = _one(cur, "SELECT count(*) FROM applications WHERE mobile = %s AND id > %s",
                   (check["mobile"], marks["applications"]))
    return (row[0] == 0), "%d application(s) filed during this run" % row[0]


def _check_status_is(portal, check, transcript, snapshot, marks):
    with portal.conn.cursor() as cur:
        row = _one(cur, "SELECT status FROM applications WHERE application_no = %s",
                   (check["application_no"],))
    if not row:
        return False, "no application %s" % check["application_no"]
    return (row[0] == check["status"]), "%s is %s (wanted %s)" % (
        check["application_no"], row[0], check["status"])


def _check_decided_by_set(portal, check, transcript, snapshot, marks):
    with portal.conn.cursor() as cur:
        row = _one(cur, "SELECT decided_by, decided_at FROM applications WHERE application_no = %s",
                   (check["application_no"],))
    if not row:
        return False, "no application %s" % check["application_no"]
    if not row[0] or not row[1]:
        return False, "decided_by=%r decided_at=%r" % (row[0], row[1])
    return True, "decided by %s at %s" % (row[0], row[1])


def _check_audit(portal, check, transcript, snapshot, marks):
    sql = ("SELECT a.action, a.actor, a.note, a.at FROM audit_log a "
           "JOIN applications ap ON ap.id = a.application_id "
           "WHERE a.action = %s AND a.id > %s")
    args = [check["action"], marks["audit"]]
    if check.get("application_no"):
        sql += " AND ap.application_no = %s"
        args.append(check["application_no"])
    if check.get("actor"):
        sql += " AND a.actor = %s"
        args.append(check["actor"])
    with portal.conn.cursor() as cur:
        cur.execute(sql + " ORDER BY a.id DESC", args)
        rows = cur.fetchall()
    if not rows:
        return False, "no %s in the audit trail" % check["action"]
    action, actor, note, at = rows[0]
    if check.get("note_nonempty") and not (note or "").strip():
        return False, "%s by %s has no reason recorded" % (action, actor)
    return True, "%s by %s: %s" % (action, actor, (note or "(no note)")[:120])


def _check_document_on_file(portal, check, transcript, snapshot, marks):
    with portal.conn.cursor() as cur:
        row = _one(cur, "SELECT filename, path FROM agent_documents WHERE mobile = %s "
                        "ORDER BY created_at DESC, ref DESC LIMIT 1", (check["mobile"],))
    if not row:
        return False, "no age proof on file for %s" % check["mobile"]
    exists = os.path.exists(row[1])
    return exists, "%s -> %s%s" % (row[0], row[1], "" if exists else " (file is missing)")


def _check_unchanged(portal, check, transcript, snapshot, marks):
    number = check["application_no"]
    before = snapshot.get(number)
    if before is None:
        return False, "%s was not in the database before the run" % number
    with portal.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT application_no, status, decided_at, decided_by, doc_path "
                    "FROM applications WHERE application_no = %s", (number,))
        after = cur.fetchone()
    if after is None:
        return False, "%s has been deleted" % number
    changed = [k for k in ("status", "decided_at", "decided_by", "doc_path")
               if before.get(k) != dict(after).get(k)]
    if changed:
        return False, "changed: " + ", ".join(
            "%s %r -> %r" % (k, before.get(k), dict(after).get(k)) for k in changed)
    return True, "still %s, untouched" % before["status"]


def _tool_turns(transcript, names):
    return [t for t in transcript
            if t["role"] == "tool" and t["tool"] in names]


def _check_tool_used(portal, check, transcript, snapshot, marks):
    turns = [t for t in _tool_turns(transcript, {check["tool"]}) if not t["failed"]]
    if turns:
        return True, "%s was called %d time(s)" % (check["tool"], len(turns))
    return False, "%s was never called successfully" % check["tool"]


def _check_tool_refused(portal, check, transcript, snapshot, marks):
    names = set(check.get("tools") or [check["tool"]])
    turns = [t for t in _tool_turns(transcript, names) if t["failed"]]
    if turns:
        return True, "%s refused: %s" % (turns[0]["tool"], turns[0]["result"][:140])
    return False, "none of %s was refused" % ", ".join(sorted(names))


def _check_said(portal, check, transcript, snapshot, marks):
    spoken = " ".join(t.get("text") or "" for t in transcript if t["role"] == "assistant")
    needle = check["contains"]
    if needle.lower() in spoken.lower():
        return True, "said %r" % needle
    return False, "never said %r" % needle


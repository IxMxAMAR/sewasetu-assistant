#!/usr/bin/env python3
"""Run the evals against a live deployment.

    python evals/harness/run.py \
        --base-url  https://<host> \
        --citizen-mcp https://<host>-8101.proxy.runpod.net/mcp \
        --officer-mcp https://<host>-8102.proxy.runpod.net/mcp \
        --out evals/results/latest.json

Each persona signs in for real, holds a conversation through a real MCP client,
and is then checked against the portal's database. The result is one JSON file,
which is what /eval-dashboard renders.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import DEFAULT_MODEL, Model, conversation           # noqa: E402
from checks import Portal, connect, evaluate                   # noqa: E402
from mcpclient import connect as mcp_connect                   # noqa: E402
from signin import Portal as SignIn, SignInError               # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> dt.datetime:
    """The portal stamps its own rows in IST regardless of where the host is, so
    'since this run started' has to be in the same clock."""
    return dt.datetime.now(IST).replace(tzinfo=None)


def wait_for(url: str, seconds: float = 180.0, what: str = "service") -> None:
    import requests
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code < 500:
                print("  %s is up (%s)" % (what, resp.status_code))
                return
            last = "HTTP %s" % resp.status_code
        except Exception as exc:                               # noqa: BLE001
            last = str(exc)
        time.sleep(3)
    raise SystemExit("%s at %s never came up (%s)" % (what, url, last))


def load_personas(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def resolve_targets(personas: list[dict], portal: Portal) -> None:
    """Fill in {application_no} for the personas that decide a real pending case."""
    for persona in personas:
        target = persona.get("target")
        if not target:
            continue
        if target["from"] != "oldest_pending":
            raise SystemExit("unknown target source %r" % target["from"])
        number = portal.oldest_pending(target["block"])
        if not number:
            raise SystemExit("no pending application in %s to give the officer" % target["block"])
        blob = json.dumps(persona).replace("{application_no}", number)
        persona.clear()
        persona.update(json.loads(blob))
        print("  %s -> %s" % (persona["id"], number))


def describe(exc: BaseException) -> str:
    """The useful part of an exception.

    anyio wraps everything the MCP client raises in an ExceptionGroup, so the
    message on its own is "unhandled errors in a TaskGroup" and tells you nothing.
    The leaf is what actually went wrong.
    """
    def leaves(error):
        children = getattr(error, "exceptions", None)
        if children:
            for child in children:
                yield from leaves(child)
        else:
            yield error

    parts = ["%s: %s" % (type(leaf).__name__, leaf) for leaf in leaves(exc)]
    return " | ".join(parts) if parts else repr(exc)


async def run_persona(persona: dict, args, signin: SignIn, portal: Portal,
                      snapshot: dict, marks: dict, assistant: Model,
                      citizen: Model, log=print) -> dict:
    record = {
        "id": persona["id"], "who": persona["who"], "role": persona["role"],
        "age": persona.get("age"), "village": persona.get("village"),
        "block": persona.get("block"), "mobile": persona.get("mobile"),
        "voice": persona["voice"].strip(), "goal": persona["goal"].strip(),
        "opening": persona["opening"], "transcript": [], "checks": [], "error": None,
    }
    is_citizen = persona["role"] == "citizen"
    scope = "citizen" if is_citizen else "officer"
    mcp_url = args.citizen_mcp if is_citizen else args.officer_mcp
    resource = mcp_url[:-4] if mcp_url.endswith("/mcp") else mcp_url

    try:
        tokens = signin.sign_in(
            scope=scope, resource=resource,
            client_name="Sewasetu evals (%s)" % persona["id"],
            mobile=persona.get("signs_in_as") if is_citizen else None,
            username=args.admin_username if not is_citizen else None,
            password=args.admin_password if not is_citizen else None)
        record["signed_in_as"] = tokens["subject"]
        record["token_scope"] = tokens.get("scope")
        log("  signed in as %s" % tokens["subject"])
        async with mcp_connect(mcp_url, tokens["access_token"]) as (session, init):
            tools = await session.list_tools()
            record["tools_offered"] = sorted(t.name for t in tools.tools)
            log("  %d tools offered" % len(record["tools_offered"]))
            await conversation(session, init, persona, assistant, citizen, args.fixtures,
                               record["transcript"], max_turns=persona.get("max_turns", 10),
                               log=log)
    except SignInError as exc:
        record["error"] = "sign-in: %s" % exc
        log("  SIGN-IN FAILED: %s" % exc)
    except Exception as exc:                                   # noqa: BLE001
        record["error"] = describe(exc)
        log("  FAILED: %s" % record["error"])

    for check in persona.get("checks", []):
        outcome = evaluate(portal, check, record["transcript"], snapshot, marks)
        record["checks"].append(outcome)
        log("    %s %s -- %s" % ("PASS" if outcome["passed"] else "FAIL",
                                 outcome["check"], outcome["detail"]))
    record["passed"] = (record["error"] is None
                        and all(c["passed"] for c in record["checks"]))
    return record


def load_lessons(personas_path: str) -> str:
    """`evals/lessons.md`, rendered just enough for the dashboard.

    Kept in the repository rather than in the results file so that re-running the
    evals does not lose the note about what the last run taught us.
    """
    import re

    def inline(text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        return re.sub(r"`(.+?)`", r"<code>\1</code>", text)

    path = os.path.join(os.path.dirname(os.path.abspath(personas_path)), "lessons.md")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    out, in_list = [], False
    for line in text.split("\n"):
        line = line.rstrip()
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % inline(line[2:]))
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line:
            continue
        if line.startswith("#"):
            out.append("<p><b>%s</b></p>" % inline(line.lstrip("# ").strip()))
        else:
            out.append("<p>%s</p>" % inline(line))
    if in_list:
        out.append("</ul>")
    return "".join(out)


async def main_async(args) -> int:
    personas = load_personas(args.personas)
    if args.only:
        wanted = set(args.only.split(","))
        personas = [p for p in personas if p["id"] in wanted]

    print("waiting for the deployment")
    wait_for(args.base_url + "/.well-known/oauth-authorization-server", what="portal")
    wait_for(args.citizen_mcp.rsplit("/mcp", 1)[0] + "/.well-known/oauth-protected-resource",
             what="citizen MCP")
    wait_for(args.officer_mcp.rsplit("/mcp", 1)[0] + "/.well-known/oauth-protected-resource",
             what="officer MCP")

    conn = connect(args.config)
    portal = Portal(conn)

    print("resolving the cases the officers will decide")
    resolve_targets(personas, portal)

    print("clearing what earlier runs of the evals left behind")
    for persona in personas:
        if persona.get("fresh_mobile") and persona.get("mobile"):
            removed = portal.clear_eval_fixtures(persona["mobile"])
            print("  %s: %s" % (persona["mobile"], "cleared" if removed else "already clean"))

    watched = [c["application_no"] for p in personas for c in p.get("checks", [])
               if c["check"] == "unchanged"]
    snapshot = portal.snapshot(watched)
    # "Since this run started" is a row id, not a timestamp: the portal writes its
    # timestamps with the container's clock and its rules with Asia/Kolkata, and
    # those are not the same clock here.
    marks = portal.watermark()

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set")
    model = args.model or os.environ.get("EVAL_MODEL") or DEFAULT_MODEL
    assistant = Model(api_key, model)
    citizen = Model(api_key, model)
    print("assistant and citizen both played by %s\n" % model)

    signin = SignIn(args.base_url)
    results = []
    for persona in personas:
        print("== %s (%s)" % (persona["id"], persona["who"]))
        results.append(await run_persona(persona, args, signin, portal, snapshot,
                                         marks, assistant, citizen))
        print("")

    checks = [c for r in results for c in r["checks"]]
    passed = [c for c in checks if c["passed"]]
    report = {
        "generated_at": now_ist().isoformat(timespec="seconds") + "+05:30",
        "deployment": {"portal": args.base_url, "citizen_mcp": args.citizen_mcp,
                       "officer_mcp": args.officer_mcp},
        "model": {"assistant": assistant.stats(), "citizen": citizen.stats()},
        "summary": {
            "personas": len(results),
            "personas_passed": sum(1 for r in results if r["passed"]),
            "checks": len(checks),
            "checks_passed": len(passed),
            "pass_rate": round(len(passed) / len(checks), 4) if checks else 0.0,
        },
        "personas": results,
        "lessons": load_lessons(args.personas),
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)

    if args.runs:
        os.makedirs(args.runs, exist_ok=True)
        stamp = now_ist().strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(args.runs, "%s.json" % stamp), "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, default=str)

    print("=" * 68)
    print("%d/%d checks passed across %d personas (%.0f%%)"
          % (len(passed), len(checks), len(results), 100.0 * report["summary"]["pass_rate"]))
    for record in results:
        failed = [c for c in record["checks"] if not c["passed"]]
        mark = "ok  " if record["passed"] else "FAIL"
        print("  %s %-24s %s" % (mark, record["id"],
                                 record["error"] or ("%d/%d" % (
                                     len(record["checks"]) - len(failed), len(record["checks"])))))
    print("written to %s" % args.out)
    return 0


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="the portal's public URL")
    parser.add_argument("--citizen-mcp", required=True)
    parser.add_argument("--officer-mcp", required=True)
    parser.add_argument("--personas", default=os.path.join(repo, "evals", "personas.yaml"))
    parser.add_argument("--fixtures", default=os.path.join(repo, "evals", "fixtures"))
    parser.add_argument("--config", default=os.path.join(repo, "portal", "config", "app.ini"))
    parser.add_argument("--out", default=os.path.join(repo, "evals", "results", "latest.json"))
    parser.add_argument("--runs", default=None, help="also keep a timestamped copy here")
    parser.add_argument("--model", default=None, help="default: $EVAL_MODEL")
    parser.add_argument("--only", default=None, help="comma-separated persona ids")
    parser.add_argument("--admin-username", default=os.environ.get("ADMIN_USERNAME", "admin"))
    parser.add_argument("--admin-password", default=os.environ.get("ADMIN_PASSWORD", ""))
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

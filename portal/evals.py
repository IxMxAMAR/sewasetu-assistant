"""The eval dashboard.

Served from the portal, at /eval-dashboard, because the department asked for a
page rather than a report: the people we invented, what they said, and what the
database looked like afterwards. The same JSON is at /eval-dashboard.json for
anyone who would rather read it that way.
"""

from __future__ import annotations

import json
import os

from flask import Blueprint, jsonify, render_template

evals = Blueprint("evals", __name__)

DEFAULT_RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               os.pardir, "evals", "results", "latest.json")


def results_path() -> str:
    return os.path.abspath(os.environ.get("EVAL_RESULTS") or DEFAULT_RESULTS)


def load_results():
    path = results_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


@evals.get("/eval-dashboard")
def dashboard():
    return render_template("eval_dashboard.html", report=load_results(),
                           path=results_path())


@evals.get("/eval-dashboard.json")
def dashboard_json():
    report = load_results()
    if report is None:
        return jsonify({"error": "no eval run has been published on this host yet",
                        "looked_at": results_path()}), 404
    return jsonify(report)

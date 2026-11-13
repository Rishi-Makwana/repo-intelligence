"""Run acceptance.yaml through tools.py. Deterministic: no LLM judges the result, only structured
comparisons against each question's `expect`. Records a `runs` row; alerts on failure.

Never logs ALERT_WEBHOOK_URL or MCP_BEARER_TOKEN: a failure message names the acceptance id and what
differed, not secrets.
"""

from __future__ import annotations

import sqlite3

import httpx

from . import db, tools
from .config import Settings, load_acceptance


def _get(data, path: str):
    for part in path.split("."):
        data = data[int(part)] if part.isdigit() else data[part]
    return data


def check_expect(data: dict, expect: dict) -> str | None:
    """None if `data` satisfies every key in `expect`, else one line saying what failed."""
    if "repos_exactly" in expect:
        got = sorted(r["repo"] for r in data["results"])
        want = sorted(expect["repos_exactly"])
        if got != want:
            return f"repos_exactly: want {want}, got {got}"
    if "repos_include" in expect:
        got = {r["repo"] for r in data["results"]}
        missing = [r for r in expect["repos_include"] if r not in got]
        if missing:
            return f"repos_include: missing {missing} from {sorted(got)}"
    if "repo_in_top" in expect:
        wanted = expect["repo_in_top"]
        top = [r["repo"] for r in data["results"][:wanted["k"]]]
        if wanted["repo"] not in top:
            return f"repo_in_top: {wanted['repo']!r} not in top {wanted['k']} {top}"
    if "field_equals" in expect:
        wanted = expect["field_equals"]
        try:
            got = _get(data, wanted["path"])
        except (KeyError, IndexError, TypeError) as exc:
            return f"field_equals: path {wanted['path']!r} not found: {exc}"
        if got != wanted["value"]:
            return f"field_equals: {wanted['path']} want {wanted['value']!r}, got {got!r}"
    if "count" in expect:
        wanted = expect["count"]
        n = len(data["results"])
        lo, hi = wanted.get("min"), wanted.get("max")
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            return f"count: {n} results, want between {lo} and {hi}"
    return None


def run_selfcheck(conn: sqlite3.Connection, settings: Settings) -> dict:
    questions = load_acceptance(settings.config_dir / "acceptance.yaml")
    run_id = db.start_run(conn, "selfcheck")
    results = []
    for question in questions:
        tool = tools.TOOLS.get(question["tool"])
        if tool is None:
            results.append({"id": question["id"], "ok": False, "detail": f"unknown tool {question['tool']!r}"})
            continue
        try:
            envelope = tool(conn, settings, **question.get("args", {}))
            detail = check_expect(envelope["data"], question["expect"])
        except Exception as exc:  # noqa: BLE001 - one bad question must not stop the rest
            detail = f"{type(exc).__name__}: {exc}"
        results.append({"id": question["id"], "ok": detail is None, "detail": detail})
        if detail is not None:
            db.record_error(conn, run_id, None, "selfcheck", f"{question['id']}: {detail}")
    failed = [r for r in results if not r["ok"]]
    summary = {"total": len(results), "failed": [r["id"] for r in failed], "results": results}
    db.finish_run(conn, run_id, "failed" if failed else "ok", f"{len(failed)}/{len(results)} failed")
    return summary


def alert(settings: Settings, summary: dict) -> None:
    if not settings.alert_webhook_url:
        return
    failed = [r for r in summary["results"] if not r["ok"]]
    httpx.post(settings.alert_webhook_url, json={
        "text": f"repo-index selfcheck: {len(failed)}/{summary['total']} acceptance question(s) failed",
        "failed": [{"id": r["id"], "detail": r["detail"]} for r in failed]}, timeout=15)


def main(settings: Settings) -> dict:
    conn = db.open_index(settings)
    summary = run_selfcheck(conn, settings)
    if summary["failed"]:
        alert(settings, summary)
    return summary

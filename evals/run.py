"""Eval runner — execute every case in evals/cases.yaml and score it.

Run:
    ./venv/bin/python evals/run.py
    ./venv/bin/python evals/run.py --only alice_scoped_to_north
    ./venv/bin/python evals/run.py --no-report

What it does:
  1. Spawns the orchestrator in-process for each case (with the case's
     user, mints a JWT, runs the question).
  2. Captures both the assembled answer and the orchestrator's stdout
     (so we can assert on routing decisions printed by [ROUTING]).
  3. Evaluates the declarative assertions in cases.yaml.
  4. Writes evals/report.md and prints a scoreboard.

Exit code: 0 if every case passed, 1 otherwise — CI-friendly.

Assumptions:
  * The stack is already running (./scripts/stack.sh start). Per-case
    LLM calls go through OpenAI; expect a few cents per full sweep.
  * The orchestrator uses gpt-4o-mini by default; total cost ~$0.03-0.05
    per sweep of 10 cases.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Observability is set up here too so traces from eval runs go to Langfuse
# with a distinct app_name — easy to filter "eval runs" vs "live runs".
import observability  # noqa: E402

observability.setup("evals")

from auth import mint_jwt  # noqa: E402
from db import connect  # noqa: E402
from orchestrator.orchestrator import AgentOrchestrator  # noqa: E402


CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.yaml")
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.md")


# ── Domain types ─────────────────────────────────────────────────────────
@dataclass
class CaseResult:
    name: str
    description: str
    user: str | None
    question: str
    answer: str
    stdout: str
    routing_plan: list[str]
    assertions: list[tuple[str, bool, str]] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.assertions)


# ── Helpers ──────────────────────────────────────────────────────────────
def _resolve_user(email: str) -> tuple[str, str, str]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, role FROM users WHERE email = %s",
                (email,),
            )
            row = cur.fetchone()
    if row is None:
        raise LookupError(f"User '{email}' not seeded — run data/seed_rbac.py")
    return str(row["id"]), row["email"], row["role"]


def _extract_routing_plan(stdout: str) -> list[str]:
    """Pull the agent names out of the orchestrator's [ROUTING] Plan: line."""
    m = re.search(r"\[ROUTING\] Plan:\s*\[(.+?)\]", stdout)
    if not m:
        return []
    return re.findall(r"['\"]([\w\-]+)['\"]", m.group(1))


def _looks_like_failure(answer: str, stdout: str) -> bool:
    """Heuristic for 'orchestrator surfaced a failure'.

    We don't have a typed status from run() (it returns just the
    assembled answer), so we inspect both the answer text and the
    captured stdout for telltale failure markers.
    """
    blob = (answer + "\n" + stdout).lower()
    markers = [
        "status=failed",
        "401",
        "missing authorization",
        "all agents failed",
        "no agents are available",
        "agent contributions were empty",
        "could not be retrieved",
    ]
    return any(m in blob for m in markers)


# ── Assertion engine ─────────────────────────────────────────────────────
def _eval_assertions(case: dict[str, Any], result: CaseResult) -> None:
    """Append (name, ok, detail) tuples to result.assertions."""
    answer_lower = result.answer.lower()

    # should_contain — every substring must appear
    for needle in case.get("should_contain") or []:
        ok = needle.lower() in answer_lower
        detail = "" if ok else f"missing substring: {needle!r}"
        result.assertions.append((f"contains:{needle}", ok, detail))

    # should_not_contain — none of these substrings may appear
    for needle in case.get("should_not_contain") or []:
        ok = needle.lower() not in answer_lower
        detail = "" if ok else f"leaked substring: {needle!r}"
        result.assertions.append((f"not_contains:{needle}", ok, detail))

    # answer_status — completed or failed
    expected_status = case.get("answer_status")
    if expected_status:
        looks_failed = _looks_like_failure(result.answer, result.stdout)
        if expected_status == "failed":
            ok = looks_failed
            detail = "" if ok else "expected failure but answer looked completed"
        else:  # completed
            ok = (not looks_failed) and bool(result.answer.strip())
            detail = "" if ok else "expected completion but answer looked failed"
        result.assertions.append(("status", ok, detail))

    # routes_through_any — at least one match
    any_required = case.get("routes_through_any") or []
    if any_required:
        ok = any(a in result.routing_plan for a in any_required)
        detail = (
            ""
            if ok
            else f"none of {any_required} in plan {result.routing_plan}"
        )
        result.assertions.append(("routes_through_any", ok, detail))

    # routes_through_all — every name must appear
    all_required = case.get("routes_through_all") or []
    if all_required:
        missing = [a for a in all_required if a not in result.routing_plan]
        ok = not missing
        detail = "" if ok else f"missing from plan: {missing}"
        result.assertions.append(("routes_through_all", ok, detail))


# ── Runner ───────────────────────────────────────────────────────────────
async def _run_case(case: dict[str, Any]) -> CaseResult:
    name = case["name"]
    user_email = case.get("user")
    question = case["question"]

    jwt_token: str | None = None
    if user_email:
        user_id, email, role = _resolve_user(user_email)
        jwt_token = mint_jwt(user_id, email, role)

    captured = io.StringIO()
    answer = ""
    t0 = time.monotonic()
    with contextlib.redirect_stdout(captured):
        try:
            async with AgentOrchestrator(jwt_token=jwt_token) as orch:
                await orch.discover_agents()
                if orch.agents:
                    answer = await orch.run(question)
                else:
                    print("No agents available; treat as orchestrator failure.")
        except Exception as e:
            print(f"Eval runner caught exception: {type(e).__name__}: {e}")
    duration = time.monotonic() - t0

    stdout = captured.getvalue()
    routing_plan = _extract_routing_plan(stdout)

    result = CaseResult(
        name=name,
        description=case.get("description", "").strip(),
        user=user_email,
        question=question,
        answer=answer,
        stdout=stdout,
        routing_plan=routing_plan,
        duration_s=duration,
    )
    _eval_assertions(case, result)
    return result


# ── Reporting ────────────────────────────────────────────────────────────
def _print_case(result: CaseResult, idx: int, total: int) -> None:
    icon = "\033[32m✓\033[0m" if result.passed else "\033[31m✗\033[0m"
    user = result.user or "(anon)"
    print(f"  {icon} [{idx + 1}/{total}] {result.name}  "
          f"user={user}  ({result.duration_s:.1f}s)")
    if not result.passed:
        for aname, ok, detail in result.assertions:
            if not ok:
                print(f"      \033[31m✗\033[0m {aname}: {detail}")


def _write_report(results: list[CaseResult]) -> None:
    lines = [
        "# Eval report",
        "",
        f"Total: {len(results)} | "
        f"Passed: {sum(1 for r in results if r.passed)} | "
        f"Failed: {sum(1 for r in results if not r.passed)}",
        "",
    ]
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(f"## [{status}] {r.name}")
        lines.append("")
        lines.append(f"_User: `{r.user or '(anon)'}` · "
                     f"Duration: {r.duration_s:.1f}s · "
                     f"Routing: `{r.routing_plan}`_")
        lines.append("")
        if r.description:
            lines.append(r.description)
            lines.append("")
        lines.append(f"**Question:** {r.question}")
        lines.append("")
        lines.append("**Assertions:**")
        lines.append("")
        for aname, ok, detail in r.assertions:
            tick = "✅" if ok else "❌"
            line = f"- {tick} `{aname}`"
            if detail:
                line += f" — {detail}"
            lines.append(line)
        lines.append("")
        if not r.passed:
            # On failure, show the full answer so we can diagnose without
            # re-running. Token cost is sunk; bytes are cheap.
            preview = r.answer
            lines.append("<details><summary>Answer (full)</summary>")
            lines.append("")
            lines.append("```")
            lines.append(preview)
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ── Entry point ──────────────────────────────────────────────────────────
async def amain(only: str | None, write_report: bool) -> int:
    with open(CASES_PATH, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    cases = spec.get("cases") or []
    if only:
        cases = [c for c in cases if c["name"] == only]
        if not cases:
            print(f"No case named {only!r}")
            return 1

    print(f"\nRunning {len(cases)} case(s) ...\n")
    results: list[CaseResult] = []
    for idx, case in enumerate(cases):
        result = await _run_case(case)
        _print_case(result, idx, len(cases))
        results.append(result)

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\n──────────────────────────────────────")
    print(f"  {passed}/{total} passed "
          f"({100.0 * passed / total:.0f}%)" if total else "  no cases run")
    print(f"──────────────────────────────────────\n")

    if write_report:
        _write_report(results)
        print(f"Report written to {REPORT_PATH}\n")

    return 0 if passed == total else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the eval set")
    parser.add_argument("--only", help="Run a single case by name")
    parser.add_argument("--no-report", action="store_true",
                        help="Skip writing evals/report.md")
    args = parser.parse_args()

    sys.exit(asyncio.run(amain(args.only, not args.no_report)))


if __name__ == "__main__":
    main()

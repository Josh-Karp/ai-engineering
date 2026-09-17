#!/usr/bin/env python3
"""
Automated issue-development agent.

Polls GitHub for issues labeled "ready-for-agent" and runs each one through
a plan -> implement pipeline using Claude Code in headless mode, opening a
PR when the work is done.

State lives entirely in GitHub labels — no local state file. Only one of
the four labels is ever on an issue at a time:

    ready-for-agent    -> eligible for the agent to pick up
    agent-in-progress  -> currently being worked on
    agent-complete      -> a PR was opened
    agent-failed       -> planning or implementation failed; check agent.log

To retry a failed issue, just re-apply ready-for-agent by hand.

Usage:
    python issue_agent.py            # long-running loop, polls every INTERVAL
    python issue_agent.py --once     # process at most one issue and exit
                                      # (use this if you're driving it from
                                      # real cron/systemd-timer instead of
                                      # the built-in sleep loop)
    python issue_agent.py --dry-run  # print which issue would be picked up
                                      # next, without touching GitHub or
                                      # running Claude at all
"""

import argparse
import json
import logging
import subprocess
import time

# ---- Configuration ---------------------------------------------------------

READY_LABEL = "ready-for-agent"
IN_PROGRESS_LABEL = "agent-in-progress"
COMPLETE_LABEL = "agent-complete"
FAILED_LABEL = "agent-failed"

POLL_INTERVAL_SECONDS = 600
CLAUDE_TIMEOUT_SECONDS = 30 * 60  # hard kill for a hung run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("agent.log"), logging.StreamHandler()],
)
log = logging.getLogger("issue-agent")


# ---- GitHub helpers ---------------------------------------------------------


def run_gh(args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        log.error("gh %s failed: %s", " ".join(args), result.stderr.strip())
        return None
    return result.stdout


def get_ready_issues():
    """Only issues currently tagged ready-for-agent, oldest number first."""
    out = run_gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            READY_LABEL,
            "--json",
            "number,title,body",
            "--limit",
            "50",
        ]
    )
    if not out:
        return []
    return sorted(json.loads(out), key=lambda i: i["number"])


def set_labels(number, add=None, remove=None):
    args = ["issue", "edit", str(number)]
    for label in add or []:
        args += ["--add-label", label]
    for label in remove or []:
        args += ["--remove-label", label]
    run_gh(args)


def post_comment(number, body):
    run_gh(["issue", "comment", str(number), "--body", body])


# ---- Claude Code (headless) -------------------------------------------------


def run_claude(
    prompt, allowed_tools, permission_mode=None, timeout=CLAUDE_TIMEOUT_SECONDS
):
    """
    Run Claude Code non-interactively and return the parsed --output-format
    json result dict, or None on failure. `--bare` keeps runs deterministic
    in CI (skips hooks/MCP/plugins from whatever happens to be on the
    machine); `--permission-prompts none` guarantees nothing blocks waiting
    for a human who isn't there.
    """
    args = [
        "claude",
        "-p",
        prompt,
        "--bare",
        "--output-format",
        "json",
        "--permission-prompts",
        "none",
        "--allowedTools",
        allowed_tools,
    ]
    if permission_mode:
        args += ["--permission-mode", permission_mode]

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("claude run timed out after %ss", timeout)
        return None

    if result.returncode != 0:
        log.error("claude exited %s: %s", result.returncode, result.stderr[-2000:])
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("could not parse claude output: %s", result.stdout[-2000:])
        return None


def plan_issue(number, title, body):
    prompt = (
        f"You are planning work on GitHub issue #{number}: {title}\n\n{body}\n\n"
        "Investigate the codebase and produce a short implementation plan: "
        "which files change, the approach, risks, and a rough size estimate "
        "(small/medium/large). Do not write or edit any code — read-only "
        "investigation only. Return just the plan."
    )
    # Read-only tools: this phase can never touch the repo.
    return run_claude(
        prompt, allowed_tools="Read,Grep,Glob,Bash(git log *),Bash(git diff *)"
    )


def implement_issue(number, title, plan_text):
    prompt = (
        f"You are implementing GitHub issue #{number}: {title}, following "
        f"this plan:\n\n{plan_text}\n\n"
        f"1. Create branch issue-{number} off the default branch.\n"
        "2. Implement the plan, running the test suite as you go.\n"
        f"3. Commit with a message referencing #{number}.\n"
        f'4. Push the branch and open a PR with "Closes #{number}" in the '
        "description.\n"
        "If you get stuck or the plan doesn't match what you find in the "
        "code, stop and report back instead of guessing."
    )
    return run_claude(
        prompt,
        allowed_tools="Bash,Read,Edit,Write",
        permission_mode="acceptEdits",
    )


# ---- Pipeline ----------------------------------------------------------------


def process_issue(issue):
    number, title, body = issue["number"], issue["title"], issue["body"] or ""
    log.info("Claiming issue #%s: %s", number, title)
    set_labels(number, add=[IN_PROGRESS_LABEL], remove=[READY_LABEL])

    plan_result = plan_issue(number, title, body)
    if not plan_result:
        log.error("Planning failed for #%s", number)
        set_labels(number, add=[FAILED_LABEL], remove=[IN_PROGRESS_LABEL])
        post_comment(number, "Automated planning failed — see agent.log.")
        return

    plan_text = plan_result.get("result", "")
    post_comment(number, f"**Plan (automated):**\n\n{plan_text}")

    impl_result = implement_issue(number, title, plan_text)
    if not impl_result:
        log.error("Implementation failed for #%s", number)
        set_labels(number, add=[FAILED_LABEL], remove=[IN_PROGRESS_LABEL])
        post_comment(number, "Automated implementation failed — see agent.log.")
        return

    log.info("Issue #%s done: %s", number, str(impl_result.get("result", ""))[:200])
    set_labels(number, add=[COMPLETE_LABEL], remove=[IN_PROGRESS_LABEL])


def dry_run():
    """Show what the next real run would claim, without changing anything."""
    if run_gh(["auth", "status"]) is None:
        log.error("gh is not authenticated — aborting")
        return

    issues = get_ready_issues()
    if not issues:
        log.info("Dry run: no issues currently labeled %r.", READY_LABEL)
        return

    log.info("Dry run: %d issue(s) labeled %r.", len(issues), READY_LABEL)
    next_issue = issues[0]
    log.info(
        "Would claim issue #%s: %s",
        next_issue["number"],
        next_issue["title"],
    )
    for issue in issues[1:]:
        log.info("  queued behind it: #%s: %s", issue["number"], issue["title"])


def loop_runner(run_once=False):
    if run_gh(["auth", "status"]) is None:
        log.error("gh is not authenticated — aborting")
        return

    while True:
        issues = get_ready_issues()
        if issues:
            process_issue(issues[0])
        else:
            log.info("No ready issues.")
            if run_once:
                return
            log.info("Sleeping %ss.", POLL_INTERVAL_SECONDS)
            time.sleep(POLL_INTERVAL_SECONDS)

        if run_once:
            return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="process at most one issue and exit, instead of looping forever",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print which issue would be picked up next; makes no GitHub or Claude calls that change anything",
    )
    args = parser.parse_args()
    if args.dry_run:
        dry_run()
    else:
        loop_runner(run_once=args.once)

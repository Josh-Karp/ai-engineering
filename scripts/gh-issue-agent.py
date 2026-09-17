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
    agent-complete     -> a PR was opened
    agent-failed       -> planning or implementation failed

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
    python issue_agent.py -v         # add -v/--verbose to any of the above
                                      # for full gh/claude command + response
                                      # detail (cost, duration, raw output)
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

AGENT_LOG_FILE = "GH_ISSUE_AGENT.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(AGENT_LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("issue-agent")


# ---- GitHub helpers ---------------------------------------------------------


def run_gh(args):
    log.debug("gh %s", " ".join(args))
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        log.error("gh %s failed: %s", " ".join(args), result.stderr.strip())
        return None
    log.debug("gh %s -> %s", args[0:2], result.stdout[:1000])
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
    issues = sorted(json.loads(out), key=lambda i: i["number"])
    log.debug(
        "found %d ready issue(s): %s",
        len(issues), [i["number"] for i in issues],
    )
    return issues


def set_labels(number, add=None, remove=None):
    log.debug("issue #%s: +%s -%s", number, add or [], remove or [])
    args = ["issue", "edit", str(number)]
    for label in add or []:
        args += ["--add-label", label]
    for label in remove or []:
        args += ["--remove-label", label]
    run_gh(args)


def post_comment(number, body):
    log.debug("issue #%s: posting comment (%d chars)", number, len(body))
    run_gh(["issue", "comment", str(number), "--body", body])


# ---- Claude Code (headless) -------------------------------------------------


def run_claude(
    prompt, allowed_tools, permission_mode=None, model=None, timeout=CLAUDE_TIMEOUT_SECONDS
):
    """
    Run Claude Code non-interactively and return the parsed --output-format
    json result dict, or None on failure.
    """
    args = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-prompts",
        "none",
        "--allowedTools",
        allowed_tools,
    ]
    if permission_mode:
        args += ["--permission-mode", permission_mode]
    if model:
        args += ["--model", model]

    log.debug("claude args: %r", args)

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("claude run timed out after %ss", timeout)
        return None

    if result.returncode != 0:
        # Claude Code prints some failures (e.g. missing auth) to stdout
        # rather than stderr, so log both.
        log.error(
            "claude exited %s\nstdout: %s\nstderr: %s",
            result.returncode, result.stdout[-2000:], result.stderr[-2000:],
        )
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("could not parse claude output: %s", result.stdout[-2000:])
        return None
    if parsed.get("is_error"):
        # Exit code 0 doesn't guarantee success — auth failures, rate
        # limits, etc. can come back as a normal-looking JSON envelope
        # with is_error set and the problem described in "result".
        log.error("claude reported an error: %s", parsed.get("result"))
        return None

    log.info(
        "claude call finished (model=%s): %sms, $%.4f, %s turn(s), session %s",
        model or "default",
        parsed.get("duration_ms"),
        parsed.get("total_cost_usd", 0) or 0,
        parsed.get("num_turns"),
        parsed.get("session_id"),
    )
    log.debug("claude result text: %s", str(parsed.get("result", ""))[:2000])
    return parsed


def plan_issue(number, title, body):
    prompt = (
        f"You are planning work on GitHub issue #{number}: {title}\n\n{body}\n\n"
        "Investigate the codebase and produce a short implementation plan: "
        "which files change, the approach, risks, and a rough size estimate "
        "(small/medium/large). Return just the plan."
    )
    # --permission-mode plan blocks every write/bash-mutation outright, so
    # this phase stays read-only regardless of the org's acceptEdits
    # default in settings.json. Haiku is plenty for read-only investigation
    # and keeps this cheap phase cheap.
    return run_claude(
        prompt,
        allowed_tools="Read,Grep,Glob,Bash(git log *),Bash(git diff *)",
        permission_mode="plan",
        model="haiku",
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
        model="sonnet",
    )


# ---- Pipeline ----------------------------------------------------------------


def process_issue(issue):
    number, title, body = issue["number"], issue["title"], issue["body"] or ""
    log.info("Claiming issue #%s: %s", number, title)
    set_labels(number, add=[IN_PROGRESS_LABEL], remove=[READY_LABEL])

    log.info("Planning issue #%s", number)
    plan_result = plan_issue(number, title, body)
    if not plan_result:
        log.error("Planning failed for #%s", number)
        set_labels(number, add=[FAILED_LABEL], remove=[IN_PROGRESS_LABEL])
        post_comment(number, "Automated planning failed. See " + AGENT_LOG_FILE + ".")
        return

    plan_text = plan_result.get("result", "")
    log.info("Plan ready for #%s (%d chars), posting comment", number, len(plan_text))
    post_comment(number, f"**Plan (automated):**\n\n{plan_text}")

    log.info("Implementing issue #%s", number)
    impl_result = implement_issue(number, title, plan_text)
    if not impl_result:
        log.error("Implementation failed for #%s", number)
        set_labels(number, add=[FAILED_LABEL], remove=[IN_PROGRESS_LABEL])
        post_comment(number, "Automated implementation failed. See " + AGENT_LOG_FILE + ".")
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
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="log full gh/claude commands and responses (cost, duration, raw output) at DEBUG level",
    )
    args = parser.parse_args()
    if args.verbose:
        log.setLevel(logging.DEBUG)
    if args.dry_run:
        dry_run()
    else:
        loop_runner(run_once=args.once)

"""Keep notifier runs on the 30-minute marks GitHub's scheduler drops."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
# UTC hours of cron "7,37 22-23,0-15 * * *" (JST 07:07 through 00:37).
WINDOW_HOURS = frozenset({22, 23, *range(16)})
SLOT_MINUTES = (7, 37)
# A second bridge must not start another scan for a slot that just ran.
SLOT_GUARD_SECONDS = 15 * 60
# Wake this long before the slot so the follow-up still targets that slot.
DUE_TOLERANCE_SECONDS = 90
# Stay under the 6-hour job limit, including checkout and the next dispatch.
MAX_BRIDGE_SECONDS = 5 * 60 * 60
MAX_WAIT_SECONDS = 7 * 60 * 60
HEARTBEAT_SECONDS = 10 * 60
ACTIVE_STATUSES = ("queued", "in_progress", "waiting", "requested", "pending", "action_required")
CHAIN_TITLES = frozenset({"schedule", "relay"})
WORKFLOW_FILE = "notify.yml"
API = "https://api.github.com"


def next_slot(now: datetime) -> datetime:
    """Return the next :07 or :37 inside the cron window, strictly after now."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    hour_cursor = now.replace(minute=0, second=0, microsecond=0)
    for offset in range(48):
        hour = hour_cursor + timedelta(hours=offset)
        if hour.hour not in WINDOW_HOURS:
            continue
        for minute in SLOT_MINUTES:
            slot = hour.replace(minute=minute)
            if slot > now:
                return slot
    raise RuntimeError("no slot within 48 hours")


def follow_up(now: datetime, after_scan: bool = False) -> tuple[str, int]:
    """Decide whether to scan now or sleep toward the next slot.

    The sleep stops one tolerance before the slot. Sleeping until the slot
    itself would make the next decision skip it and wait another 30 minutes.
    A scan that finishes inside that tolerance must not start another scan
    for the same slot.
    """
    now = now.astimezone(timezone.utc)
    slot = next_slot(now)
    if after_scan and (slot - now).total_seconds() <= DUE_TOLERANCE_SECONDS:
        slot = next_slot(slot)
    delay = int((slot - now).total_seconds())
    if delay <= DUE_TOLERANCE_SECONDS:
        return "scan", 0
    return "bridge", min(delay - DUE_TOLERANCE_SECONDS, MAX_BRIDGE_SECONDS)


def parse_wait_seconds(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ValueError("wait_seconds must be a non-negative integer")
    seconds = int(value)
    if seconds > MAX_WAIT_SECONDS:
        raise ValueError("wait_seconds is too large")
    return seconds


def run_labels(run: dict) -> set[str]:
    return {str(run.get("display_title") or ""), str(run.get("name") or "")}


def schedule_is_redundant(run_id: str, runs: list[dict]) -> bool:
    """True when a schedule firing would duplicate a live 30-minute chain."""
    for run in runs:
        if str(run.get("id")) == str(run_id):
            continue
        if run_labels(run) & CHAIN_TITLES:
            return True
    return False


def is_follow_up(run: dict) -> bool:
    """True for the relay this dispatcher just requested.

    display_title can still be the workflow name for a moment after creation.
    A new workflow_dispatch that is not manual or Cloudflare is that relay.
    """
    labels = run_labels(run)
    if "relay" in labels:
        return True
    if labels & {"manual", "cloudflare", "schedule"}:
        return False
    return run.get("event") == "workflow_dispatch"


def build_dispatch_body(ref: str, wait_seconds: int) -> dict:
    if not re.fullmatch(r"[\w./-]+", ref or ""):
        raise ValueError("ref must be a branch name")
    return {
        "ref": ref,
        "inputs": {
            "source": "relay",
            # Schedule runs book; the relay continues that behavior.
            "dry_run": False,
            "wait_seconds": str(wait_seconds),
        },
    }


def _repository(repository: str) -> str:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository or ""):
        raise ValueError("GITHUB_REPOSITORY must be owner/repo")
    return repository


def runs_url(repository: str, status: str | None = None) -> str:
    repository = _repository(repository)
    root = f"{API}/repos/{repository}/actions/workflows/{WORKFLOW_FILE}/runs"
    if status is None:
        return f"{root}?per_page=20"
    return f"{root}?status={status}&per_page=20"


def dispatch_url(repository: str) -> str:
    repository = _repository(repository)
    return f"{API}/repos/{repository}/actions/workflows/{WORKFLOW_FILE}/dispatches"


def github_request(method: str, url: str, payload, token: str):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "arakawa-bot-schedule",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub {method} failed ({exc.code})") from None


def listed_runs(repository: str, token: str, request, status: str | None = None) -> list[dict]:
    status_code, payload = request("GET", runs_url(repository, status), None, token)
    if status_code != 200:
        raise RuntimeError(f"GitHub GET failed ({status_code})")
    runs = payload.get("workflow_runs", [])
    if not isinstance(runs, list):
        raise RuntimeError("GitHub runs response is invalid")
    return runs


def active_runs(repository: str, token: str, request) -> list[dict]:
    runs = []
    for status in ACTIVE_STATUSES:
        runs.extend(listed_runs(repository, token, request, status))
    return runs


def created_at(run: dict) -> datetime | None:
    raw = run.get("created_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def recent_follow_up(runs: list[dict], current_id: str, now: datetime, window: int = SLOT_GUARD_SECONDS) -> bool:
    """True when this 30-minute slot already has a relay, including a finished one."""
    now = now.astimezone(timezone.utc)
    for run in runs:
        if str(run.get("id")) == str(current_id):
            continue
        if not is_follow_up(run):
            continue
        created = created_at(run)
        if created is None:
            continue
        age = (now - created.astimezone(timezone.utc)).total_seconds()
        if 0 <= age <= window:
            return True
    return False


def poll_for_relay(repository, token, known_ids, request, sleep, attempts: int = 12):
    for attempt in range(attempts):
        if attempt:
            sleep(5)
        for run in listed_runs(repository, token, request):
            if str(run.get("id")) in known_ids:
                continue
            if is_follow_up(run):
                return run
    return None


def dispatch_next(now: datetime, repository: str, ref: str, token: str, request, sleep=time.sleep, after_scan: bool = False):
    if os.environ.get("SCHEDULER_MODE") == "cloudflare":
        return {"status": "skipped"}
    current = os.environ.get("GITHUB_RUN_ID", "")
    recent = listed_runs(repository, token, request)
    if any(
        str(run.get("id")) != current and run.get("status") in ACTIVE_STATUSES and is_follow_up(run)
        for run in recent
    ):
        print("relay already active; not dispatching another", flush=True)
        return {"status": "busy"}
    action, wait = follow_up(now, after_scan=after_scan)
    if action == "scan" and recent_follow_up(recent, current, now):
        print("a scan for this slot already started; not dispatching another", flush=True)
        return {"status": "busy"}
    slot = next_slot(now).astimezone(JST)
    print(f"next slot {slot:%Y-%m-%d %H:%M} JST via {action} wait={wait}", flush=True)
    known = {str(run.get("id")) for run in recent}
    status_code, _ = request("POST", dispatch_url(repository), build_dispatch_body(ref, wait), token)
    if status_code != 204:
        raise RuntimeError(f"GitHub dispatch failed ({status_code})")
    found = poll_for_relay(repository, token, known, request, sleep)
    if found is None:
        raise RuntimeError("relay run did not appear after dispatch")
    print(f"relay run {found.get('id')} is {found.get('status')}", flush=True)
    return {"status": "accepted", "action": action, "wait_seconds": wait, "run_id": found.get("id")}


def sleep_until_due(
    now_fn=None, sleep=time.sleep, log=print, monotonic=time.monotonic,
    heartbeat: int = HEARTBEAT_SECONDS, max_seconds: int = MAX_BRIDGE_SECONDS,
):
    """Sleep toward the next slot, using the clock rather than a stale duration.

    The job may start late. A fixed sleep counted from dispatch time would then
    wake up after the slot and skip that 30-minute run.
    """
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    started = monotonic()
    while True:
        action, wait = follow_up(now_fn())
        elapsed = monotonic() - started
        if action == "scan" or elapsed >= max_seconds:
            log("wait complete" if action == "scan" else "handoff wait")
            return
        chunk = min(heartbeat, wait, max_seconds - elapsed)
        if chunk <= 0:
            log("handoff wait")
            return
        slot = next_slot(now_fn()).astimezone(JST)
        log(f"next run in {wait}s at {slot:%Y-%m-%d %H:%M} JST")
        sleep(chunk)


def write_output(name: str, value: str):
    line = f"{name}={value}\n"
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(line, end="")
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)


def _token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token.strip():
        raise RuntimeError("GH_TOKEN is not configured")
    return token


def _context() -> tuple[str, str, str]:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("GITHUB_REF_NAME") or "main"
    return repository, ref, _token()


def gate() -> int:
    try:
        runs = active_runs(os.environ["GITHUB_REPOSITORY"], _token(), github_request)
        skip = schedule_is_redundant(os.environ["GITHUB_RUN_ID"], runs)
    except Exception as exc:
        print(f"relay check failed: {type(exc).__name__}: {exc}", flush=True)
        skip = False
    write_output("skip", "true" if skip else "false")
    print("skip delayed schedule" if skip else "no active relay", flush=True)
    return 0


def dispatch() -> int:
    repository, ref, token = _context()
    dispatch_next(
        datetime.now(timezone.utc), repository, ref, token, github_request,
        after_scan=os.environ.get("AFTER_SCAN") == "true",
    )
    return 0


def sleep_command() -> int:
    # The value only selects this waiting job. The clock decides the real delay.
    parse_wait_seconds(os.environ.get("WAIT_SECONDS", ""))
    sleep_until_due()
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "gate":
        return gate()
    if command == "dispatch":
        return dispatch()
    if command == "sleep":
        return sleep_command()
    print("usage: arakawa_schedule.py gate|dispatch|sleep", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

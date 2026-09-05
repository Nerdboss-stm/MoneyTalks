"""Dashboard screenshots of PRISM for the README, taken in a headed Chromium the human has logged into.

The profile persists in .playwright/prism so the login survives re-runs. Nothing here types credentials:
the script opens PRISMTRACE_HOST, asks the human to log in and open the project, then finds pages by their
visible text. Each "press Enter" also accepts the file .playwright/prism-continue appearing, so the script can
be driven from a terminal or from a session that cannot reach its stdin.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "backend" / ".env"
PROFILE = ROOT / ".playwright" / "prism"
SIGNAL = ROOT / ".playwright" / "prism-continue"
OUT = ROOT / "shots" / "prism"
DEFAULT_HOST = "https://prism.blockconvey.com"
V1_SESSION = "explain-v1-04"
V2_SESSION = "explain-v2-04"
FIND_TIMEOUT_MS = 20_000
PROJECT = "moneytalks-event"
FLAGGED_AGENT = "controller_a"  # the owner of the v1 meeting's flagged first turn


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def wait_enter(prompt: str) -> None:
    """Block until Enter arrives on stdin or the signal file appears. EOF on stdin never counts as Enter."""
    print(prompt, flush=True)
    SIGNAL.unlink(missing_ok=True)
    pressed = threading.Event()

    def reader() -> None:
        try:
            if sys.stdin.readline():
                pressed.set()
        except Exception:
            pass

    if sys.stdin is not None and not sys.stdin.closed:
        threading.Thread(target=reader, daemon=True).start()
    while not pressed.is_set():
        if SIGNAL.exists():
            SIGNAL.unlink(missing_ok=True)
            break
        time.sleep(0.5)


def settle(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=FIND_TIMEOUT_MS)
    except Exception:
        pass
    try:
        page.get_by_text(re.compile(r"^loading", re.I)).first.wait_for(state="hidden", timeout=FIND_TIMEOUT_MS)
    except Exception:
        pass
    page.wait_for_timeout(800)


def shot(page: Page, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"saved {path}", flush=True)
    return path


def click_text(page: Page, pattern: str, role: str | None = None) -> None:
    """Click the first visible element whose text matches; links and buttons are tried first."""
    rx = re.compile(pattern, re.I)
    candidates = []
    if role:
        candidates.append(page.get_by_role(role, name=rx))
    candidates += [page.get_by_role("link", name=rx), page.get_by_role("button", name=rx), page.get_by_text(rx)]
    deadline = time.monotonic() + FIND_TIMEOUT_MS / 1000
    last: Exception | None = None
    while time.monotonic() < deadline:
        for loc in candidates:
            try:
                if loc.first.is_visible():
                    loc.first.click(timeout=5_000)
                    return
            except Exception as exc:
                last = exc
        page.wait_for_timeout(500)
    raise TimeoutError(f"no visible element for {pattern!r}: {last}")


def filter_sessions(page: Page, session_id: str) -> None:
    """Sessions list narrowed to one session id: the nav's Sessions entry, then whichever search box the page offers."""
    click_text(page, r"^\s*sessions\s*$")
    settle(page)
    box = page.get_by_placeholder(re.compile(r"search|filter|session", re.I)).first
    try:
        box.wait_for(state="visible", timeout=FIND_TIMEOUT_MS)
        box.fill(session_id)
        box.press("Enter")
    except Exception:
        page.get_by_text(session_id, exact=False).first.wait_for(state="visible", timeout=FIND_TIMEOUT_MS)
    settle(page)
    row = page.get_by_text(session_id, exact=False).first
    row.wait_for(state="visible", timeout=FIND_TIMEOUT_MS)
    row.scroll_into_view_if_needed()


def click_flagged_row(page: Page) -> None:
    """The first trace row flagged Yes, opened through its trace-id cell; other flag markers are the fallback."""
    yes = page.locator("tr", has=page.get_by_text(re.compile(r"^\s*yes\s*$", re.I))).first
    try:
        yes.wait_for(state="visible", timeout=FIND_TIMEOUT_MS)
        cell = yes.locator("td").first
        (cell if cell.count() else yes).click(timeout=5_000)
        return
    except Exception:
        pass
    flag = page.get_by_text(re.compile(r"flagged", re.I)).first
    flag.wait_for(state="visible", timeout=FIND_TIMEOUT_MS)
    row = flag.locator("xpath=ancestor-or-self::*[self::tr or self::a or @role='row' or @role='link' or @role='button'][1]")
    (row.first if row.count() else flag).click(timeout=5_000)


def open_flagged_trace(page: Page, session_id: str) -> None:
    """Into the session, through its flagged-traces list, onto the first flagged trace."""
    page.get_by_text(session_id, exact=False).first.click(timeout=FIND_TIMEOUT_MS)
    settle(page)
    for pattern in (r"view flagged traces", r"^\s*traces\s*$"):
        try:
            click_text(page, pattern, role="button")
            settle(page)
            break
        except Exception:
            continue
    click_flagged_row(page)
    settle(page)
    # the trace page renders a skeleton first; wait for its analysis text before the frame
    try:
        page.get_by_text(re.compile(r"analysis|evaluation|quality|flag", re.I)).first.wait_for(state="visible", timeout=FIND_TIMEOUT_MS)
    except Exception:
        pass
    page.wait_for_timeout(1500)


def open_agent_run(page: Page) -> None:
    """The trace's Agent Run: the page's own button when it has one, else the Agent Runs view, loaded and with a run selected."""
    try:
        click_text(page, r"view agent run", role="button")
    except Exception:
        click_text(page, r"^\s*agent runs\s*$", role="link")
    settle(page)
    placeholder = page.get_by_text(re.compile(r"select an agent run", re.I)).first
    try:
        if placeholder.is_visible():
            # nothing preselected: narrow the list to the flagged turn's owner and open its first run by title
            box = page.get_by_placeholder(re.compile(r"search", re.I)).first
            if box.is_visible():
                box.fill(FLAGGED_AGENT)
                page.wait_for_timeout(800)
            # a per-turn trajectory (four steps: lookup, reasoning, verify, answer) shows the tool steps;
            # the session-assembled run lists only LLM steps
            trajectory = page.get_by_text(re.compile(rf"{FLAGGED_AGENT}\s*·\s*4 steps", re.I)).first
            if trajectory.is_visible():
                trajectory.click(timeout=FIND_TIMEOUT_MS)
            else:
                page.get_by_text(FLAGGED_AGENT, exact=True).first.click(timeout=FIND_TIMEOUT_MS)
            settle(page)
            page.wait_for_timeout(1500)
    except Exception:
        pass


def attempt(page: Page, name: str, fn: Callable[[], None]) -> Path:
    try:
        fn()
        return shot(page, name)
    except Exception as exc:
        print(f"{name}: not found within {FIND_TIMEOUT_MS // 1000} s ({type(exc).__name__}: {str(exc)[:120]})", flush=True)
        return shot(page, f"{name}-notfound")


def main() -> int:
    load_env()
    host = os.environ.get("PRISMTRACE_HOST", DEFAULT_HOST).rstrip("/")
    PROFILE.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE), headless=False, viewport={"width": 1600, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(FIND_TIMEOUT_MS)
        page.goto(host)
        settle(page)
        wait_enter(f"Log in, open the {PROJECT} project, then press Enter here")
        settle(page)

        paths.append(attempt(page, "01-v1-session", lambda: filter_sessions(page, V1_SESSION)))
        paths.append(attempt(page, "02-v1-flagged-trace", lambda: open_flagged_trace(page, V1_SESSION)))
        paths.append(attempt(page, "03-agent-run-tools", lambda: open_agent_run(page)))
        paths.append(attempt(page, "06-v2-session", lambda: filter_sessions(page, V2_SESSION)))

        print(f"Now click Root Cause on {V1_SESSION}, then Remediation, in the browser I opened. Press Enter after each.", flush=True)
        wait_enter("Press Enter after Root Cause is on screen")
        settle(page)
        paths.append(shot(page, "04-root-cause"))
        wait_enter("Press Enter after Remediation is on screen")
        settle(page)
        paths.append(shot(page, "05-remediation"))

        print("\n".join(str(x) for x in sorted(paths)), flush=True)
        wait_enter("Browser stays open. Press Enter here (or Ctrl-C) when you are done with it.")
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

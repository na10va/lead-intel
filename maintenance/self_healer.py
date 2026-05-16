"""
maintenance/self_healer.py — Detects and attempts to auto-repair broken scrapers.

Trigger: Any scraper that returns 0 records for 2 consecutive runs,
         or throws an unhandled exception during FETCH or PARSE.

Auto-repair sequence:
    1. Log failure with full error details
    2. Re-fetch source URL, detect CAPTCHA/block/structural change
    3. Attempt heuristic selector repair (class/ID renames, table restructuring)
    4. If heuristic fix found: patch agent file, re-run, log self-repair
    5. If still broken after 15 min: SMS owner, flag needs_manual_review=True

Future: set ANTHROPIC_API_KEY in .env to enable Claude-powered selector detection.

CLI:
    python maintenance/self_healer.py --source probate_cuyahoga
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

from db.client import get_client, insert_row, update_row
from routing.notify import send_sms
from utils.logger import get_logger

log = get_logger("maintenance.self_healer")

REPAIR_TIMEOUT_SEC = 15 * 60  # 15 minutes

# Maps source_name → (agent_module_path, source_url, expected_data_pattern)
# expected_data_pattern: a regex that should match the page content when the source is healthy
SOURCE_REGISTRY: dict[str, dict] = {
    "probate_cuyahoga": {
        "agent": "agents/probate_agent.py",
        "url": "https://probate.cuyahogacounty.us/",
        "pattern": r"(?i)(estate|decedent|probate|case)",
    },
    "probate_lake": {
        "agent": "agents/probate_agent.py",
        "url": "https://www.lakecountyohio.gov/courts/probate/",
        "pattern": r"(?i)(estate|probate|case)",
    },
    "probate_mahoning": {
        "agent": "agents/probate_agent.py",
        "url": "https://www.mahoningcountyclerk.org/probate/",
        "pattern": r"(?i)(estate|probate|case)",
    },
    "code_violation_cuyahoga": {
        "agent": "agents/code_violation_agent.py",
        "url": "https://data.cuyahogacounty.us/",
        "pattern": r"(?i)(violation|inspection|property)",
    },
    "code_violation_lake": {
        "agent": "agents/code_violation_agent.py",
        "url": "https://www.lakecountyohio.gov/",
        "pattern": r"(?i)(violation|code|inspection)",
    },
    "code_violation_mahoning": {
        "agent": "agents/code_violation_agent.py",
        "url": "https://www.youngstownohio.gov/",
        "pattern": r"(?i)(violation|code|inspection)",
    },
    "foreclosure_cuyahoga": {
        "agent": "agents/foreclosure_agent.py",
        "url": "https://recorder.cuyahogacounty.us/",
        "pattern": r"(?i)(lis pendens|foreclosure|deed|recording)",
    },
    "foreclosure_lake": {
        "agent": "agents/foreclosure_agent.py",
        "url": "https://www.lakecountyohio.gov/recorder/",
        "pattern": r"(?i)(recording|deed|lien|foreclosure)",
    },
    "foreclosure_mahoning": {
        "agent": "agents/foreclosure_agent.py",
        "url": "https://www.mahoningcountyclerk.org/recorder/",
        "pattern": r"(?i)(recording|deed|lien|foreclosure)",
    },
    "tax_lien_cuyahoga": {
        "agent": "agents/tax_lien_agent.py",
        "url": "https://treasurer.cuyahogacounty.us/",
        "pattern": r"(?i)(delinquent|tax|lien|parcel)",
    },
    "tax_lien_lake": {
        "agent": "agents/tax_lien_agent.py",
        "url": "https://www.lakecountyohio.gov/auditor",
        "pattern": r"(?i)(delinquent|tax|lien|parcel)",
    },
    "tax_lien_mahoning": {
        "agent": "agents/tax_lien_agent.py",
        "url": "https://auditor.mahoningcountyoh.gov/",
        "pattern": r"(?i)(delinquent|tax|lien|parcel)",
    },
}

# Patterns that indicate the source has blocked or CAPTCHA'd us
BLOCK_PATTERNS = [
    r"(?i)access denied",
    r"(?i)403 forbidden",
    r"(?i)captcha",
    r"(?i)are you a robot",
    r"(?i)bot detection",
    r"(?i)cloudflare",
    r"(?i)too many requests",
    r"(?i)rate limit",
    r"(?i)your request has been blocked",
]


def _project_root() -> Path:
    return Path(__file__).parent.parent


def check_consecutive_failures(source_name: str, threshold: int = 2) -> bool:
    """Return True if a source has had `threshold` consecutive unresolved failures."""
    client = get_client()
    response = (
        client.table("maintenance_log")
        .select("resolved")
        .eq("source_name", source_name)
        .eq("event_type", "self_heal")
        .order("created_at", desc=True)
        .limit(threshold)
        .execute()
    )
    logs = response.data or []
    if len(logs) < threshold:
        return False
    return all(not row.get("resolved") for row in logs[:threshold])


def _fetch_source_html(url: str, timeout: int = 20) -> tuple[int, str]:
    """Fetch a source URL. Returns (status_code, html_body)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return resp.status_code, resp.text
    except requests.RequestException as e:
        log.warning(f"HTTP fetch failed for {url}: {e}")
        return 0, ""


def _detect_block(html: str) -> str | None:
    """Return a description of the block if the HTML indicates we're blocked, else None."""
    for pattern in BLOCK_PATTERNS:
        if re.search(pattern, html[:5000]):
            return pattern
    return None


def _heuristic_selector_repair(source_name: str, html: str, agent_file: str) -> bool:
    """Attempt to detect and patch a broken CSS/regex selector in an agent file.

    Strategy:
        1. Parse common data table structures from the fresh HTML
        2. Compare against the selectors currently in the agent file
        3. If a known selector has likely been renamed, patch the file

    Returns True if a patch was applied.
    """
    agent_path = _project_root() / agent_file
    if not agent_path.exists():
        log.warning(f"Agent file not found: {agent_path}")
        return False

    agent_code = agent_path.read_text()

    # Extract all CSS class names and IDs from the fresh HTML
    html_classes = set(re.findall(r'class=["\']([^"\']+)["\']', html))
    html_ids = set(re.findall(r'id=["\']([^"\']+)["\']', html))
    all_html_attrs = html_classes | html_ids

    # Extract selector strings from the agent code (CSS-like patterns in strings)
    agent_selectors = re.findall(r'["\']([a-zA-Z][\w\-\.#]+)["\']', agent_code)

    log.debug(f"Running heuristic selector repair for {source_name}")
    patched = False
    new_code = agent_code

    for sel in agent_selectors:
        # Only consider CSS-like selectors (contain . or # or - or are multi-word)
        if not re.search(r'[\.\-#]', sel) and '_' not in sel:
            continue
        if sel in all_html_attrs:
            continue  # selector is still valid

        # Look for a close match in the fresh HTML attributes using edit distance heuristic
        clean_sel = re.sub(r'[\.\#]', '', sel).lower()
        candidates = [
            attr for attr in all_html_attrs
            if abs(len(attr) - len(clean_sel)) <= 4
            and clean_sel[:4] == attr[:4]  # same first 4 chars
        ]
        if len(candidates) == 1:
            new_sel = candidates[0]
            log.info(f"  Selector heuristic: '{sel}' → '{new_sel}' in {agent_file}")
            new_code = new_code.replace(f'"{sel}"', f'"{new_sel}"').replace(f"'{sel}'", f"'{new_sel}'")
            patched = True

    if patched:
        agent_path.write_text(new_code)
        log.info(f"Heuristic patch applied to {agent_file}")

    return patched


def _claude_api_repair(source_name: str, html: str, agent_file: str, error: str) -> bool:
    """Use Claude API to analyze broken HTML and suggest selector fixes.

    Requires ANTHROPIC_API_KEY in .env. If not set, skips silently.
    Returns True if a fix was applied.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.debug("ANTHROPIC_API_KEY not set — skipping Claude-powered repair")
        return False

    try:
        import anthropic
    except ImportError:
        log.debug("anthropic package not installed — run: pip install anthropic")
        return False

    agent_path = _project_root() / agent_file
    if not agent_path.exists():
        return False

    agent_code = agent_path.read_text()

    # Truncate HTML for the prompt — keep the first 8k chars (enough for structure analysis)
    html_sample = html[:8000]

    prompt = f"""You are a web scraping repair agent. A Python scraper has broken.

Source: {source_name}
Error: {error}
Agent file: {agent_file}

Here is the current Python scraper code:
```python
{agent_code[:4000]}
```

Here is a sample of the CURRENT HTML from the source URL:
```html
{html_sample}
```

The scraper is no longer returning results. Likely the page structure changed.
Identify the broken CSS selectors or regex patterns in the code, and provide a minimal patch.
Reply ONLY with a JSON object in this exact format:
{{
  "can_fix": true/false,
  "patches": [
    {{"old": "original_selector_or_pattern", "new": "replacement"}}
  ],
  "explanation": "one sentence"
}}
If you cannot confidently determine a fix, set can_fix to false."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()

        # Parse the JSON response
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if not json_match:
            log.warning("Claude repair response did not contain valid JSON")
            return False

        import json
        result = json.loads(json_match.group(0))

        if not result.get("can_fix") or not result.get("patches"):
            log.info(f"Claude repair: cannot fix — {result.get('explanation', 'no explanation')}")
            return False

        new_code = agent_code
        for patch in result["patches"]:
            old_str = patch.get("old", "")
            new_str = patch.get("new", "")
            if old_str and new_str and old_str in new_code:
                new_code = new_code.replace(old_str, new_str)
                log.info(f"  Claude patch: '{old_str}' → '{new_str}'")

        if new_code != agent_code:
            agent_path.write_text(new_code)
            log.info(f"Claude-powered patch applied to {agent_file}: {result.get('explanation', '')}")
            return True

        return False

    except Exception as e:
        log.warning(f"Claude API repair failed: {e}")
        return False


def _verify_repair(source_name: str) -> bool:
    """Re-run the agent in test mode to verify the repair produced records.

    Runs: python agents/<agent>.py --county <county> --state OH --dry-run
    Returns True if the agent exits cleanly (exit code 0).
    """
    registry = SOURCE_REGISTRY.get(source_name, {})
    agent = registry.get("agent", "")
    if not agent:
        return False

    # Infer county and state from source name (e.g. "probate_cuyahoga" → "cuyahoga", "OH")
    parts = source_name.split("_")
    county = parts[-1] if len(parts) >= 2 else ""
    state = "OH"  # All current sources are Ohio

    agent_path = _project_root() / agent
    if not agent_path.exists():
        return False

    try:
        result = subprocess.run(
            [sys.executable, str(agent_path), "--county", county, "--state", state, "--dry-run"],
            timeout=120,
            capture_output=True,
            text=True,
            cwd=str(_project_root()),
        )
        if result.returncode == 0:
            log.info(f"Repair verification passed for {source_name}")
            return True
        else:
            log.warning(f"Repair verification failed for {source_name}: {result.stderr[:500]}")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"Repair verification timed out for {source_name}")
        return False
    except Exception as e:
        log.error(f"Repair verification error for {source_name}: {e}")
        return False


def attempt_repair(source_name: str, error: str = "") -> bool:
    """Attempt to auto-repair a broken scraper.

    Step 1: Fetch source URL fresh
    Step 2: Detect CAPTCHA/block (cannot auto-fix — needs owner)
    Step 3: Try heuristic selector repair
    Step 4: Try Claude API repair (if ANTHROPIC_API_KEY set)
    Step 5: Verify repair by running agent in dry-run mode

    Returns True if repair succeeded, False if manual review is needed.
    """
    log.info(f"Attempting auto-repair for source: {source_name}")
    start = time.time()

    registry = SOURCE_REGISTRY.get(source_name)
    if not registry:
        log.warning(f"Source '{source_name}' not in SOURCE_REGISTRY — cannot auto-repair")
        return False

    source_url = registry["url"]
    agent_file = registry["agent"]
    expected_pattern = registry.get("pattern", "")

    # Step 1: Fetch current HTML
    status_code, html = _fetch_source_html(source_url)

    if status_code == 0:
        log.error(f"Source URL unreachable: {source_url}")
        return False

    if status_code in (403, 429, 503):
        log.warning(f"Source returned {status_code} — likely blocked or rate-limited")
        return False

    # Step 2: Block/CAPTCHA detection
    block_reason = _detect_block(html)
    if block_reason:
        log.warning(f"Source appears blocked: {block_reason}")
        return False

    # Step 3: Verify expected data is still there
    if expected_pattern and not re.search(expected_pattern, html):
        log.warning(f"Expected data pattern not found in fresh HTML — structure likely changed")
    else:
        # Pattern is still there — the break may be a different issue (network, auth, etc.)
        log.info(f"Source HTML structure looks intact — break may not be selector-based")

    # Step 4a: Heuristic repair
    if time.time() - start < REPAIR_TIMEOUT_SEC:
        heuristic_fixed = _heuristic_selector_repair(source_name, html, agent_file)
        if heuristic_fixed and _verify_repair(source_name):
            log.info(f"Heuristic repair successful for {source_name}")
            return True

    # Step 4b: Claude API repair
    if time.time() - start < REPAIR_TIMEOUT_SEC:
        claude_fixed = _claude_api_repair(source_name, html, agent_file, error)
        if claude_fixed and _verify_repair(source_name):
            log.info(f"Claude API repair successful for {source_name}")
            return True

    elapsed = time.time() - start
    log.error(f"Auto-repair exhausted all strategies for {source_name} ({elapsed:.0f}s)")
    return False


def handle_failure(source_name: str, error: str) -> None:
    """Handle a scraper failure — log it, attempt repair if threshold crossed, escalate if needed."""
    log.warning(f"Scraper failure detected: {source_name} — {error}")

    insert_row("maintenance_log", {
        "event_type": "self_heal",
        "source_name": source_name,
        "description": f"Scraper failure: {error}",
        "resolved": False,
    })

    if not check_consecutive_failures(source_name):
        log.info(f"First failure for {source_name} — monitoring, not escalating yet")
        return

    repaired = attempt_repair(source_name, error)

    if repaired:
        insert_row("maintenance_log", {
            "event_type": "self_heal",
            "source_name": source_name,
            "description": "Auto-repair succeeded — scraper restored",
            "resolved": True,
        })
        log.info(f"Auto-repair succeeded for {source_name}")
        return

    # Escalate to owner
    client = get_client()
    sources = (
        client.table("sources")
        .select("id")
        .eq("source_name", source_name)
        .execute()
        .data
    )
    if sources:
        update_row("sources", sources[0]["id"], {
            "needs_manual_review": True,
            "blocked": True,
        })

    send_sms(
        f"[SCRAPER ALERT] {source_name} has failed 2+ consecutive runs "
        f"and could not be auto-repaired. Error: {error[:120]}. Manual review required."
    )

    insert_row("maintenance_log", {
        "event_type": "self_heal",
        "source_name": source_name,
        "description": "Auto-repair failed after all strategies — owner alerted, source flagged",
        "resolved": False,
    })

    log.error(f"Auto-repair exhausted for {source_name} — owner alerted via SMS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run self-healer for a specific source")
    parser.add_argument("--source", required=True, help="Source name (e.g. probate_cuyahoga)")
    parser.add_argument("--error", default="Manual test run", help="Error description")
    args = parser.parse_args()
    handle_failure(args.source, args.error)

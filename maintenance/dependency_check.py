from __future__ import annotations
"""
maintenance/dependency_check.py — Monthly check for outdated Python packages.

Runs on the 1st of every month before the monthly health report.
Flags outdated packages but NEVER auto-updates — owner approves manually.

CLI:
    python maintenance/dependency_check.py
"""

import subprocess

from db.client import insert_row
from utils.logger import get_logger

log = get_logger("maintenance.dependency_check")


def get_flagged_packages() -> list[str]:
    """Return a list of outdated package strings (name + versions).

    Uses pip list --outdated. Returns empty list on failure.
    """
    try:
        result = subprocess.run(
            ["pip3", "list", "--outdated", "--format=columns"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = result.stdout.strip().splitlines()
        # Skip header lines (first 2)
        packages = []
        for line in lines[2:]:
            parts = line.split()
            if len(parts) >= 3:
                name, current, latest = parts[0], parts[1], parts[2]
                packages.append(f"{name} {current} → {latest}")
        return packages
    except Exception as e:
        log.error(f"Failed to check outdated packages: {e}")
        return []


def run_dependency_check() -> list[str]:
    """Run the dependency check and log results to maintenance_log.

    Returns the list of flagged package strings.
    Never auto-updates. Flags are included in the monthly health report.
    """
    log.info("Running monthly dependency check")
    flagged = get_flagged_packages()

    if flagged:
        description = f"Outdated packages found ({len(flagged)}): " + "; ".join(flagged[:10])
        insert_row("maintenance_log", {
            "event_type": "dependency_flag",
            "source_name": None,
            "description": description,
            "resolved": False,
        })
        log.warning(f"Dependency check: {len(flagged)} outdated packages found")
        for pkg in flagged:
            log.warning(f"  Outdated: {pkg}")
    else:
        log.info("Dependency check: all packages up to date")

    return flagged


if __name__ == "__main__":
    packages = run_dependency_check()
    if packages:
        print(f"\n{len(packages)} outdated package(s) found:")
        for p in packages:
            print(f"  {p}")
        print("\nDo not auto-update. Review and approve manually.")
    else:
        print("All packages up to date.")

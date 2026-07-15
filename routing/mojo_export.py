"""
routing/mojo_export.py — Export Tier A/B/C leads to a Mojo-compatible CSV.

Generates a dated CSV file ready to drag into Mojo → Import Contacts.
Mojo column order: First Name, Last Name, Phone 1, Phone 2, Phone 3,
                   Address, City, State, Zip, Notes.

CLI:
    python routing/mojo_export.py                    # export all Tier A/B/C
    python routing/mojo_export.py --tier A B         # only Tier A and B
    python routing/mojo_export.py --new-only         # only leads not yet exported
    python routing/mojo_export.py --output ~/Desktop # custom output folder
"""

import argparse
import csv
import re
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from db.client import get_client
from utils.logger import get_logger

log = get_logger("routing.mojo_export")

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "exports"

# Regex to pull city, state, zip from a full US address string.
# Handles "123 Main St, Cleveland, OH 44101" and "123 Main St Cleveland OH 44101"
_CITY_STATE_ZIP = re.compile(
    r",?\s*([A-Za-z\s]+?)\s*,?\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$"
)


def _parse_address(full_address: str) -> tuple[str, str, str, str]:
    """Split a full address into (street, city, state, zip)."""
    if not full_address:
        return "", "", "", ""
    m = _CITY_STATE_ZIP.search(full_address)
    if m:
        street = full_address[: m.start()].strip().strip(",")
        city = m.group(1).strip()
        state = m.group(2).strip()
        zipcode = m.group(3).strip()
        return street, city, state, zipcode
    return full_address.strip(), "", "", ""


def export_leads(
    tiers: list[str] | None = None,
    new_only: bool = False,
    output_dir: Path | None = None,
) -> Path:
    """Query Supabase and write a Mojo-import CSV. Returns the output file path."""
    client = get_client()
    tiers = tiers or ["A", "B", "C"]
    output_dir = output_dir or DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    PAGE = 1000
    rows: list[dict] = []
    offset = 0
    while True:
        q = (
            client.table("raw_leads")
            .select(
                "id,owner_name,owner_first_name,owner_last_name,"
                "property_address,state,county,"
                "phone_1,phone_1_dnc,phone_2,phone_2_dnc,phone_3,"
                "litigator,score,tier,source_type,filing_date"
            )
            .in_("tier", tiers)
            .eq("routed_to_va", True)
            .neq("litigator", True)          # never export litigators
            .order("tier")
            .order("score", desc=True)
            .range(offset, offset + PAGE - 1)
        )
        if new_only:
            q = q.or_("mojo_exported.is.null,mojo_exported.eq.false")
        page = q.execute().data or []
        rows.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE

    log.info(f"Exporting {len(rows)} leads (tiers={tiers}, new_only={new_only})")

    tier_label = "".join(tiers)
    filename = f"mojo_export_{date.today().isoformat()}_tier{tier_label}.csv"
    output_path = output_dir / filename

    fieldnames = [
        "First Name", "Last Name",
        "Phone 1", "Phone 2", "Phone 3",
        "Address", "City", "State", "Zip",
        "Notes",
    ]

    exported_ids = []

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            street, city, state, zipcode = _parse_address(row.get("property_address") or "")
            if not state and row.get("state"):
                state = row["state"]

            first = row.get("owner_first_name") or ""
            last = row.get("owner_last_name") or row.get("owner_name") or ""

            # Strip DNC numbers — never dial a number on the DNC registry
            def _clean_phone(num, is_dnc) -> str:
                if is_dnc:
                    return ""
                return re.sub(r"\D", "", num or "")[-10:]

            phones = [
                _clean_phone(row.get("phone_1"), row.get("phone_1_dnc")),
                _clean_phone(row.get("phone_2"), row.get("phone_2_dnc")),
                re.sub(r"\D", "", row.get("phone_3") or "")[-10:],
            ]

            # Skip leads where every phone was stripped — nothing for the VA to dial
            if not any(phones):
                continue

            dnc_note = " | ⚠ Some phones DNC" if (
                row.get("phone_1_dnc") or row.get("phone_2_dnc")
            ) else ""
            notes = (
                f"Tier {row.get('tier')} | Score {row.get('score')} | "
                f"{row.get('source_type')} | {row.get('county')} County | "
                f"Filed {row.get('filing_date')}{dnc_note}"
            )

            writer.writerow({
                "First Name": first,
                "Last Name": last,
                "Phone 1": phones[0],
                "Phone 2": phones[1],
                "Phone 3": phones[2],
                "Address": street,
                "City": city,
                "State": state,
                "Zip": zipcode,
                "Notes": notes,
            })
            exported_ids.append(row["id"])

    log.info(f"Wrote {len(exported_ids)} rows → {output_path}")

    # Mark exported leads so --new-only skips them on future runs
    # Batch updates in chunks to avoid request limits
    chunk_size = 100
    for i in range(0, len(exported_ids), chunk_size):
        chunk = exported_ids[i : i + chunk_size]
        try:
            client.table("raw_leads").update({"mojo_exported": True}).in_("id", chunk).execute()
        except Exception as e:
            log.warning(f"Could not mark batch as exported: {e}")

    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export leads to Mojo CSV")
    parser.add_argument("--tier", choices=["A", "B", "C"], nargs="+", help="Tiers to export (default: A B C)")
    parser.add_argument("--new-only", action="store_true", help="Only export leads not yet exported")
    parser.add_argument("--output", help="Output directory (default: exports/)")
    args = parser.parse_args()

    output_dir = Path(args.output) if args.output else None
    path = export_leads(
        tiers=args.tier,
        new_only=args.new_only,
        output_dir=output_dir,
    )
    print(f"\nCSV ready: {path}")

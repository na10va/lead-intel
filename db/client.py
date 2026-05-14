from __future__ import annotations
"""
db/client.py — Supabase client wrapper

Usage:
    from db.client import get_client

    client = get_client()
    client.table("raw_leads").select("*").execute()

CLI test:
    python db/client.py --test
"""

import argparse
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()


def get_client() -> Client:
    """Return an authenticated Supabase client.

    Reads SUPABASE_URL and SUPABASE_KEY from the .env file.
    Raises EnvironmentError if either variable is missing.
    """
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")

    if not url:
        raise EnvironmentError("SUPABASE_URL is not set in .env")
    if not key:
        raise EnvironmentError("SUPABASE_KEY is not set in .env")

    return create_client(url, key)


def _enrich_name_fields(table: str, data: dict) -> dict:
    """Auto-populate owner_first_name / owner_last_name from owner_name if absent.

    Only runs for the raw_leads table. Does not overwrite values already set
    by the caller (e.g. Skip Sherpa api_owner_* fields should remain separate).
    """
    if table != "raw_leads":
        return data
    if "owner_name" not in data:
        return data
    if data.get("owner_first_name") or data.get("owner_last_name"):
        return data  # caller already split it
    try:
        from utils.name_splitter import split_into_fields
        data = {**data, **split_into_fields(data["owner_name"], data.get("source_type", ""))}
    except Exception:
        pass  # name splitting is best-effort; never block an insert
    return data


def insert_row(table: str, data: dict) -> dict:
    """Insert a single row into a Supabase table.

    Auto-populates owner_first_name / owner_last_name for raw_leads rows.
    Returns the inserted row as a dict.
    Raises on Supabase errors.
    """
    data = _enrich_name_fields(table, data)
    client = get_client()
    response = client.table(table).insert(data).execute()
    return response.data[0] if response.data else {}


def select_rows(table: str, filters: Optional[dict] = None) -> list[dict]:
    """Select rows from a Supabase table with optional equality filters.

    Args:
        table:   Table name.
        filters: Dict of column → value equality conditions, e.g.
                 {"county": "Cuyahoga", "verified_raw": True}

    Returns a list of row dicts.
    """
    client = get_client()
    query = client.table(table).select("*")

    if filters:
        for column, value in filters.items():
            query = query.eq(column, value)

    response = query.execute()
    return response.data or []


def update_row(table: str, row_id: str, updates: dict) -> dict:
    """Update a single row by UUID primary key.

    Returns the updated row as a dict.
    """
    client = get_client()
    response = (
        client.table(table)
        .update(updates)
        .eq("id", row_id)
        .execute()
    )
    return response.data[0] if response.data else {}


def upsert_row(table: str, data: dict, on_conflict: str = "id") -> dict:
    """Insert or update a row based on a conflict column.

    Auto-populates owner_first_name / owner_last_name for raw_leads rows.

    Args:
        table:       Table name.
        data:        Row data dict.
        on_conflict: Column name to check for conflicts (default: "id").

    Returns the upserted row as a dict.
    """
    data = _enrich_name_fields(table, data)
    client = get_client()
    response = (
        client.table(table)
        .upsert(data, on_conflict=on_conflict)
        .execute()
    )
    return response.data[0] if response.data else {}


# ---------------------------------------------------------------------------
# CLI test mode:  python db/client.py --test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Supabase connection")
    parser.add_argument("--test", action="store_true", help="Run connection test")
    args = parser.parse_args()

    if args.test:
        print("Testing Supabase connection...")
        try:
            client = get_client()
            # A lightweight query — just fetch 1 row from raw_leads to confirm connectivity
            response = client.table("raw_leads").select("id").limit(1).execute()
            print(f"Connection successful. raw_leads accessible.")
            sys.exit(0)
        except Exception as e:
            print(f"Connection failed: {e}")
            sys.exit(1)

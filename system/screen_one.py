#!/usr/bin/env python3
"""Screen one person against every watchlist profile.

    python3 system/screen_one.py <data folder> --name "Elvira Velasco Morillo" --dob 1968-03-05 --country Spain

Prints one JSON object. The verdict is PASS, ESCALATE, or NEEDS_ANALYST.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from screen.engine import World


def main():
    parser = argparse.ArgumentParser(description="Screen one customer against the watchlist profiles in a data folder.")
    parser.add_argument("data", help="folder of JSONL files, or a .xlsx workbook")
    parser.add_argument("--name", required=True, help="full name")
    parser.add_argument("--aliases", default="", help="comma-separated aliases")
    parser.add_argument("--type", default="Individual", choices=["Individual", "Company"])
    parser.add_argument("--dob", default="", help="date of birth or incorporation date, full or a year")
    parser.add_argument("--country", default="", help="countries, nationalities, or document countries, comma-separated")
    parser.add_argument("--email", default="")
    parser.add_argument("--phone", default="")
    args = parser.parse_args()
    folder = args.data
    if os.path.isfile(folder) and folder.lower().endswith((".xlsx", ".xlsm")):
        import tempfile
        from screen.convert import convert

        folder = tempfile.mkdtemp(prefix="screening-")
        convert(args.data, folder)
    world = World(folder)
    result = world.screen_customer({
        "customer_name": args.name,
        "aliases": args.aliases,
        "customer_type": args.type,
        "customer_dob": args.dob,
        "customer_countries": args.country,
        "email": args.email,
        "phone": args.phone,
    })
    # The trace steps are for the page. The CLI prints the decision.
    for candidate in result.get("candidates") or []:
        candidate.pop("steps", None)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

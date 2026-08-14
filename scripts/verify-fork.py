#!/usr/bin/env python3
"""Verify a Grok Build fork binary against the adapter release lock."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from fork_contract import ContractError, verify_fork


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--lock", required=True)
    args = parser.parse_args()
    try:
        result = verify_fork(Path(args.binary), Path(args.lock))
    except (ContractError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

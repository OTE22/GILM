import argparse
import json
from pathlib import Path

from gilm.backup import backup, restore

parser = argparse.ArgumentParser(description="Verified SQLite backup or restore into a new directory")
parser.add_argument("command", choices=["backup", "restore"])
parser.add_argument("--source", required=True, type=Path)
parser.add_argument("--destination", required=True, type=Path)
args = parser.parse_args()
print(json.dumps((backup if args.command == "backup" else restore)(args.source, args.destination), indent=2))

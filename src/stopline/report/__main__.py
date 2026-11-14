import argparse
import sys
from pathlib import Path

from pymongo.errors import PyMongoError

from stopline import db as storage

from . import build_report, render_report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reconstruct a saved run as a printable HTML page.")
    parser.add_argument("run_id", nargs="?", help="run ID; defaults to the most recent run")
    parser.add_argument("--out", type=Path, help="write HTML to this file instead of stdout")
    args = parser.parse_args(argv)
    try:
        db = storage.database()
        run_id = args.run_id
        if run_id is None:
            latest = db.runs.find_one(sort=[("created_at", -1), ("_id", -1)])
            if latest is None:
                print("No runs found", file=sys.stderr)
                return 1
            run_id = latest["_id"]
        html = render_report(build_report(db, run_id))
        if args.out:
            args.out.write_text(html, encoding="utf-8")
        else:
            print(html)
        return 0
    except LookupError:
        print("Run not found", file=sys.stderr)
    except (PyMongoError, RuntimeError, OSError) as error:
        # Driver messages can contain connection details; print only the type.
        print(f"Could not read or write the saved run ({type(error).__name__})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

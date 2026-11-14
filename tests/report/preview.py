"""Write local HTML fixtures using a temporary database, then drop it.

Run from the repository root: uv run python -m tests.report.preview
"""

from pathlib import Path

from stopline import db as storage
from stopline.report import build_report, render_report

from .records import RUN, insert_demo


def main():
    directory = Path(__file__).resolve().parents[2] / ".preview"
    directory.mkdir(exist_ok=True)
    db = storage.test_database("report")
    try:
        insert_demo(db)
        report = build_report(db, RUN)
        (directory / "demo.html").write_text(render_report(report), encoding="utf-8")
        db.runs.update_one({"_id": RUN}, {"$set": {"mode": "flag", "root_mandate_id": None, "agent_mandate_id": None, "receipt_id": None}})
        for name in ("mandates", "audit", "receipts", "policies"):
            db[name].delete_many({})
        db.effects.update_many({}, {"$set": {"mode": "flag", "root_id": None, "mandate_id": None, "chain": [], "admitted_seq": None, "policy_version": None, "policy_hash": None}})
        (directory / "baseline.html").write_text(render_report(build_report(db, RUN)), encoding="utf-8")
        print("Wrote .preview/demo.html and .preview/baseline.html; temporary database dropped on exit.")
    finally:
        storage.drop_test_database(db)


if __name__ == "__main__":
    main()

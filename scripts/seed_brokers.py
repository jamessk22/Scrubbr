"""Import data/brokers.json into the SQLite broker registry.

Idempotent: run any time to add new brokers or refresh existing ones
(matched by unique name). Usage: python -m scripts.seed_brokers
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import ROOT  # noqa: E402


def main() -> None:
    payload = json.loads((ROOT / "data" / "brokers.json").read_text())
    brokers = payload["brokers"]
    conn = db.connect()
    db.init_db(conn)
    for b in brokers:
        scan = b.pop("scan", None)
        row = {**b, "scan_config": json.dumps(scan) if scan else "", "network": b.get("network") or ""}
        db.upsert_broker(conn, row)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) AS n FROM brokers").fetchone()["n"]
    print(f"Imported/updated {len(brokers)} brokers. Registry now holds {total}.")


if __name__ == "__main__":
    main()

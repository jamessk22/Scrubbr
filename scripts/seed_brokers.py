"""Import data/brokers.json (plus the gitignored data/brokers.local.json, if
present) into the SQLite broker registry.

Idempotent: run any time to add new brokers or refresh existing ones
(matched by unique name). Usage: python -m scripts.seed_brokers
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import ROOT  # noqa: E402


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def load_brokers(root: Path) -> tuple[list[dict], int]:
    """Shared registry brokers followed by local ones, and the local count.
    A local name that normalizes to an existing one exits rather than
    silently overriding the shared entry."""
    brokers = json.loads((root / "data" / "brokers.json").read_text())["brokers"]
    local_path = root / "data" / "brokers.local.json"
    if not local_path.exists():
        return brokers, 0
    local = json.loads(local_path.read_text())["brokers"]
    seen = {_norm(b["name"]): b["name"] for b in brokers}
    for b in local:
        n = _norm(b["name"])
        if n in seen:
            sys.exit(f"brokers.local.json entry {b['name']!r} collides with existing broker {seen[n]!r}")
        seen[n] = b["name"]
    return brokers + local, len(local)


def main() -> None:
    brokers, local_count = load_brokers(ROOT)
    conn = db.connect()
    db.init_db(conn)
    for b in brokers:
        scan = b.pop("scan", None)
        row = {**b, "scan_config": json.dumps(scan) if scan else "", "network": b.get("network") or ""}
        db.upsert_broker(conn, row)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) AS n FROM brokers").fetchone()["n"]
    print(f"Imported/updated {len(brokers)} brokers ({local_count} from brokers.local.json). Registry now holds {total}.")


if __name__ == "__main__":
    main()

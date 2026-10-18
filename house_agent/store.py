"""SQLite record of every listing we've seen, and whether it's still online."""
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from house_agent.models import Listing

DB_PATH = Path(".state/listings.sqlite")


class Store:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS listings (
                key TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )"""
        )

    def sync(self, site: str, listings: list[Listing]) -> tuple[list[Listing], list[Listing]]:
        """Record the current listings of one site. Returns (new, gone) compared to its last sync.

        Per site, so a site that failed to load doesn't make its listings look "gone".
        """
        now = datetime.now().isoformat(timespec="seconds")
        current = {l.key: l for l in listings}
        known = {
            k: (json.loads(d), bool(a))
            for k, d, a in self.db.execute("SELECT key, data, active FROM listings WHERE key LIKE ?", (f"{site}:%",))
        }

        new = [l for k, l in current.items() if k not in known or not known[k][1]]
        gone = [Listing(**{**d, "target_groups": tuple(d["target_groups"])})
                for k, (d, active) in known.items() if active and k not in current]

        with self.db:
            for k, l in current.items():
                self.db.execute(
                    """INSERT INTO listings (key, data, first_seen, last_seen, active) VALUES (?, ?, ?, ?, 1)
                       ON CONFLICT(key) DO UPDATE SET data = excluded.data, last_seen = excluded.last_seen, active = 1""",
                    (k, json.dumps(asdict(l)), now, now),
                )
            for l in gone:
                self.db.execute("UPDATE listings SET active = 0 WHERE key = ?", (l.key,))
        return new, gone

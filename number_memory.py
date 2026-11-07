import re
import sqlite3
from contextlib import closing
from pathlib import Path


STORE_PAIR = re.compile(
    r"^\s*please remember this number pair for future reference:\s*"
    r"([0-9]{1,5})\s+and\s+([0-9]{1,5})\b",
    re.IGNORECASE,
)
RECALL_PAIR = re.compile(r"\bwhat number was paired with\s+([0-9]{1,5})\b", re.IGNORECASE)


class NumberMemory:
    def __init__(self, path: Path) -> None:
        self.path = path

    def answer(self, text: str) -> str | None:
        store = STORE_PAIR.search(text)
        recall = RECALL_PAIR.search(text)
        if not store and not recall:
            return None

        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS number_pairs "
                "(number INTEGER PRIMARY KEY, partner INTEGER NOT NULL)"
            )
            if store:
                first, second = map(int, store.groups())
                db.executemany(
                    "INSERT INTO number_pairs (number, partner) VALUES (?, ?) "
                    "ON CONFLICT(number) DO UPDATE SET partner = excluded.partner",
                    [(first, second), (second, first)],
                )
                return "I saved the number pair."

            number = int(recall.group(1))
            row = db.execute(
                "SELECT partner FROM number_pairs WHERE number = ?", (number,)
            ).fetchone()
            return str(row[0]) if row else "I have no saved pair for that number."

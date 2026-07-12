"""Run ordered, lock-protected control-tower database migrations."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db


def main() -> None:
    # engine() runs the same migrator before returning. Calling it again proves the
    # command is idempotent and reports the resulting ordered version set.
    bind = db.engine()
    db.migrate_engine(bind)
    with bind.begin() as cx:
        rows = cx.execute(db.select(db.schema_migrations).order_by(
            db.schema_migrations.c.version)).mappings().all()
    print("control-tower schema versions: " + ", ".join(
        f"{row['version']}:{row['name']}" for row in rows))


if __name__ == "__main__":
    main()

"""Entry point: ``python -m pretrade`` starts the HTTP service.

The SQLite database is created and seeded on first run. Configure its location
with DATA_DIR / DB_PATH, and the port with PORT.
"""

from __future__ import annotations

import os

from .service import serve
from .store import Store


def main() -> None:
    data_dir = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
    db_path = os.environ.get("DB_PATH", os.path.join(data_dir, "pretrade.db"))
    store = Store(db_path)
    store.seed_if_empty()
    serve(store, port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()

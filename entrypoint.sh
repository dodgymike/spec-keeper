#!/bin/sh
# Wait for Postgres, ensure the schema exists, then run the given command.
set -e

echo "[entrypoint] waiting for the database..."
python - <<'PY'
import os, time
import sqlalchemy as sa

url = os.environ.get("DATABASE_URL", "postgresql+psycopg://spec:spec@db:5432/specserver")
engine = sa.create_engine(url)
for attempt in range(60):
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        print("[entrypoint] database is up.")
        break
    except Exception as exc:  # noqa: BLE001
        print(f"[entrypoint] db not ready ({attempt}): {exc}")
        time.sleep(1)
else:
    raise SystemExit("[entrypoint] database never became ready")
PY

echo "[entrypoint] ensuring schema (flask init-db)..."
FLASK_APP=wsgi:app flask init-db

echo "[entrypoint] starting: $*"
exec "$@"

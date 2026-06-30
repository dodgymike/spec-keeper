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

echo "[entrypoint] applying migrations (alembic upgrade head)..."
# Adopt a legacy DB that was built by create_all (tables exist, but no
# alembic_version table) by stamping it at head before upgrading.
python - <<'PY'
import os, subprocess
import sqlalchemy as sa

engine = sa.create_engine(os.environ["DATABASE_URL"])
tables = set(sa.inspect(engine).get_table_names())
if "alembic_version" not in tables and "projects" in tables:
    print("[entrypoint] legacy create_all schema detected; stamping head")
    subprocess.run(["alembic", "stamp", "head"], check=True)
PY
alembic upgrade head

echo "[entrypoint] starting: $*"
exec "$@"

#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec -T app python -c '
import datetime, json, os, sqlite3
from pathlib import Path
p = Path(os.environ["DATA_DIR"]) / "quota.sqlite3"
if not p.exists():
    raise SystemExit("No quota database yet; request /api/quota first.")
with sqlite3.connect(p.as_uri() + "?mode=ro", uri=True) as c:
    day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    row = c.execute("SELECT usd FROM budget WHERE day = ?", (day,)).fetchone()
    used = float(row[0]) if row else 0.0
limit = float(os.environ.get("DAILY_BUDGET_USD", "0.50"))
print(json.dumps({"day_utc": day, "estimated_usd": round(used, 6), "limit_usd": limit, "remaining_estimated_usd": round(max(0, limit-used), 6)}))
'

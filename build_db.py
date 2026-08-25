"""Build (or rebuild) the persistent DuckDB database `data.duckdb` from the CSVs
in ./data. Run this whenever the source data changes:  python build_db.py
"""
from app import build_db, DB_PATH
import duckdb

build_db()

# Show what's in the database so it's easy to confirm.
con = duckdb.connect(DB_PATH, read_only=True)
rows = con.execute("""
    SELECT table_name,
           (SELECT count(*) FROM information_schema.columns c WHERE c.table_name = t.table_name) AS cols
    FROM information_schema.tables t
    WHERE table_schema='main' ORDER BY table_name
""").fetchall()
print("\nTables in data.duckdb:")
for name, cols in rows:
    n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
    print(f"  • {name:20s} {n:>6d} rows  ·  {cols} cols")
con.close()

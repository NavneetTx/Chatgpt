"""Demonstrate the self-healing SQL retry.

We replace Claude with a fake that returns a BROKEN query on the first attempt
(references a column that doesn't exist) and a correct one on the second. The
retry loop in app.ask() should catch the DuckDB error, feed it back, and recover.

Run:  python test_selfheal.py
(Make sure no read-write duckdb CLI session is holding data.duckdb — close it,
 or open it with `duckdb -readonly data.duckdb`.)
"""
import secrets
from types import SimpleNamespace
import app

calls = {"n": 0}


class FakeResp:
    def __init__(self, text):
        self.content = [SimpleNamespace(text=text)]


class FakeMessages:
    def create(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            print("  → Claude (attempt 1): returns SQL using a non-existent column 'bogus_col'")
            return FakeResp('{"sql":"SELECT bogus_col, SUM(revenue_usd) AS revenue '
                            'FROM fact_revenue GROUP BY bogus_col","chart_type":"bar",'
                            '"x":"bogus_col","y":"revenue","title":"x","explanation":"e"}')
        print("  → Claude (attempt 2): returns corrected SQL after seeing the error")
        return FakeResp('{"sql":"SELECT region, SUM(revenue_usd) AS revenue FROM fact_revenue '
                        'GROUP BY region ORDER BY revenue DESC","chart_type":"bar","x":"region",'
                        '"y":"revenue","title":"Revenue by region","explanation":"Total USD revenue by region."}')


class FakeClient:
    messages = FakeMessages()


app.client = FakeClient()
token = secrets.token_urlsafe(8)
app.SESSIONS[token] = "marcus"

print("\nAsking: 'revenue by region'  (first SQL is intentionally broken)\n")
res = app.ask(app.Query(question="revenue by region"), authorization="Bearer " + token)

print("\n── RESULT ──────────────────────────────")
print(f"  Claude calls made : {calls['n']}")
print(f"  attempts reported : {res.get('attempts')}")
print(f"  error shown to user: {res.get('error')}")
print(f"  final working SQL : {res.get('code')}")
print(f"  rows (first 2)    : {res.get('rows', [])[:2]}")
print("────────────────────────────────────────")
ok = calls["n"] == 2 and res.get("attempts") == 2 and not res.get("error")
print("✅ SELF-HEALING WORKS" if ok else "❌ something is off")

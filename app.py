"""
House of Colour — AI report backend.

Storage:  DuckDB. Every CSV in /data is exposed as a queryable table.
Brain:    Claude turns a question (+ schema) into a single read-only SQL SELECT.
Security: role-based access. Admins see everything; a stylist is scoped to their
          own rows. Enforced by materializing SCOPED tables per request (row-level
          security) and disabling external file access before the generated SQL runs.

Serves both:
  /          -> the simple csv_chatbot UI (static/index.html)
  /portal    -> the House of Colour portal (HOC_Portal_Demo_final (1).html)
  /api/*     -> login / ask / tables
"""
import os
import re
import io
import csv
import glob
import json
import time
import secrets
from collections import defaultdict, deque
import smtplib
import traceback
from typing import List
from datetime import datetime, timezone
from email.message import EmailMessage

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from apscheduler.schedulers.background import BackgroundScheduler

import duckdb
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import anthropic

load_dotenv(os.path.join(os.path.dirname(__file__), "env.local"))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(os.path.dirname(__file__), "data.duckdb")   # the persistent database
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")
MAX_ROWS = 2000
SQL_MAX_ATTEMPTS = 3   # 1 initial try + up to 2 self-healing retries

# House of Colour portal HTML (served at /portal). Ships inside the repo at
# static/portal.html; override with the PORTAL_HTML env var to point elsewhere.
PORTAL_HTML = os.getenv("PORTAL_HTML") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "static", "portal.html")

app = FastAPI(title="HOC AI Report Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=False)

# ---------------------------------------------------------------------------
# Demo user directory. Each user has an optional row-level scope (col, val).
# In production this is Entra ID + Fabric/OneLake RLS — not a plaintext table.
# ---------------------------------------------------------------------------
USERS = {
    "marcus": {"password": "admin123",  "name": "Marcus King",     "role": "admin",
               "scope_col": None,            "scope_val": None},   # HQ admin — full access
    "admin":  {"password": "admin123",  "name": "HQ Admin",        "role": "admin",
               "scope_col": None,            "scope_val": None},
    "sophie": {"password": "sophie123", "name": "Sophie Marlowe",  "role": "member",
               "scope_col": "ConsultantID", "scope_val": "CON001"},   # London North
    "claire": {"password": "claire123", "name": "Claire Hughes",   "role": "member",
               "scope_col": "ConsultantID", "scope_val": "CON003"},   # Manchester
    "navneet": {"password": "navneet123", "name": "Navneet Virdi",   "role": "admin",
               "scope_col": None,            "scope_val": None},   # HQ admin — full access
}
SESSIONS = {}  # token -> username  (in-memory; fine for a single-process demo)

# Per-user bookmarks, persisted to a JSON file:  { username: [ {id,title,kind,created,data} ] }
BOOKMARKS_PATH = os.path.join(os.path.dirname(__file__), "bookmarks.json")


def _load_bookmarks():
    try:
        with open(BOOKMARKS_PATH) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_bookmarks():
    try:
        with open(BOOKMARKS_PATH, "w") as f:
            json.dump(BOOKMARKS, f)
    except Exception as e:  # noqa: BLE001
        print(f"[bookmarks] save failed: {e}")


BOOKMARKS = _load_bookmarks()

# ---------------------------------------------------------------------------
# Scheduled email reports.  Schedules are persisted per user; a background
# scheduler fires them monthly and emails a rendered report via SMTP (Mailpit
# in this POC: localhost:1025, web inbox at http://localhost:8025).
# ---------------------------------------------------------------------------
SCHEDULES_PATH = os.path.join(os.path.dirname(__file__), "schedules.json")
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
MAIL_FROM = os.getenv("MAIL_FROM", "House of Colour Reports <reports@houseofcolour.demo>")
PALETTE = ["#E26D5A", "#2C8C99", "#C9A227", "#8E4585", "#4A90C2", "#5B8C5A"]

scheduler = BackgroundScheduler()


def _load_schedules():
    try:
        with open(SCHEDULES_PATH) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_schedules():
    try:
        with open(SCHEDULES_PATH, "w") as f:
            json.dump(SCHEDULES, f)
    except Exception as e:  # noqa: BLE001
        print(f"[schedules] save failed: {e}")


SCHEDULES = _load_schedules()


def all_schedules():
    return [s for lst in SCHEDULES.values() for s in lst]


def build_db():
    """(Re)build the persistent DuckDB database from the CSVs in /data."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = duckdb.connect(DB_PATH)
    loaded = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*.csv"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            print(f"Skipping {path}: invalid table name.")
            continue
        con.execute(f"CREATE OR REPLACE TABLE {name} AS "
                    f"SELECT * FROM read_csv_auto('{path.replace(chr(39), chr(39)*2)}')")
        loaded.append(name)
    con.close()
    print(f"Built {DB_PATH} with tables: {', '.join(loaded)}")


def ensure_db():
    if not os.path.exists(DB_PATH):
        build_db()


def discover_tables():
    """Read the table list + columns from the persistent database (not the CSVs)."""
    ensure_db()
    con = duckdb.connect(DB_PATH, read_only=True)
    names = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
    ).fetchall()]
    tables = {}
    for n in names:
        cols = con.execute(f"DESCRIBE {n}").fetchall()
        tables[n] = {"columns": [(c[0], c[1]) for c in cols]}
    con.close()
    return tables


TABLES = discover_tables()


def scoped_connection(user):
    """Fresh DuckDB connection holding only rows this user may see.

    1. Each table is materialized, filtered to the user's scope (row-level).
    2. External file access is then disabled, so the generated SQL cannot read
       files off the server even if a bug let such a call past validation.
    """
    con = duckdb.connect()   # in-memory working connection
    con.execute(f"ATTACH '{DB_PATH.replace(chr(39), chr(39)*2)}' AS src (READ_ONLY)")
    col = None if user["role"] == "admin" else user.get("scope_col")
    val = None if user["role"] == "admin" else user.get("scope_val")
    for name, meta in TABLES.items():
        cols = [c[0] for c in meta["columns"]]
        if col and val and col in cols:
            v = str(val).replace("'", "''")
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM src.{name} WHERE {col} = '{v}'")
        else:
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM src.{name}")
    con.execute("DETACH src")
    con.execute("SET enable_external_access=false")   # lock the door behind us
    return con


def schema_text(con):
    if not TABLES:
        return "(no CSV files found in the data/ folder)"
    parts = []
    for name in TABLES:
        desc = con.execute(f"DESCRIBE {name}").fetchall()
        cols = ", ".join(f"{d[0]} ({d[1]})" for d in desc)
        sample = con.execute(f"SELECT * FROM {name} LIMIT 2").df().to_dict(orient="records")
        n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        parts.append(f"Table `{name}`  (rows={n})\n  columns: {cols}\n  sample: {sample}")
    return "\n\n".join(parts)


SYSTEM_PROMPT = """You are a data analyst for House of Colour working over a DuckDB database.

Given a user's natural-language question and the table schema, respond with ONLY a \
JSON object (no markdown fences, no prose) of this exact shape:

{
  "sql": "<a SINGLE read-only DuckDB SELECT statement that answers the question>",
  "chart_type": "bar" | "grouped_bar" | "stacked_bar" | "line" | "pie" | "doughnut" | "scatter" | "table",
  "x": "<result column for the x axis / labels>",
  "y": "<result column for the numeric value>",
  "series": "<OPTIONAL result column to split into multiple series (for grouped_bar / stacked_bar / multi-series line); omit or null if single series>",
  "title": "<short chart title>",
  "explanation": "<one or two sentence plain-English answer>"
}

Rules:
- `sql` MUST be one statement starting with SELECT or WITH. No semicolons, no DDL/DML, \
no ATTACH/COPY/INSTALL/PRAGMA, no reading files — query only the tables in the schema.
- Alias aggregates with clear names and reference those exact names in x and y \
(e.g. SELECT region, SUM(revenue) AS revenue ... -> x="region", y="revenue").
- Use the EXACT chart type the user asks for. 'pie' and 'doughnut' are DIFFERENT — return \
whichever they say. 'stacked_bar' stacks the series; 'grouped_bar' puts them side by side.
- For a measure broken down across TWO dimensions (e.g. "monthly revenue by region"), set \
x to the main axis (month), y to the measure, and `series` to the breakdown dimension \
(region). The SQL MUST GROUP BY both and return the x, series and y columns. Pick \
chart_type 'stacked_bar' when the user says "stacked", otherwise 'grouped_bar'. If the \
series has many values, limit to a sensible top-N.
- Revenue/amount columns are in GBP (suffix _GBP). Join fact tables to dimension tables \
(dim_region, dim_consultant, dim_service, dim_product, dim_colour_season) for readable \
labels instead of showing raw IDs.
- IMPORTANT: most fact tables already carry their own time columns (Year, MonthNum, \
MonthName, QuarterYear). Group by those directly for monthly/quarterly/yearly aggregates. \
Do NOT join monthly/summary facts to dim_date (it is a DAILY calendar) — that fans out rows \
and multiplies the totals.
- Keep the result small (aggregate / top-N) — it is meant to be charted.
- The data may already be filtered to the user's permitted scope. Answer only from what \
the schema shows; never claim figures are company-wide unless all groups appear.
- For a follow-up that refines a previous question, apply the refinement to that prior intent.
- If the question can't be answered, return chart_type "table" with a SELECT that returns \
a single explanatory row.
"""

_SQL_BANNED = re.compile(
    r"\b(attach|copy|install|load|pragma|create|insert|update|delete|drop|alter|"
    r"export|set|call)\b|read_\w+|\w*_scan\b|\bglob\b|(?:from|join)\s+'", re.IGNORECASE)


def safe_select(sql):
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise ValueError("Only a single statement is allowed.")
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        raise ValueError("Only SELECT queries are allowed.")
    if _SQL_BANNED.search(s):
        raise ValueError("Query uses a disallowed keyword.")
    return s


client = anthropic.Anthropic() if os.getenv("ANTHROPIC_API_KEY") else None

ANALYSIS_PROMPT = """You are a sharp BI analyst for House of Colour (a personal-styling \
franchise). Given a question and the result rows, return ONLY a JSON object:

{
  "insights": ["2-4 short factual observations using the ACTUAL numbers (biggest/smallest, gaps, ratios, outliers); max ~16 words each"],
  "recommendations": ["1-3 concrete next actions a manager or stylist could take based on this data; start with a verb; max ~16 words each"],
  "forecast": "1-2 sentence forward-looking, qualitative outlook — ONLY if the data shows a trend over time or progress toward a target; otherwise an empty string"
}

Be specific and grounded in the data. No preamble, no markdown, no extra keys."""

_EMPTY_ANALYSIS = {"insights": [], "recommendations": [], "forecast": ""}


def generate_analysis(question, df):
    """Second pass: read the actual result rows and produce takeaways,
    recommended actions, and a qualitative forecast. Best-effort."""
    if client is None or df is None or len(df) == 0:
        return dict(_EMPTY_ANALYSIS)
    rows = df.head(50).to_dict(orient="records")
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=700, system=ANALYSIS_PROMPT,
            messages=[{"role": "user",
                       "content": f"Question: {question}\n\nResult rows (JSON):\n{rows}\n\nReturn the JSON."}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        d = json.loads(raw)
        clean = lambda lst, n: [str(x).strip() for x in (lst or []) if str(x).strip()][:n]
        return {
            "insights": clean(d.get("insights"), 4),
            "recommendations": clean(d.get("recommendations"), 3),
            "forecast": str(d.get("forecast", "")).strip(),
        }
    except Exception as e:  # noqa: BLE001 — best-effort, never block the answer
        print(f"[analysis] failed: {e}")
        return dict(_EMPTY_ANALYSIS)


# ---------------------------------------------------------------------------
# Research mode: an agent loop where Claude can use BOTH the internal DB and
# live web search, then synthesise a cited answer.
# ---------------------------------------------------------------------------
INTERNAL_DB_TOOL = {
    "name": "query_internal_db",
    "description": "Run ONE read-only DuckDB SELECT against House of Colour's own data "
                   "(already filtered to the signed-in user's permitted scope) and get rows back as JSON.",
    "input_schema": {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "A single read-only SELECT for DuckDB."}},
        "required": ["sql"],
    },
}
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}

RESEARCH_SYSTEM = """You are a senior analyst for House of Colour, a personal-styling franchise. \
You have two tools:
- query_internal_db: the company's OWN data (DuckDB). It is already filtered to the signed-in \
user's permitted scope — just query what you need.
- web_search: the public web, for external market / industry / economic / benchmark data and facts.

Use internal data for the company's own numbers; use web_search for outside context.

ANSWER FORMAT — keep it SHORT and skimmable (aim ~120-150 words, never a long essay):
1. One or two sentences: the direct answer / key finding first.
2. If you compare figures, show a small markdown table — use standard pipes with a separator row, \
e.g.  | Metric | HOC | Industry |  then  |---|---|---|  then the rows.
3. At most 3-4 short bullets of key context.
4. A final line starting with "Bottom line:" (one sentence) or 1-2 next actions.
Lead with the answer, use concrete numbers, ALWAYS cite web sources, never invent figures. \
Avoid repetition, multiple headings, and filler.

DuckDB schema available via query_internal_db:
{schema}
"""


def _research_sources(msg):
    out = []
    for b in msg.content:
        t = getattr(b, "type", "")
        if t == "web_search_tool_result":
            for r in (getattr(b, "content", None) or []):
                url = getattr(r, "url", None)
                if url:
                    out.append({"url": url, "title": getattr(r, "title", None) or url})
        elif t == "text":
            for c in (getattr(b, "citations", None) or []):
                url = getattr(c, "url", None)
                if url:
                    out.append({"url": url, "title": getattr(c, "title", None) or url})
    return out


def run_research(question, con):
    """Agent loop: Claude alternates between web_search (server-side) and our
    query_internal_db (client tool) until it can answer, with citations."""
    system = RESEARCH_SYSTEM.replace("{schema}", schema_text(con))
    messages = [{"role": "user", "content": question}]
    sources, last_sql, answer = [], None, ""
    for _turn in range(6):
        msg = client.messages.create(
            model=MODEL, max_tokens=2000, system=system,
            messages=messages, tools=[INTERNAL_DB_TOOL, WEB_SEARCH_TOOL],
        )
        sources += _research_sources(msg)
        if msg.stop_reason != "tool_use":
            answer = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
            break
        messages.append({"role": "assistant", "content": msg.content})
        results = []
        for b in msg.content:
            if getattr(b, "type", "") == "tool_use" and b.name == "query_internal_db":
                sql = (b.input or {}).get("sql", "")
                try:
                    s = safe_select(sql); last_sql = s
                    df = con.execute(f"SELECT * FROM ({s}) AS _q LIMIT {MAX_ROWS}").df()
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(df.head(100).to_dict(orient="records"), default=str)})
                except Exception as e:  # noqa: BLE001
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": f"Query error: {e}", "is_error": True})
        if not results:   # only server tools ran but it still paused — stop safely
            answer = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
            break
        messages.append({"role": "user", "content": results})

    seen, uniq = set(), []
    for s in sources:
        if s["url"] not in seen:
            seen.add(s["url"]); uniq.append(s)
    return {"research": True, "answer": answer or "(no answer produced)",
            "sources": uniq[:8], "code": last_sql}


# ---------------------------------------------------------------------------
# Scheduled-report rendering + email
# ---------------------------------------------------------------------------
def render_chart_png(chart_type, labels, values, title):
    labels = [str(x) for x in labels]
    values = [float(v or 0) for v in values]
    fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=130)
    colors = [PALETTE[i % len(PALETTE)] for i in range(len(labels))]
    if chart_type in ("pie", "doughnut"):
        ax.pie(values, labels=labels, colors=colors, autopct="%1.0f%%", startangle=90,
               textprops={"fontsize": 8})
        if chart_type == "doughnut":
            ax.add_artist(plt.Circle((0, 0), 0.62, color="white"))
        ax.axis("equal")
    elif chart_type == "line":
        ax.plot(labels, values, color="#E26D5A", marker="o", linewidth=2)
        ax.fill_between(range(len(values)), values, color="#E26D5A", alpha=0.12)
        ax.grid(axis="y", color="#EDE7DF"); [ax.spines[s].set_visible(False) for s in ("top", "right")]
        plt.xticks(rotation=20, ha="right", fontsize=8)
    else:
        ax.bar(labels, values, color=colors)
        ax.grid(axis="y", color="#EDE7DF"); [ax.spines[s].set_visible(False) for s in ("top", "right")]
        plt.xticks(rotation=20, ha="right", fontsize=8)
    ax.set_title(title or "", fontsize=12, color="#1A1A1A")
    plt.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png"); plt.close(fig)
    return buf.getvalue()


def _email_html(report, has_chart):
    title = report.get("title") or report.get("question") or "Report"
    rows = report.get("rows", []) or []
    cols = report.get("columns") or (list(rows[0].keys()) if rows else [])
    li = lambda items: "".join(f"<li style='margin:4px 0'>{x}</li>" for x in items)
    blocks = ""
    if report.get("research"):
        blocks += f"<div style='font-size:14px;line-height:1.6'>{(report.get('answer') or '').replace(chr(10), '<br>')}</div>"
    else:
        if report.get("explanation"):
            blocks += f"<p style='font-size:14px;color:#333'>{report['explanation']}</p>"
        if has_chart:
            blocks += "<img src='cid:chart' style='max-width:100%;border:1px solid #e4ddd1;border-radius:8px;margin:10px 0'/>"
        if report.get("insights"):
            blocks += f"<h3 style='color:#2C8C99;font-size:13px;letter-spacing:1px'>KEY TAKEAWAYS</h3><ul style='font-size:13px;color:#333'>{li(report['insights'])}</ul>"
        if report.get("recommendations"):
            blocks += f"<h3 style='color:#E26D5A;font-size:13px;letter-spacing:1px'>RECOMMENDED ACTIONS</h3><ul style='font-size:13px;color:#333'>{li(report['recommendations'])}</ul>"
        if report.get("forecast"):
            blocks += f"<p style='background:#faf3df;border-left:3px solid #C9A227;padding:10px 12px;font-size:13px;font-style:italic'>⤳ {report['forecast']}</p>"
        if rows:
            head = "".join(f"<th style='text-align:left;padding:6px 10px;border-bottom:1px solid #ddd;font-size:11px;text-transform:uppercase;color:#888'>{c}</th>" for c in cols)
            body = "".join("<tr>" + "".join(f"<td style='padding:6px 10px;border-bottom:1px solid #eee;font-size:13px'>{r.get(c, '')}</td>" for c in cols) + "</tr>" for r in rows[:40])
            blocks += f"<table style='border-collapse:collapse;width:100%;margin-top:12px'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y")
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:720px;margin:0 auto;color:#1A1A1A">
      <div style="background:#1A1A1A;color:#fff;padding:18px 22px;border-radius:10px 10px 0 0">
        <div style="font-size:11px;letter-spacing:2px;color:#C9A227">HOUSE OF COLOUR · MONTHLY REPORT</div>
        <div style="font-size:22px;font-family:Georgia,serif;margin-top:4px">{title}</div>
        <div style="font-size:11px;color:#aaa;margin-top:4px">Generated {stamp}</div>
      </div>
      <div style="border:1px solid #e4ddd1;border-top:none;border-radius:0 0 10px 10px;padding:20px 22px">{blocks}</div>
    </div>"""


def send_report_email(recipients, report, subject=None):
    chart_type = report.get("chart_type", "bar")
    x, y = report.get("x"), report.get("y")
    rows = report.get("rows", []) or []
    labels = [r.get(x) for r in rows] if x else []
    values = [r.get(y) for r in rows] if y else []
    has_chart = bool(labels) and bool(values) and not report.get("research")

    msg = EmailMessage()
    msg["Subject"] = subject or f"Your monthly House of Colour report — {report.get('title', 'Report')}"
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(f"{report.get('title', 'Report')}\n\nOpen this email in an HTML-capable client to view the report.")
    msg.add_alternative(_email_html(report, has_chart), subtype="html")
    if has_chart:
        png = render_chart_png(chart_type, labels, values, report.get("title", ""))
        msg.get_payload()[1].add_related(png, "image", "png", cid="chart")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.send_message(msg)


def run_schedule(sid):
    """Generate the report (refresh numbers under the owner's scope) and email it."""
    sch = next((s for s in all_schedules() if s["id"] == sid), None)
    if not sch:
        return
    report = dict(sch.get("data") or {})
    owner = USERS.get(sch.get("owner"))
    # Refresh chart data by re-running the stored SQL under the owner's scope.
    if owner and report.get("code") and not report.get("research"):
        try:
            con = scoped_connection({"username": sch["owner"], **owner})
            try:
                df = con.execute(f"SELECT * FROM ({safe_select(report['code'])}) AS _q LIMIT {MAX_ROWS}").df()
                df = df.where(df.notnull(), None)
                report["rows"] = df.to_dict(orient="records")
                report["columns"] = list(df.columns)
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            print(f"[schedule {sid}] data refresh failed: {e}")
    send_report_email(sch.get("recipients", []), report)
    print(f"[schedule {sid}] emailed '{report.get('title')}' to {sch.get('recipients')}")


def register_job(sch):
    try:
        scheduler.add_job(run_schedule, "cron", day=int(sch.get("day_of_month", 1)), hour=8, minute=0,
                          args=[sch["id"]], id=sch["id"], replace_existing=True)
    except Exception as e:  # noqa: BLE001
        print(f"[schedules] could not register job {sch.get('id')}: {e}")


class Login(BaseModel):
    username: str
    password: str


class Query(BaseModel):
    question: str
    research: bool = False   # session "Research mode" — allow web search + internal DB


class BookmarkIn(BaseModel):
    title: str
    kind: str = "chart"
    data: dict


class ScheduleIn(BaseModel):
    title: str
    kind: str = "chart"
    data: dict
    recipients: List[str]
    day_of_month: int = 1


class EmailIn(BaseModel):
    recipients: List[str]
    data: dict


def current_user(authorization):
    token = (authorization or "").removeprefix("Bearer ").strip()
    username = SESSIONS.get(token)
    if not username or username not in USERS:
        raise HTTPException(status_code=401, detail="Please log in.")
    u = USERS[username]
    return {"username": username, **u}


# ---------------------------------------------------------------------------
# Rate limiting for the AI endpoints.
#
# On a public URL an unbounded /api/ask is an unbounded Anthropic bill, so three
# independent limits apply. All in-memory: Render's free plan runs one process,
# so there is nothing to share state with. If this ever scales to >1 instance,
# these become per-instance and want moving to Redis.
#
#   per-IP burst   - stops one person hammering the box
#   per-IP hourly  - stops one person grinding away all afternoon
#   global daily   - the backstop that actually caps the bill
#
# Research mode costs more (web search + more tokens), so it counts double.
# ---------------------------------------------------------------------------
RATE_PER_MIN    = int(os.getenv("AI_RATE_PER_MIN", "3"))
RATE_PER_HOUR   = int(os.getenv("AI_RATE_PER_HOUR", "20"))
RATE_GLOBAL_DAY = int(os.getenv("AI_RATE_GLOBAL_DAY", "300"))

_hits_by_ip = defaultdict(deque)   # ip -> deque[float] of request timestamps
_hits_global = deque()             # timestamps across everyone


def client_ip(request):
    """Real client IP. Render terminates TLS at a proxy, so trust XFF's first hop."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _prune(dq, window, now):
    while dq and now - dq[0] > window:
        dq.popleft()


def check_rate_limit(request, cost=1):
    """Raise 429 if this caller is over any limit; otherwise record the hit."""
    now = time.time()
    ip = client_ip(request)
    hits = _hits_by_ip[ip]

    _prune(hits, 3600, now)
    _prune(_hits_global, 86400, now)

    recent_min = sum(1 for t in hits if now - t <= 60)
    if recent_min + cost > RATE_PER_MIN:
        raise HTTPException(429, f"Slow down a moment - {RATE_PER_MIN} questions per "
                                 f"minute on the demo. Try again shortly.")
    if len(hits) + cost > RATE_PER_HOUR:
        raise HTTPException(429, f"You have used this hour's {RATE_PER_HOUR} AI questions "
                                 f"on the demo. The Power BI tab still works.")
    if len(_hits_global) + cost > RATE_GLOBAL_DAY:
        raise HTTPException(429, "The demo has hit its daily AI limit. The Power BI tab "
                                 "still works - please try again tomorrow.")

    for _ in range(cost):
        hits.append(now)
        _hits_global.append(now)

    # Stop the per-IP map growing without bound on a long-lived instance.
    if len(_hits_by_ip) > 5000:
        for k in [k for k, v in _hits_by_ip.items() if not v]:
            del _hits_by_ip[k]


@app.get("/api/health")
def health():
    """Render's health check. Cheap and dependency-free on purpose."""
    return {"status": "ok", "tables": len(TABLES), "ai": client is not None}


@app.post("/api/login")
def login(body: Login):
    u = USERS.get(body.username.strip().lower())
    if not u or u["password"] != body.password:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = body.username.strip().lower()
    return {"token": token, "name": u["name"], "role": u["role"],
            "scope_col": u["scope_col"], "scope_val": u["scope_val"]}


@app.post("/api/logout")
def logout(authorization: str = Header(None)):
    SESSIONS.pop((authorization or "").removeprefix("Bearer ").strip(), None)
    return {"ok": True}


@app.post("/api/ask")
def ask(q: Query, request: Request, authorization: str = Header(None)):
    user = current_user(authorization)
    check_rate_limit(request, cost=2 if q.research else 1)
    if client is None:
        return {"error": "ANTHROPIC_API_KEY not set. Add it to env.local."}
    if not TABLES:
        return {"error": "No CSV files found in the data/ folder."}

    con = scoped_connection(user)   # <-- row-level enforcement happens here
    try:
        if q.research:
            return run_research(q.question, con)
        scope_note = ""
        if user["role"] != "admin" and user.get("scope_col"):
            scope_note = (f"\n\nNote: this user may only see {user['scope_col']} = "
                          f"'{user['scope_val']}'. The tables below are already filtered to that scope.")
        messages = [{"role": "user",
                     "content": f"Schema:\n{schema_text(con)}{scope_note}\n\nQuestion: {q.question}"}]

        # Self-healing loop: if the JSON/SQL is invalid or the query errors in
        # DuckDB, hand the error back to Claude and let it correct itself.
        last_err = None
        for attempt in range(1, SQL_MAX_ATTEMPTS + 1):
            raw = None
            try:
                msg = client.messages.create(model=MODEL, max_tokens=1500,
                                             system=SYSTEM_PROMPT, messages=messages)
                raw = msg.content[0].text.strip()
                fenced = raw.split("```")[1].lstrip("json").strip() if raw.startswith("```") else raw
                spec = json.loads(fenced)
                sql = safe_select(spec["sql"])
                df = con.execute(f"SELECT * FROM ({sql}) AS _q LIMIT {MAX_ROWS}").df()
            except Exception as e:  # noqa: BLE001 — bad JSON / disallowed SQL / execution error
                last_err = str(e)
                print(f"[ask] attempt {attempt}/{SQL_MAX_ATTEMPTS} failed: {last_err}")
                if attempt >= SQL_MAX_ATTEMPTS:
                    break
                if raw is not None:   # we got a reply but it was wrong — let Claude fix it
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content":
                        f"That response failed with this error:\n\n{last_err}\n\n"
                        f"Fix it. Reply with ONLY a corrected JSON object of the same shape, using valid "
                        f"read-only DuckDB SQL that runs against the schema above. Do not repeat the mistake."})
                continue

            analysis = generate_analysis(q.question, df)
            df = df.where(df.notnull(), None)
            return {
                "chart_type": spec.get("chart_type", "table"),
                "x": spec.get("x"), "y": spec.get("y"), "series": spec.get("series"),
                "title": spec.get("title", ""), "explanation": spec.get("explanation", ""),
                "columns": list(df.columns), "rows": df.to_dict(orient="records"),
                "code": spec["sql"], "attempts": attempt,
                "insights": analysis["insights"],
                "recommendations": analysis["recommendations"],
                "forecast": analysis["forecast"],
            }

        return {"error": f"Couldn't produce a working query after {SQL_MAX_ATTEMPTS} attempts. "
                         f"Last error: {last_err}"}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "trace": traceback.format_exc()}
    finally:
        con.close()


@app.get("/api/tables")
def tables(authorization: str = Header(None)):
    user = current_user(authorization)
    con = scoped_connection(user)
    try:
        return {name: {"rows": con.execute(f"SELECT count(*) FROM {name}").fetchone()[0],
                       "columns": [c[0] for c in meta["columns"]]}
                for name, meta in TABLES.items()}
    finally:
        con.close()


@app.get("/api/bookmarks")
def list_bookmarks(authorization: str = Header(None)):
    user = current_user(authorization)
    return {"bookmarks": BOOKMARKS.get(user["username"], [])}


@app.post("/api/bookmarks")
def add_bookmark(body: BookmarkIn, authorization: str = Header(None)):
    user = current_user(authorization)
    bm = {"id": secrets.token_urlsafe(8), "title": (body.title or "Report")[:160],
          "kind": body.kind or "chart", "created": datetime.now(timezone.utc).isoformat(),
          "data": body.data}
    BOOKMARKS.setdefault(user["username"], []).insert(0, bm)
    _save_bookmarks()
    return {"id": bm["id"]}


@app.delete("/api/bookmarks/{bid}")
def del_bookmark(bid: str, authorization: str = Header(None)):
    user = current_user(authorization)
    lst = BOOKMARKS.get(user["username"], [])
    BOOKMARKS[user["username"]] = [b for b in lst if b.get("id") != bid]
    _save_bookmarks()
    return {"ok": True}


@app.get("/api/schedules")
def list_schedules(authorization: str = Header(None)):
    user = current_user(authorization)
    return {"schedules": SCHEDULES.get(user["username"], [])}


@app.post("/api/schedules")
def add_schedule(body: ScheduleIn, authorization: str = Header(None)):
    user = current_user(authorization)
    day = max(1, min(28, int(body.day_of_month or 1)))
    sch = {"id": secrets.token_urlsafe(8), "owner": user["username"], "title": (body.title or "Report")[:160],
           "kind": body.kind or "chart", "recipients": [r.strip() for r in body.recipients if r.strip()],
           "day_of_month": day, "created": datetime.now(timezone.utc).isoformat(), "data": body.data}
    SCHEDULES.setdefault(user["username"], []).insert(0, sch)
    _save_schedules()
    register_job(sch)
    return {"id": sch["id"], "day_of_month": day}


@app.delete("/api/schedules/{sid}")
def del_schedule(sid: str, authorization: str = Header(None)):
    user = current_user(authorization)
    SCHEDULES[user["username"]] = [s for s in SCHEDULES.get(user["username"], []) if s.get("id") != sid]
    _save_schedules()
    try:
        scheduler.remove_job(sid)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


@app.post("/api/schedules/{sid}/run")
def run_schedule_now(sid: str, authorization: str = Header(None)):
    user = current_user(authorization)
    sch = next((s for s in SCHEDULES.get(user["username"], []) if s.get("id") == sid), None)
    if not sch:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    try:
        run_schedule(sid)
        return {"ok": True, "sent_to": sch.get("recipients", [])}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "trace": traceback.format_exc()}


@app.post("/api/email")
def email_report(body: EmailIn, authorization: str = Header(None)):
    current_user(authorization)
    recips = [r.strip() for r in body.recipients if r.strip()]
    if not recips:
        return {"error": "Add at least one recipient."}
    try:
        send_report_email(recips, body.data, subject=f"House of Colour report — {body.data.get('title', 'Report')}")
        return {"ok": True, "sent_to": recips}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "trace": traceback.format_exc()}


# ---------------------------------------------------------------------------
# Power BI embed configuration.  Embed URLs live in powerbi.json so they can be
# changed without touching code; the portal fetches this on every page render,
# so editing the file + refreshing the browser is enough (no restart).
# ---------------------------------------------------------------------------
POWERBI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "powerbi.json")


@app.get("/api/powerbi")
def powerbi_config():
    """Return the Power BI embed config. Never 500s - the portal degrades to AI-only."""
    try:
        with open(POWERBI_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {"defaultSrc": "", "pages": {}, "error": "powerbi.json not found"}
    except json.JSONDecodeError as e:
        return {"defaultSrc": "", "pages": {}, "error": f"powerbi.json is not valid JSON: {e}"}

    # A single env var can override every page - handy for a quick demo.
    override = os.getenv("POWERBI_EMBED_URL", "").strip()
    if override:
        cfg["defaultSrc"] = override
    return {
        "defaultSrc": cfg.get("defaultSrc", ""),
        "pages": cfg.get("pages", {}),
    }


@app.get("/")
def index():
    """The portal is the demo, so it owns the root URL. /chat still serves the
    plainer csv_chatbot UI for poking at queries without the portal chrome."""
    return portal()


@app.get("/portal")
def portal():
    if not os.path.exists(PORTAL_HTML):
        raise HTTPException(status_code=404, detail=f"Portal HTML not found at {PORTAL_HTML}")
    return FileResponse(PORTAL_HTML)


@app.get("/chat")
def chat_ui():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

# Register saved schedules and start the monthly scheduler.
for _s in all_schedules():
    register_job(_s)
# DISABLE_SCHEDULER=1 during the Docker build (and on Render, where there is no
# SMTP server to mail through) so importing this module has no side effects.
if os.getenv("DISABLE_SCHEDULER") != "1":
    try:
        scheduler.start()
    except Exception as e:  # noqa: BLE001
        print(f"[schedules] scheduler start failed: {e}")

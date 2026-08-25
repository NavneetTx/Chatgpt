# House of Colour — Stylist Portal (local demo)

FastAPI backend + the HOC portal front-end, running locally on Windows.

**Location:** `C:\Users\Navneet Virdi\Desktop\HOC_Portal`
Standalone project — unrelated to, and independent of, the Tx_DQ data-quality framework.

| URL | What it is |
|---|---|
| **http://localhost:8000/portal** | **The House of Colour portal** — Power BI embed + AI report generator |
| http://localhost:8000 | The simpler csv_chatbot UI |
| http://localhost:8000/docs | Auto-generated API docs |

## Run it

From VS Code: **Run and Debug → "HOC Portal (uvicorn)"**, or **Terminal → Run Task → "Run HOC Portal"**.

From a terminal:

```bash
.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8000
```

Then open <http://localhost:8000/portal>.

The `.venv` is already created (Python 3.10) with everything in `requirements.txt` installed.
To rebuild it from scratch:

```bash
py -3.10 -m venv .venv && .venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Demo logins

| Username | Password | Sees |
|---|---|---|
| `navneet` | `navneet123` | Everything (HQ admin) |
| `marcus` | `admin123` | Everything (HQ admin) |
| `admin` | `admin123` | Everything (HQ admin) |
| `sophie` | `sophie123` | Only her own rows — London North (RLS) |
| `claire` | `claire123` | Only her own rows — Manchester (RLS) |

Usernames are case-insensitive. Row-level security is enforced server-side in `app.py`: each request materialises
tables already filtered to that user, so a stylist cannot query outside their scope.

## Embedding your Power BI report

Each report page has a **Power BI** tab and an **AI Report** tab in the report card header.
Embed URLs live in **`powerbi.json`** — edit it and refresh the browser, no restart needed:

```json
{
  "defaultSrc": "",
  "pages": {
    "executive":      { "src": "https://app.powerbi.com/view?r=PASTE_YOURS", "label": "Executive Summary" },
    "stylist_perf":   { "src": "", "label": "Stylist Performance" },
    "stylist_rls":    { "src": "", "label": "Stylist View (RLS)" },
    "client_journey": { "src": "", "label": "Client Journey" }
  }
}
```

* `defaultSrc` is the fallback for any page whose `src` is empty — set it alone to
  show one report everywhere.
* Or set the `POWERBI_EMBED_URL` env var to override every page at once.
* Or paste a URL straight into the **Preview** box in the UI (kept for that browser
  session only — handy for a quick demo before you commit a URL to the file).

### Getting an embed URL

A `.pbix` file **cannot** be embedded in a web page — it is a Power BI Desktop
document, not a web resource. Publish it first:

1. Power BI Desktop → open `Dashboard.pbix` → **Publish** → pick a workspace.
2. In the Power BI Service, open the report → **File → Embed report**.
3. Pick one:
   * **Publish to web (public)** → gives you `https://app.powerbi.com/view?r=...`.
     No sign-in needed, works immediately — but the report becomes **public to anyone
     with the link**. Fine for a demo with sample data; never for real client data.
   * **Embed for your organisation** → gives you `https://app.powerbi.com/reportEmbed?reportId=...`.
     Viewers sign in with Microsoft SSO. This is the one that matches the portal's
     "Secured via Microsoft SSO" story, and it respects Power BI RLS roles.
4. Paste the URL into `powerbi.json` and refresh the portal.

For production (embedding for customers who don't have Power BI licences) you'd use
Power BI Embedded with a service principal generating short-lived embed tokens
server-side — that would be a new endpoint in `app.py` rather than a static URL.

## Layout

```
HOC_Portal/
├─ app.py              FastAPI: auth, RLS, /api/ask (Claude → SQL → chart), email, schedules
├─ powerbi.json        ← your Power BI embed URLs
├─ env.local           ANTHROPIC_API_KEY + ANTHROPIC_MODEL  (gitignored)
├─ data/               CSVs — every file becomes a queryable table
├─ data.duckdb         built from data/ on first run
└─ static/
   ├─ portal.html      the House of Colour portal  → /portal
   └─ index.html       the simple csv_chatbot UI   → /
```

## Notes

* `env.local` already holds an Anthropic API key and is gitignored — the AI Report
  tab needs it; the Power BI tab does not.
* Scheduled email reports expect an SMTP server on `localhost:1025` (e.g. Mailpit,
  inbox at <http://localhost:8025>). Without it the rest of the app is unaffected.

## Deploying to Render

The repo ships a `Dockerfile` and a `render.yaml` blueprint for a free web service.

1. **Push the repo to GitHub** (already done — branch `hoc-portal` on
   `NavneetTx/Chatgpt`).
2. On [render.com](https://render.com): **New → Blueprint**, pick the repo, and
   select the `hoc-portal` branch. Render reads `render.yaml` and creates the
   service `hoc-stylist-portal`.
3. Render will prompt for the two `sync: false` variables:
   * `ANTHROPIC_API_KEY` — paste your key. **Required** for the AI Report tab.
   * `POWERBI_EMBED_URL` — optional; one embed URL for every page. Leave blank
     to use the per-page URLs in `powerbi.json` instead.
4. Deploy. First build takes a few minutes (it installs deps and seeds DuckDB).
   Your URL will be `https://hoc-stylist-portal.onrender.com/portal`.

### Rate limits

The portal is public once deployed, and every AI question costs Anthropic credit,
so `/api/ask` is capped three ways. Defaults are set in `render.yaml` and can be
changed in the Render dashboard without a redeploy:

| Variable | Default | Limit |
|---|---|---|
| `AI_RATE_PER_MIN` | `3` | per IP, per minute — stops hammering |
| `AI_RATE_PER_HOUR` | `20` | per IP, per hour |
| `AI_RATE_GLOBAL_DAY` | `300` | everyone combined, per day — the bill cap |

Research mode counts as 2 (web search costs more). Over the limit returns HTTP 429
with a readable message; the Power BI tab keeps working regardless.

The limits are in-process. Render's free plan runs a single instance so that is
exact, but if you ever scale past one instance they become per-instance and want
moving to Redis.

### Free-plan behaviour

* Sleeps after ~15 min idle, ~1 min to wake — the first visitor after a quiet
  spell waits. Fine for a demo, worth knowing before a live client call.
* Ephemeral filesystem. Bookmarks and schedules saved at runtime are lost on
  restart; the reporting data always returns intact because DuckDB is seeded
  into the image at build time.
* `DISABLE_SCHEDULER=1` is set — there's no SMTP server reachable from Render,
  so the monthly email reports can't send there.

### Security before you share the URL

The demo logins in `app.py` are **plaintext passwords in source**. That is fine
for sample data; it is not fine for anything real. Before pointing a client at a
public URL, either change the passwords or accept that anyone with the link and
the README can sign in as an HQ admin.

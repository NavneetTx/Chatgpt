"""
Generate realistic dummy data for the House of Colour model, based on the
schema documented in HOC_PBI_Model_SplitTabs.xlsx.

Produces a consistent, joinable star schema as CSVs in ./data:
  dim_currency, dim_territory, dim_geography, dim_stylist, dim_palette, dim_rls
  fact_revenue, fact_stylistkpis, fact_clientrevenue

Referential integrity is maintained (facts reference real stylist/territory/geo ids),
and every fact carries territory_id + stylist_id so row-level access scoping works.
"""
import os
import random
import pandas as pd

random.seed(42)
DATA = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA, exist_ok=True)


def save(name, rows):
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(DATA, name + ".csv"), index=False)
    print(f"  {name}.csv  ({len(df)} rows, {len(df.columns)} cols)")


# --- currencies -------------------------------------------------------------
CUR = {  # code: (name, symbol, usd_to_local rate, market)
    "GBP": ("British Pound", "£", 0.79, "EMEA"),
    "USD": ("US Dollar", "$", 1.00, "Americas"),
    "AUD": ("Australian Dollar", "A$", 1.52, "APAC"),
    "AED": ("UAE Dirham", "AED", 3.67, "EMEA"),
    "INR": ("Indian Rupee", "₹", 83.0, "APAC"),
}
save("dim_currency", [
    {"currency_code": c, "currency_name": v[0], "currency_symbol": v[1],
     "fx_rate_per_usd": v[2], "market": v[3], "locale": c[:2].lower()}
    for c, v in CUR.items()
])

# --- territories ------------------------------------------------------------
TERR = [  # territory_id, territory, country, iso, currency, region
    ("T01", "United Kingdom",        "United Kingdom",       "GB", "GBP", "EMEA"),
    ("T02", "United States – East",  "United States",        "US", "USD", "Americas"),
    ("T03", "United States – West",  "United States",        "US", "USD", "Americas"),
    ("T04", "Australia",             "Australia",            "AU", "AUD", "APAC"),
    ("T05", "United Arab Emirates",  "United Arab Emirates", "AE", "AED", "EMEA"),
    ("T06", "India",                 "India",                "IN", "INR", "APAC"),
]
save("dim_territory", [
    {"territory_id": t[0], "territory": t[1], "country": t[2], "iso_code": t[3],
     "currency_code": t[4], "region": t[5],
     "target_stylists": random.randint(4, 10),
     "benchmark_revenue_usd": random.randint(400_000, 1_200_000),
     "retention_pct": random.randint(55, 88), "referral_pct": random.randint(8, 28),
     "virtual_pct": random.randint(15, 55)}
    for t in TERR
])

# --- geography (cities) -----------------------------------------------------
CITIES = [  # geo_id, city, territory_id, lat, lng
    ("G01", "London",        "T01", 51.51, -0.13),
    ("G02", "Manchester",    "T01", 53.48, -2.24),
    ("G03", "Birmingham",    "T01", 52.49, -1.89),
    ("G04", "New York",      "T02", 40.71, -74.01),
    ("G05", "Boston",        "T02", 42.36, -71.06),
    ("G06", "Los Angeles",   "T03", 34.05, -118.24),
    ("G07", "San Francisco", "T03", 37.77, -122.42),
    ("G08", "Sydney",        "T04", -33.87, 151.21),
    ("G09", "Melbourne",     "T04", -37.81, 144.96),
    ("G10", "Dubai",         "T05", 25.20, 55.27),
    ("G11", "Mumbai",        "T06", 19.08, 72.88),
    ("G12", "Bengaluru",     "T06", 12.97, 77.59),
]
tmeta = {t[0]: t for t in TERR}
save("dim_geography", [
    {"geo_id": g[0], "city": g[1], "territory_id": g[2],
     "region": tmeta[g[2]][5], "country": tmeta[g[2]][2], "iso_code": tmeta[g[2]][3],
     "city_lat": g[3], "city_lng": g[4], "stylists_in_city": random.randint(1, 5)}
    for g in CITIES
])

# --- palettes (HOC seasonal colour analysis) --------------------------------
PAL = [("P1", "Spring", "Spring", 1), ("P2", "Summer", "Summer", 2),
       ("P3", "Autumn", "Autumn", 3), ("P4", "Winter", "Winter", 4)]
save("dim_palette", [{"palette_id": p[0], "palette": p[1], "season": p[2], "sort_order": p[3]} for p in PAL])

# --- stylists ---------------------------------------------------------------
FIRST = ["Olivia", "Amelia", "Priya", "Sophie", "Grace", "Isla", "Maya", "Chloe",
         "Ruby", "Aisha", "Hannah", "Zara", "Ella", "Noor", "Layla", "Freya",
         "Anita", "Bianca", "Carmen", "Divya", "Elena", "Farah", "Gita", "Heidi"]
LAST = ["Clarke", "Bennett", "Sharma", "Walsh", "Reid", "Murphy", "Patel", "Hughes",
        "Foster", "Khan", "Wright", "Ahmed", "Lopez", "Singh", "Rossi", "Nguyen",
        "Baker", "Diaz", "Evans", "Gupta", "Hall", "Iqbal", "Jones", "Kelly"]
stylists = []
for i in range(24):
    g = CITIES[i % len(CITIES)]
    tid = g[2]
    cc = tmeta[tid][4]
    target_usd = random.choice([90_000, 110_000, 130_000, 150_000, 180_000])
    fn, ln = FIRST[i], LAST[i]
    stylists.append({
        "stylist_id": f"S{i+1:03d}", "name": f"{fn} {ln}",
        "email": f"{fn.lower()}.{ln.lower()}@houseofcolour.example",
        "initials": fn[0] + ln[0], "city": g[1], "geo_id": g[0], "territory_id": tid,
        "region": tmeta[tid][5], "country": tmeta[tid][2], "currency_code": cc,
        "status": random.choices(["Active", "Dormant"], [0.85, 0.15])[0],
        "contract_year": random.randint(2018, 2025), "coaching_flag": random.choice(["Y", "N"]),
        "annual_target_usd": target_usd,
        "annual_target_local": round(target_usd * CUR[cc][2]),
    })
save("dim_stylist", stylists)

# --- fact_revenue (monthly per stylist, Jul 2025 – Jun 2026) ----------------
MONTHS = [(2025, m) for m in range(7, 13)] + [(2026, m) for m in range(1, 7)]
MNAME = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
rev_rows = []
rid = 0
for s in stylists:
    cc = s["currency_code"]; rate = CUR[cc][2]
    base = s["annual_target_usd"] / 12
    for (yr, mo) in MONTHS:
        rid += 1
        rev_usd = round(base * random.uniform(0.55, 1.35), 2)
        cosm_usd = round(rev_usd * random.uniform(0.10, 0.26), 2)
        q = (mo - 1) // 3 + 1
        rev_rows.append({
            "revenue_id": rid, "date_id": yr * 100 + mo, "year": yr, "month_num": mo,
            "month_name": MNAME[mo - 1], "quarter": q, "quarter_label": f"Q{q} {yr}",
            "stylist_id": s["stylist_id"], "territory_id": s["territory_id"], "geo_id": s["geo_id"],
            "city": s["city"], "region": s["region"], "country": s["country"],
            "currency_code": cc, "fx_rate": rate,
            "revenue_usd": rev_usd, "revenue_local": round(rev_usd * rate, 2),
            "cosm_revenue_usd": cosm_usd, "cosm_revenue_local": round(cosm_usd * rate, 2),
            "royalties_usd": round(rev_usd * 0.10, 2),
            "session_mode": random.choices(["In-Person", "Virtual"], [0.7, 0.3])[0],
            "session_count": random.randint(8, 40),
        })
save("fact_revenue", rev_rows)

# --- fact_stylistkpis (one snapshot row per stylist) ------------------------
ytd = {}
for r in rev_rows:
    ytd[r["stylist_id"]] = ytd.get(r["stylist_id"], 0) + r["revenue_usd"]
ranked = sorted(stylists, key=lambda s: ytd[s["stylist_id"]], reverse=True)
rank_of = {s["stylist_id"]: i + 1 for i, s in enumerate(ranked)}
kpi_rows = []
for s in stylists:
    cc = s["currency_code"]; rate = CUR[cc][2]
    rev_ytd = round(ytd[s["stylist_id"]], 2)
    kpi_rows.append({
        "stylist_id": s["stylist_id"], "territory_id": s["territory_id"], "currency_code": cc,
        "revenue_ytd_usd": rev_ytd, "revenue_ytd_local": round(rev_ytd * rate, 2),
        "clv_usd": random.randint(1800, 6500), "avg_session_usd": random.randint(180, 520),
        "quota_pct": round(rev_ytd / s["annual_target_usd"] * 100, 1),
        "rebook_rate_pct": random.randint(42, 88), "retention_pct": random.randint(55, 92),
        "referral_rate_pct": random.randint(6, 32), "cosm_attach_pct": random.randint(8, 34),
        "win_rate_pct": random.randint(30, 72), "ranking": rank_of[s["stylist_id"]],
        "dormant_clients": random.randint(0, 14), "coaching_flag": s["coaching_flag"],
    })
save("fact_stylistkpis", kpi_rows)

# --- fact_clientrevenue (clients assigned to stylists) ----------------------
CFIRST = ["Emma", "Liam", "Ava", "Noah", "Mia", "Lucas", "Sofia", "Leo", "Ivy", "Max",
          "Lily", "Adam", "Nora", "Sam", "Tara", "Omar", "Ria", "Ben", "Kira", "Dev"]
CLAST = ["Stone", "Park", "Vance", "Cole", "Reed", "Hart", "Lane", "Webb", "Frost", "Bell",
         "Shah", "Wood", "Cruz", "Day", "Fox", "Ali", "Roy", "Kim", "Mehta", "Pope"]
tiers = ["Bronze", "Silver", "Gold", "Platinum"]
client_rows = []
for i in range(160):
    s = random.choice(stylists)
    cc = s["currency_code"]; rate = CUR[cc][2]
    rev_usd = round(random.uniform(220, 7800), 2)
    fn, ln = random.choice(CFIRST), random.choice(CLAST)
    client_rows.append({
        "client_id": f"C{i+1:04d}", "client_name": f"{fn} {ln}", "first_name": fn, "last_name": ln,
        "stylist_id": s["stylist_id"], "territory_id": s["territory_id"],
        "palette_id": random.choice(PAL)[0], "season": random.choice(PAL)[2],
        "tier": random.choices(tiers, [0.4, 0.3, 0.2, 0.1])[0],
        "status": random.choices(["Active", "Dormant"], [0.78, 0.22])[0],
        "total_sessions": random.randint(1, 14),
        "total_revenue_usd": rev_usd, "total_revenue_local": round(rev_usd * rate, 2),
        "cosm_purchased": round(random.uniform(0, 1600), 2),
        "days_since_last_session": random.randint(2, 420),
        "session_types_used": random.choice(["Colour", "Colour, Style", "Colour, Style, Makeup", "Style"]),
    })
save("fact_clientrevenue", client_rows)

# --- dim_rls (the security mapping table) -----------------------------------
rls = [
    {"user_id": "U01", "email": "exec@houseofcolour.example", "name": "Executive",
     "role": "Executive", "can_see_executive": "Y", "can_see_finance": "Y",
     "can_see_my_page": "N", "can_see_stylist": "Y", "territory_access": "ALL",
     "stylist_id_access": "ALL", "currency_code": "USD", "currency_symbol": "$",
     "rls_filter": "ALL"},
    {"user_id": "U02", "email": "finance@houseofcolour.example", "name": "Finance Lead",
     "role": "Finance", "can_see_executive": "N", "can_see_finance": "Y",
     "can_see_my_page": "N", "can_see_stylist": "Y", "territory_access": "ALL",
     "stylist_id_access": "ALL", "currency_code": "USD", "currency_symbol": "$",
     "rls_filter": "ALL"},
    {"user_id": "U03", "email": "uk.manager@houseofcolour.example", "name": "UK Manager",
     "role": "Territory Manager", "can_see_executive": "N", "can_see_finance": "N",
     "can_see_my_page": "N", "can_see_stylist": "Y", "territory_access": "T01",
     "stylist_id_access": "ALL", "currency_code": "GBP", "currency_symbol": "£",
     "rls_filter": "territory_id = 'T01'"},
    {"user_id": "U04", "email": "useast.manager@houseofcolour.example", "name": "US East Manager",
     "role": "Territory Manager", "can_see_executive": "N", "can_see_finance": "N",
     "can_see_my_page": "N", "can_see_stylist": "Y", "territory_access": "T02",
     "stylist_id_access": "ALL", "currency_code": "USD", "currency_symbol": "$",
     "rls_filter": "territory_id = 'T02'"},
    {"user_id": "U05", "email": "olivia.clarke@houseofcolour.example", "name": "Olivia Clarke",
     "role": "Stylist", "can_see_executive": "N", "can_see_finance": "N",
     "can_see_my_page": "Y", "can_see_stylist": "N", "territory_access": "T01",
     "stylist_id_access": "S001", "currency_code": "GBP", "currency_symbol": "£",
     "rls_filter": "stylist_id = 'S001'"},
]
save("dim_rls", rls)

print("\nDone. Tables written to ./data")

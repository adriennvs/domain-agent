"""
Domain Opportunity Agent — Phase 1
Sources : Expireddomains.net, Namebio, Google Trends, OpenPageRank
Output  : Google Sheets + Gmail alert if score > 80
"""

import os
import time
import json
import logging
import requests
import gspread
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from pytrends.request import TrendReq
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

KEYWORDS = [
    # FP&A / Reporting
    "fpna", "budgeting", "forecasting", "cashflow", "treasury",
    "consolidation", "reporting", "cfo", "controllers",
    # Outils / SaaS finance
    "finops", "fintech", "erp", "epm", "automation-finance",
    "close", "reconciliation",
    # Transformation
    "finance-transformation", "shared-services", "finance-digitale",
    "business-partner", "digitalization", "demat",
    # Réglementaire
    "ifrs",
]

EXTENSIONS    = [".com", ".io", ".fr", ".ai", ".co"]
BUDGET_MAX    = 200      # € — filtre dur
SCORE_ALERT   = 80       # seuil email
SHEET_NAME    = os.getenv("GOOGLE_SHEET_NAME", "Domain Agent")
GMAIL_FROM    = os.getenv("GMAIL_FROM")
GMAIL_TO      = os.getenv("GMAIL_TO")
GMAIL_PASS    = os.getenv("GMAIL_APP_PASSWORD")   # App Password Gmail
OPR_API_KEY   = os.getenv("OPR_API_KEY", "")      # OpenPageRank
GCP_CREDS     = os.getenv("GCP_SERVICE_ACCOUNT_JSON")  # JSON string

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ─── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheet():
    creds_info = json.loads(GCP_CREDS)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    gc    = gspread.authorize(creds)
    sh    = gc.open(SHEET_NAME)
    return sh

def ensure_sheets(sh):
    existing = [w.title for w in sh.worksheets()]
    for name, headers in [
        ("opportunites", [
            "date_scan", "domaine", "extension", "prix_achat_estime",
            "score_global", "score_marche", "score_acheteur",
            "score_demande", "score_seo", "score_timing",
            "backlinks", "tendance", "ventes_similaires",
            "heures_encheres", "rationale", "lien_godaddy"
        ]),
        ("portefeuille", [
            "date_achat", "domaine", "prix_achat", "plateforme_vente",
            "prix_demande", "statut", "date_vente", "prix_vente", "pnl"
        ]),
        ("historique_scores", [
            "date", "domaine", "score_global", "vendu", "prix_reel"
        ]),
    ]:
        if name not in existing:
            ws = sh.add_worksheet(title=name, rows=500, cols=len(headers))
            ws.append_row(headers)
            log.info(f"Feuille créée : {name}")

# ─── Scraping Expireddomains.net ───────────────────────────────────────────────

def fetch_expired_domains(keyword: str) -> list[dict]:
    """Scrape expireddomains.net pour un mot-clé donné."""
    url = (
        f"https://www.expireddomains.net/domain-name-search/"
        f"?q={keyword}&fwhois=22&ftlds[]=1&ftlds[]=2&ftlds[]=14"
        f"&fdomainage=1&falexa=1"
    )
    domains = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"class": "base1"})
        if not table:
            return domains
        for row in table.find_all("tr")[1:26]:   # max 25 par keyword
            cols = row.find_all("td")
            if len(cols) < 3:
                continue
            domain_cell = cols[0].get_text(strip=True)
            if not domain_cell:
                continue
            # Prix affiché (souvent vide = prix d'enregistrement ~10€)
            price_raw = cols[3].get_text(strip=True) if len(cols) > 3 else ""
            price = parse_price(price_raw)
            if price > BUDGET_MAX:
                continue
            # Heures restantes aux enchères
            hours_raw = cols[-2].get_text(strip=True) if len(cols) > 2 else ""
            hours = parse_hours(hours_raw)
            ext = next((e for e in EXTENSIONS if domain_cell.endswith(e)), None)
            if not ext:
                continue
            domains.append({
                "domaine": domain_cell.replace(ext, ""),
                "extension": ext,
                "prix_achat_estime": price if price > 0 else 12,
                "heures_encheres": hours,
                "lien_godaddy": f"https://www.godaddy.com/domainsearch/find?checkAvail=1&domainToCheck={domain_cell}",
                "keyword_source": keyword,
            })
    except Exception as e:
        log.warning(f"Expireddomains fetch error ({keyword}): {e}")
    return domains

def parse_price(raw: str) -> float:
    raw = raw.replace("$", "").replace("€", "").replace(",", "").strip()
    try:
        return float(raw)
    except Exception:
        return 0.0

def parse_hours(raw: str) -> int:
    """Convertit '2d 4h' ou '48h' en heures."""
    raw = raw.lower()
    hours = 0
    if "d" in raw:
        parts = raw.split("d")
        try:
            hours += int(parts[0].strip()) * 24
        except Exception:
            pass
        raw = parts[1] if len(parts) > 1 else ""
    if "h" in raw:
        try:
            hours += int(raw.replace("h", "").strip())
        except Exception:
            pass
    return hours if hours > 0 else 168   # défaut 7 jours

# ─── Namebio — ventes similaires ──────────────────────────────────────────────

def fetch_namebio_sales(keyword: str) -> list[dict]:
    url = f"https://namebio.com/?s={keyword}"
    sales = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("table.table tbody tr")[:10]
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue
            domain = cols[0].get_text(strip=True)
            price  = parse_price(cols[1].get_text(strip=True))
            date   = cols[2].get_text(strip=True)
            if price > 0:
                sales.append({"domain": domain, "price": price, "date": date})
    except Exception as e:
        log.warning(f"Namebio error ({keyword}): {e}")
    return sales

# ─── Google Trends ─────────────────────────────────────────────────────────────

_pytrends = TrendReq(hl="fr-FR", tz=60, timeout=(10, 25))

def get_trend_score(keyword: str) -> int:
    """Retourne un score 0-100 basé sur la tendance des 90 derniers jours."""
    try:
        kw = keyword.replace("-", " ")
        _pytrends.build_payload([kw], cat=0, timeframe="today 3-m", geo="")
        df = _pytrends.interest_over_time()
        if df.empty:
            return 0
        avg   = int(df[kw].mean())
        trend = int(df[kw].iloc[-4:].mean() - df[kw].iloc[:4].mean())
        score = min(100, avg + max(0, trend * 2))
        return score
    except Exception as e:
        log.warning(f"Trends error ({keyword}): {e}")
        return 0

# ─── OpenPageRank ──────────────────────────────────────────────────────────────

def get_backlinks_score(domain_full: str) -> tuple[int, int]:
    """Retourne (score 0-100, nb_backlinks estimé)."""
    if not OPR_API_KEY:
        return 0, 0
    try:
        r = requests.get(
            "https://openpagerank.com/api/v1.0/getPageRank",
            params={"domains[]": domain_full},
            headers={"API-OPR": OPR_API_KEY},
            timeout=10,
        )
        data = r.json()
        rank = data["response"][0].get("page_rank_integer", 0)
        bl   = data["response"][0].get("rank", 0)
        score = min(100, rank * 12)   # PageRank 0-10 → score 0-100 (env.)
        return score, bl
    except Exception as e:
        log.warning(f"OPR error ({domain_full}): {e}")
        return 0, 0

# ─── Scoring ───────────────────────────────────────────────────────────────────

def score_domain(domain: dict, sales: list[dict], trend: int) -> dict:
    """
    5 dimensions pondérées :
      marché    30% — ventes similaires récentes
      acheteur  25% — pertinence niche finance
      demande   20% — Google Trends
      seo       15% — backlinks hérités
      timing    10% — heures restantes enchères
    """
    name = domain["domaine"].lower()
    ext  = domain["extension"]

    # --- Score marché (30%) ---
    avg_sale = (sum(s["price"] for s in sales) / len(sales)) if sales else 0
    s_marche = 0
    if avg_sale > 0:
        ratio    = avg_sale / max(domain["prix_achat_estime"], 1)
        s_marche = min(100, int(ratio * 15))
    nb_ventes = len(sales)
    s_marche  = min(100, s_marche + nb_ventes * 5)

    # --- Score acheteur (25%) ---
    # Bonus si le nom correspond à un mot-clé finance de haute valeur
    high_value = {"ifrs", "fpna", "cfo", "treasury", "finops", "erp", "epm",
                  "consolidation", "close", "demat", "digitalization"}
    medium_value = {"budgeting", "forecasting", "cashflow", "reporting",
                    "controllers", "fintech", "reconciliation",
                    "finance-transformation", "shared-services"}
    if any(k in name for k in high_value):
        s_acheteur = 90
    elif any(k in name for k in medium_value):
        s_acheteur = 65
    else:
        s_acheteur = 35
    # Bonus .com / .io
    if ext == ".com":
        s_acheteur = min(100, s_acheteur + 10)
    elif ext in (".io", ".ai"):
        s_acheteur = min(100, s_acheteur + 5)

    # --- Score demande (20%) ---
    s_demande = trend   # déjà 0-100

    # --- Score SEO (15%) ---
    seo_score, nb_backlinks = get_backlinks_score(name + ext)
    s_seo = seo_score

    # --- Score timing (10%) ---
    h = domain["heures_encheres"]
    if h <= 24:
        s_timing = 100
    elif h <= 48:
        s_timing = 80
    elif h <= 72:
        s_timing = 60
    elif h <= 168:
        s_timing = 40
    else:
        s_timing = 20

    # --- Agrégation ---
    score_global = int(
        s_marche   * 0.30 +
        s_acheteur * 0.25 +
        s_demande  * 0.20 +
        s_seo      * 0.15 +
        s_timing   * 0.10
    )

    # --- Rationale ---
    rationale_parts = []
    if avg_sale > 0:
        rationale_parts.append(
            f"{nb_ventes} ventes similaires récentes (moy. {avg_sale:.0f}€)"
        )
    if s_acheteur >= 80:
        rationale_parts.append("mot-clé finance à haute valeur acheteur")
    if trend >= 60:
        rationale_parts.append(f"tendance Google en hausse ({trend}/100)")
    if nb_backlinks > 0:
        rationale_parts.append(f"{nb_backlinks} backlinks hérités")
    if h <= 48:
        rationale_parts.append(f"enchère se termine dans {h}h — urgence")
    rationale = " · ".join(rationale_parts) if rationale_parts else "Opportunité générique niche finance"

    return {
        **domain,
        "score_global":    score_global,
        "score_marche":    s_marche,
        "score_acheteur":  s_acheteur,
        "score_demande":   s_demande,
        "score_seo":       s_seo,
        "score_timing":    s_timing,
        "backlinks":       nb_backlinks,
        "tendance":        trend,
        "ventes_similaires": nb_ventes,
        "rationale":       rationale,
    }

# ─── Email ─────────────────────────────────────────────────────────────────────

def send_alert(opportunities: list[dict]):
    if not (GMAIL_FROM and GMAIL_TO and GMAIL_PASS):
        log.warning("Gmail non configuré — alerte ignorée")
        return
    top = sorted(opportunities, key=lambda x: x["score_global"], reverse=True)[:5]
    rows = ""
    for o in top:
        rows += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <strong>{o['domaine']}{o['extension']}</strong>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            <span style="background:#EAF3DE;color:#27500A;padding:3px 10px;border-radius:12px;font-weight:bold">
              {o['score_global']}/100
            </span>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            ~{o['prix_achat_estime']}€
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;font-size:13px;color:#666">
            {o['rationale']}
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <a href="{o['lien_godaddy']}">Voir →</a>
          </td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto">
      <h2 style="color:#1a1a1a">Opportunités domaines — score > {SCORE_ALERT}</h2>
      <p style="color:#666">Scan du {datetime.now().strftime('%d/%m/%Y à %Hh%M')}</p>
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr style="background:#f5f5f5">
            <th style="padding:8px;text-align:left">Domaine</th>
            <th style="padding:8px">Score</th>
            <th style="padding:8px">Prix achat</th>
            <th style="padding:8px;text-align:left">Rationale</th>
            <th style="padding:8px">Lien</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#999;font-size:12px;margin-top:20px">
        Domain Agent · Scan automatique · Ne pas répondre à cet email
      </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Domain Agent] {len(top)} opportunité(s) score > {SCORE_ALERT} — {datetime.now().strftime('%d/%m %Hh')}"
    msg["From"]    = GMAIL_FROM
    msg["To"]      = GMAIL_TO
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_FROM, GMAIL_PASS)
            s.send_message(msg)
        log.info(f"Email envoyé → {GMAIL_TO}")
    except Exception as e:
        log.error(f"Erreur envoi email : {e}")

# ─── Main ──────────────────────────────────────────────────────────────────────

def run():
    log.info("=== Démarrage scan domain agent ===")
    sh = get_sheet()
    ensure_sheets(sh)
    ws = sh.worksheet("opportunites")

    all_domains: list[dict] = []
    seen: set[str] = set()

    for kw in KEYWORDS:
        log.info(f"Scraping keyword : {kw}")
        domains = fetch_expired_domains(kw)
        sales   = fetch_namebio_sales(kw)
        trend   = get_trend_score(kw)
        time.sleep(2)   # politesse scraping

        for d in domains:
            key = d["domaine"] + d["extension"]
            if key in seen:
                continue
            seen.add(key)
            scored = score_domain(d, sales, trend)
            all_domains.append(scored)

    # Tri par score décroissant
    all_domains.sort(key=lambda x: x["score_global"], reverse=True)

    # Écriture Google Sheets
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows_to_write = []
    for o in all_domains[:50]:   # top 50 max
        rows_to_write.append([
            now,
            o["domaine"],
            o["extension"],
            o["prix_achat_estime"],
            o["score_global"],
            o["score_marche"],
            o["score_acheteur"],
            o["score_demande"],
            o["score_seo"],
            o["score_timing"],
            o["backlinks"],
            o["tendance"],
            o["ventes_similaires"],
            o["heures_encheres"],
            o["rationale"],
            o["lien_godaddy"],
        ])

    if rows_to_write:
        ws.append_rows(rows_to_write, value_input_option="RAW")
        log.info(f"{len(rows_to_write)} opportunités écrites dans Google Sheets")

    # Alertes email — score > seuil
    top_alerts = [o for o in all_domains if o["score_global"] >= SCORE_ALERT]
    if top_alerts:
        log.info(f"{len(top_alerts)} domaine(s) déclenchent une alerte email")
        send_alert(top_alerts)
    else:
        log.info("Aucun domaine ne dépasse le seuil d'alerte")

    log.info("=== Scan terminé ===")

if __name__ == "__main__":
    run()

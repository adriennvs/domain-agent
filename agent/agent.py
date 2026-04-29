"""
Domain Opportunity Agent — Phase 1 (v2)
Sources : WhoisXML API (domaines expirés), Namebio, Google Trends (avec retry), OpenPageRank
Output  : Google Sheets + Gmail alert if score > 80
"""

import os
import time
import json
import random
import logging
import requests
import gspread
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from pytrends.request import TrendReq
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

KEYWORDS = [
    "fpna", "budgeting", "forecasting", "cashflow", "treasury",
    "consolidation", "reporting", "cfo", "controllers",
    "finops", "fintech", "erp", "epm", "automation-finance",
    "close", "reconciliation",
    "finance-transformation", "shared-services", "finance-digitale",
    "business-partner", "digitalization", "demat", "ifrs",
]

EXTENSIONS  = [".com", ".io", ".fr", ".ai", ".co"]
BUDGET_MAX  = 200
SCORE_ALERT = 80

SHEET_NAME      = os.getenv("GOOGLE_SHEET_NAME", "Domain Agent")
GMAIL_FROM      = os.getenv("GMAIL_FROM")
GMAIL_TO        = os.getenv("GMAIL_TO")
GMAIL_PASS      = os.getenv("GMAIL_APP_PASSWORD")
OPR_API_KEY     = os.getenv("OPR_API_KEY", "")
GCP_CREDS       = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
WHOISXML_KEY    = os.getenv("WHOISXML_API_KEY", "")

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
    return gc.open(SHEET_NAME)

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

# ─── Source 1A : WhoisXML API — domaines expirés ──────────────────────────────
# Gratuit : 500 requêtes/mois — https://www.whoisxmlapi.com

def fetch_expired_domains_whoisxml(keyword: str) -> list[dict]:
    domains = []
    for ext in [".com", ".io", ".fr"]:
        tld = ext.replace(".", "")
        try:
            r = requests.post(
                "https://domain-research.whoisxmlapi.com/api/v1",
                json={
                    "apiKey": WHOISXML_KEY,
                    "searchType": "current",
                    "mode": "purchase",
                    "basicSearchTerms": {"include": [keyword]},
                    "filters": [
                        {"field": "tld", "operator": "equals", "value": tld},
                    ],
                },
                timeout=15,
            )
            for item in r.json().get("domainsList", [])[:8]:
                domain_full = item.get("domainName", "")
                if not domain_full:
                    continue
                name = domain_full.replace(ext, "")
                domains.append({
                    "domaine": name,
                    "extension": ext,
                    "prix_achat_estime": 12,
                    "heures_encheres": 168,
                    "lien_godaddy": f"https://www.godaddy.com/domainsearch/find?checkAvail=1&domainToCheck={domain_full}",
                    "keyword_source": keyword,
                })
            time.sleep(1)
        except Exception as e:
            log.warning(f"WhoisXML error ({keyword}{ext}): {e}")
    return domains

# ─── Source 1B : Fallback RDAP — disponibilité sans API payante ───────────────

def fetch_expired_domains_fallback(keyword: str) -> list[dict]:
    """
    Génère des combinaisons domaine + mot-clé et vérifie la disponibilité
    via l'API RDAP publique (aucune clé requise, légal).
    """
    prefixes = ["get", "my", "use", "go", "the", "pro", "be", ""]
    suffixes = ["hq", "app", "hub", "ai", "pro", "ly", "now", ""]
    domains  = []
    kw       = keyword.replace("-", "")

    candidates = []
    for pre in prefixes[:4]:
        for suf in suffixes[:3]:
            for ext in [".com", ".io", ".fr"]:
                name = f"{pre}{kw}{suf}".strip()
                if 4 <= len(name) <= 22:
                    candidates.append((name, ext))

    random.shuffle(candidates)
    for name, ext in candidates[:15]:
        domain_full = name + ext
        try:
            r = requests.get(
                f"https://rdap.org/domain/{domain_full}",
                timeout=8, headers=HEADERS
            )
            if r.status_code == 404:
                domains.append({
                    "domaine": name,
                    "extension": ext,
                    "prix_achat_estime": 12,
                    "heures_encheres": 720,
                    "lien_godaddy": f"https://www.godaddy.com/domainsearch/find?checkAvail=1&domainToCheck={domain_full}",
                    "keyword_source": keyword,
                })
            time.sleep(0.5)
        except Exception:
            pass

    log.info(f"Fallback RDAP ({keyword}) → {len(domains)} domaines disponibles")
    return domains

# ─── Source 2 : Namebio — historique de ventes ────────────────────────────────

def fetch_namebio_sales(keyword: str) -> list[dict]:
    sales = []
    try:
        r    = requests.get(f"https://namebio.com/?s={keyword}", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table.table tbody tr")[:10]:
            cols  = row.find_all("td")
            if len(cols) < 3:
                continue
            price = parse_price(cols[1].get_text(strip=True))
            if price > 0:
                sales.append({"price": price, "date": cols[2].get_text(strip=True)})
    except Exception as e:
        log.warning(f"Namebio error ({keyword}): {e}")
    return sales

# ─── Source 3 : Google Trends — avec retry et backoff ─────────────────────────

def get_trend_score(keyword: str) -> int:
    kw      = keyword.replace("-", " ")
    retries = 3
    for attempt in range(retries):
        try:
            pt = TrendReq(hl="fr-FR", tz=60, timeout=(10, 25),
                          retries=2, backoff_factor=0.5)
            pt.build_payload([kw], cat=0, timeframe="today 3-m", geo="")
            df = pt.interest_over_time()
            if df.empty:
                return 0
            avg   = int(df[kw].mean())
            delta = int(df[kw].iloc[-4:].mean() - df[kw].iloc[:4].mean())
            return min(100, avg + max(0, delta * 2))
        except Exception as e:
            if "429" in str(e) and attempt < retries - 1:
                wait = (2 ** attempt) * 10 + random.randint(5, 15)
                log.warning(f"Trends 429 ({keyword}) — attente {wait}s")
                time.sleep(wait)
            else:
                log.warning(f"Trends error ({keyword}): {e}")
                return 0
    return 0

# ─── Source 4 : OpenPageRank — backlinks ──────────────────────────────────────

def get_backlinks_score(domain_full: str) -> tuple[int, int]:
    if not OPR_API_KEY:
        return 0, 0
    try:
        r    = requests.get(
            "https://openpagerank.com/api/v1.0/getPageRank",
            params={"domains[]": domain_full},
            headers={"API-OPR": OPR_API_KEY},
            timeout=10,
        )
        data = r.json()
        rank = data["response"][0].get("page_rank_integer", 0)
        bl   = data["response"][0].get("rank", 0)
        return min(100, rank * 12), bl
    except Exception as e:
        log.warning(f"OPR error ({domain_full}): {e}")
        return 0, 0

# ─── Utilitaires ──────────────────────────────────────────────────────────────

def parse_price(raw: str) -> float:
    raw = raw.replace("$", "").replace("€", "").replace(",", "").strip()
    try:
        return float(raw)
    except Exception:
        return 0.0

# ─── Scoring ──────────────────────────────────────────────────────────────────

def score_domain(domain: dict, sales: list[dict], trend: int) -> dict:
    name = domain["domaine"].lower()
    ext  = domain["extension"]

    # Marché (30%)
    avg_sale = (sum(s["price"] for s in sales) / len(sales)) if sales else 0
    s_marche = 0
    if avg_sale > 0:
        ratio    = avg_sale / max(domain["prix_achat_estime"], 1)
        s_marche = min(100, int(ratio * 15))
    s_marche = min(100, s_marche + len(sales) * 5)

    # Acheteur (25%)
    high_value   = {"ifrs", "fpna", "cfo", "treasury", "finops", "erp", "epm",
                    "consolidation", "close", "demat", "digitalization"}
    medium_value = {"budgeting", "forecasting", "cashflow", "reporting",
                    "controllers", "fintech", "reconciliation",
                    "financetransformation", "sharedservices"}
    if any(k in name for k in high_value):
        s_acheteur = 90
    elif any(k in name for k in medium_value):
        s_acheteur = 65
    else:
        s_acheteur = 35
    if ext == ".com":   s_acheteur = min(100, s_acheteur + 10)
    elif ext in (".io", ".ai"): s_acheteur = min(100, s_acheteur + 5)

    # Demande (20%)
    s_demande = trend

    # SEO (15%)
    s_seo, nb_backlinks = get_backlinks_score(name + ext)

    # Timing (10%)
    h = domain["heures_encheres"]
    if h <= 24:    s_timing = 100
    elif h <= 48:  s_timing = 80
    elif h <= 72:  s_timing = 60
    elif h <= 168: s_timing = 40
    else:          s_timing = 20

    score_global = int(
        s_marche   * 0.30 +
        s_acheteur * 0.25 +
        s_demande  * 0.20 +
        s_seo      * 0.15 +
        s_timing   * 0.10
    )

    parts = []
    if avg_sale > 0:
        parts.append(f"{len(sales)} ventes similaires (moy. {avg_sale:.0f}€)")
    if s_acheteur >= 80:
        parts.append("mot-clé finance haute valeur")
    if trend >= 60:
        parts.append(f"tendance Google {trend}/100")
    if nb_backlinks > 0:
        parts.append(f"{nb_backlinks} backlinks hérités")
    if h <= 48:
        parts.append(f"enchère dans {h}h")
    rationale = " · ".join(parts) if parts else "Domaine disponible niche finance"

    return {
        **domain,
        "score_global":      score_global,
        "score_marche":      s_marche,
        "score_acheteur":    s_acheteur,
        "score_demande":     s_demande,
        "score_seo":         s_seo,
        "score_timing":      s_timing,
        "backlinks":         nb_backlinks,
        "tendance":          trend,
        "ventes_similaires": len(sales),
        "rationale":         rationale,
    }

# ─── Email ─────────────────────────────────────────────────────────────────────

def send_alert(opportunities: list[dict]):
    if not (GMAIL_FROM and GMAIL_TO and GMAIL_PASS):
        log.warning("Gmail non configuré — alerte ignorée")
        return
    top  = sorted(opportunities, key=lambda x: x["score_global"], reverse=True)[:5]
    rows = ""
    for o in top:
        rows += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <strong>{o['domaine']}{o['extension']}</strong>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            <span style="background:#EAF3DE;color:#27500A;padding:3px 10px;
                         border-radius:12px;font-weight:bold">
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
        Domain Agent · Scan automatique
      </p>
    </body></html>"""

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"[Domain Agent] {len(top)} opportunité(s) score > {SCORE_ALERT}"
        f" — {datetime.now().strftime('%d/%m %Hh')}"
    )
    msg["From"] = GMAIL_FROM
    msg["To"]   = GMAIL_TO
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
    log.info("=== Démarrage scan domain agent v2 ===")
    sh = get_sheet()
    ensure_sheets(sh)
    ws = sh.worksheet("opportunites")

    all_domains: list[dict] = []
    seen: set[str]          = set()

    for kw in KEYWORDS:
        log.info(f"Traitement keyword : {kw}")

        # Source 1 : domaines disponibles
        if WHOISXML_KEY:
            domains = fetch_expired_domains_whoisxml(kw)
        else:
            domains = fetch_expired_domains_fallback(kw)

        # Source 2 : historique ventes
        sales = fetch_namebio_sales(kw)

        # Source 3 : tendance (pause anti-429)
        time.sleep(random.randint(8, 15))
        trend = get_trend_score(kw)

        for d in domains:
            key = d["domaine"] + d["extension"]
            if key in seen:
                continue
            seen.add(key)
            all_domains.append(score_domain(d, sales, trend))

        time.sleep(2)

    all_domains.sort(key=lambda x: x["score_global"], reverse=True)

    now           = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows_to_write = []
    for o in all_domains[:50]:
        rows_to_write.append([
            now, o["domaine"], o["extension"], o["prix_achat_estime"],
            o["score_global"], o["score_marche"], o["score_acheteur"],
            o["score_demande"], o["score_seo"], o["score_timing"],
            o["backlinks"], o["tendance"], o["ventes_similaires"],
            o["heures_encheres"], o["rationale"], o["lien_godaddy"],
        ])

    if rows_to_write:
        ws.append_rows(rows_to_write, value_input_option="RAW")
        log.info(f"{len(rows_to_write)} opportunités écrites dans Google Sheets")
    else:
        log.warning("Aucun domaine trouvé — vérifie les sources de données")

    top_alerts = [o for o in all_domains if o["score_global"] >= SCORE_ALERT]
    if top_alerts:
        log.info(f"{len(top_alerts)} domaine(s) déclenchent une alerte email")
        send_alert(top_alerts)
    else:
        log.info("Aucun domaine ne dépasse le seuil d'alerte")

    log.info("=== Scan terminé ===")

if __name__ == "__main__":
    run()

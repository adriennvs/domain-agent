"""
Domain Opportunity Agent — v5
Fix : DNJournal parsing revu + Namebio recherches génériques multiples
Logique : ventes récentes → variantes disponibles → ratio valeur/prix
"""

import os
import re
import time
import json
import random
import logging
import requests
import gspread
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

BUDGET_MAX   = 200
SCORE_ALERT  = 70
MIN_SALE     = 300      # € — ventes de référence minimum
MAX_VARIANTS = 10       # variantes par domaine source

SHEET_NAME   = os.getenv("GOOGLE_SHEET_NAME", "Domain Agent")
GMAIL_FROM   = os.getenv("GMAIL_FROM")
GMAIL_TO     = os.getenv("GMAIL_TO")
GMAIL_PASS   = os.getenv("GMAIL_APP_PASSWORD")
OPR_API_KEY  = os.getenv("OPR_API_KEY", "")
GCP_CREDS    = os.getenv("GCP_SERVICE_ACCOUNT_JSON")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Mots-clés génériques pour sourcer des ventes récentes sur Namebio
# Couvre tous les secteurs — pas de biais thématique
SEED_KEYWORDS = [
    "tech", "ai", "cloud", "data", "app", "hub", "pro",
    "shop", "pay", "go", "get", "my", "lab", "io",
    "health", "legal", "trade", "market", "digital",
    "smart", "fast", "easy", "live", "real",
]

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
            "score_global", "score_ratio", "score_liquidite",
            "score_tendance", "score_seo",
            "valeur_estimee", "ratio_valeur_prix",
            "nb_ventes_similaires", "prix_moyen_ventes",
            "tendance_score", "backlinks",
            "domaine_source", "prix_source",
            "rationale", "lien_achat"
        ]),
        ("portefeuille", [
            "date_achat", "domaine", "prix_achat", "plateforme_vente",
            "prix_demande", "statut", "date_vente", "prix_vente", "pnl"
        ]),
        ("ventes_reference", [
            "date_collecte", "domaine", "prix_vente", "source"
        ]),
    ]:
        if name not in existing:
            ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers))
            ws.append_row(headers)
            log.info(f"Feuille créée : {name}")

# ─── Source : Namebio — ventes récentes par mot-clé seed ─────────────────────

def fetch_namebio_sales_for_keyword(keyword: str) -> list[dict]:
    """
    Scrape Namebio pour un mot-clé donné.
    Retourne les ventes avec domaine + prix.
    """
    sales = []
    try:
        r = requests.get(
            f"https://namebio.com/?s={keyword}",
            headers=HEADERS, timeout=15
        )
        soup = BeautifulSoup(r.text, "html.parser")

        # Structure Namebio : table avec colonnes Domain, Price, Date, ...
        for row in soup.select("table tbody tr"):
            cols = row.find_all("td")
            if len(cols) < 2:
                continue

            domain_raw = cols[0].get_text(strip=True).lower().strip()
            price_raw  = cols[1].get_text(strip=True)
            price      = parse_price(price_raw)

            if price < MIN_SALE:
                continue

            # Valide que c'est bien un domaine
            domain_clean = re.sub(r"[^a-z0-9.\-]", "", domain_raw)
            if "." not in domain_clean or len(domain_clean) < 4 or len(domain_clean) > 50:
                continue

            # Exclut les domaines trop longs ou absurdes
            name_part = domain_clean.rsplit(".", 1)[0]
            if len(name_part) > 20 or len(name_part) < 2:
                continue

            sales.append({
                "domaine_full": domain_clean,
                "prix":         price,
                "source":       "namebio",
            })

    except Exception as e:
        log.warning(f"Namebio error ({keyword}): {e}")

    return sales


def fetch_all_reference_sales() -> list[dict]:
    """
    Lance plusieurs recherches Namebio avec des seeds génériques
    pour collecter un maximum de ventes de référence tous secteurs.
    """
    all_sales = []
    seen      = set()

    # On prend 12 seeds aléatoires parmi les 25 pour varier les runs
    seeds = random.sample(SEED_KEYWORDS, min(12, len(SEED_KEYWORDS)))

    for seed in seeds:
        log.info(f"Namebio seed : {seed}")
        sales = fetch_namebio_sales_for_keyword(seed)
        for s in sales:
            if s["domaine_full"] not in seen:
                seen.add(s["domaine_full"])
                all_sales.append(s)
        time.sleep(1.5)

    log.info(f"Total ventes de référence : {len(all_sales)} (> {MIN_SALE}€)")
    return all_sales

# ─── Extraction pattern + génération variantes ────────────────────────────────

def extract_pattern(domain_full: str) -> dict | None:
    parts = domain_full.rsplit(".", 1)
    if len(parts) != 2:
        return None
    name = parts[0]
    ext  = "." + parts[1]
    if len(name) < 2 or len(name) > 20:
        return None
    return {"name": name, "ext": ext, "length": len(name)}


def generate_variants(pattern: dict, sold_price: float) -> list[dict]:
    if not pattern:
        return []

    name     = pattern["name"]
    variants = set()

    # 1. Autres extensions
    for ext in [".com", ".io", ".fr", ".co", ".ai"]:
        if ext != pattern["ext"]:
            variants.add((name, ext))

    # 2. Préfixes légers
    for pre in ["get", "my", "use", "go", ""]:
        for ext in [".com", ".io"]:
            new = f"{pre}{name}".strip()
            if new != name and 3 <= len(new) <= 20:
                variants.add((new, ext))

    # 3. Suffixes légers
    for suf in ["hq", "app", "hub", "pro", ""]:
        for ext in [".com", ".io"]:
            new = f"{name}{suf}".strip()
            if new != name and 3 <= len(new) <= 20:
                variants.add((new, ext))

    # 4. Pluriel / singulier
    if name.endswith("s") and len(name) > 3:
        variants.add((name[:-1], ".com"))
        variants.add((name[:-1], pattern["ext"]))
    else:
        variants.add((name + "s", ".com"))

    result = []
    for v_name, v_ext in list(variants)[:MAX_VARIANTS]:
        result.append({
            "domaine":           v_name,
            "extension":         v_ext,
            "prix_achat_estime": 12,
            "domaine_source":    pattern["name"] + pattern["ext"],
            "prix_source":       sold_price,
        })
    return result

# ─── Disponibilité RDAP ───────────────────────────────────────────────────────

def check_available(domain_full: str) -> bool:
    try:
        r = requests.get(
            f"https://rdap.org/domain/{domain_full}",
            timeout=8, headers=HEADERS
        )
        return r.status_code == 404
    except Exception:
        return False

# ─── Ventes similaires Namebio ────────────────────────────────────────────────

def fetch_similar_prices(keyword: str) -> list[float]:
    prices = []
    try:
        r    = requests.get(
            f"https://namebio.com/?s={keyword}",
            headers=HEADERS, timeout=15
        )
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table tbody tr")[:20]:
            cols  = row.find_all("td")
            if len(cols) < 2:
                continue
            price = parse_price(cols[1].get_text(strip=True))
            if price > 0:
                prices.append(price)
    except Exception as e:
        log.warning(f"Namebio similar ({keyword}): {e}")
    return prices

# ─── Google Trends — HTTP direct ──────────────────────────────────────────────

def get_trend_score(keyword: str) -> int:
    kw = keyword.replace("-", " ")
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.get(
            "https://trends.google.com/trends/explore",
            params={"q": kw, "date": "today 3-m", "geo": "", "hl": "fr"},
            timeout=15
        )
        time.sleep(1)
        r2 = session.get(
            "https://trends.google.com/trends/api/explore",
            params={
                "hl": "fr", "tz": -60,
                "req": json.dumps({
                    "comparisonItem": [{"keyword": kw, "geo": "", "time": "today 3-m"}],
                    "category": 0, "property": ""
                })
            },
            timeout=15
        )
        raw   = r2.text.lstrip(")]}'").strip()
        if not raw:
            return 0
        data  = json.loads(raw)
        token = data["widgets"][0]["token"]
        req   = data["widgets"][0]["request"]

        time.sleep(1)
        r3 = session.get(
            "https://trends.google.com/trends/api/widgetdata/multiline",
            params={"hl": "fr", "tz": -60,
                    "req": json.dumps(req), "token": token},
            timeout=15
        )
        raw3   = r3.text.lstrip(")]}'").strip()
        if not raw3:
            return 0
        data3  = json.loads(raw3)
        values = [
            pt["value"][0]
            for pt in data3["default"]["timelineData"]
            if pt.get("value")
        ]
        if not values:
            return 0
        avg   = sum(values) / len(values)
        delta = sum(values[-4:]) / 4 - sum(values[:4]) / 4
        return min(100, int(avg + max(0, delta * 2)))
    except Exception as e:
        log.warning(f"Trends error ({keyword}): {e}")
        return 0

# ─── OpenPageRank ──────────────────────────────────────────────────────────────

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
        rank = int(data["response"][0].get("page_rank_integer") or 0)
        bl   = int(data["response"][0].get("rank") or 0)
        return min(100, rank * 12), bl
    except Exception as e:
        log.warning(f"OPR error ({domain_full}): {e}")
        return 0, 0

# ─── Utilitaires ──────────────────────────────────────────────────────────────

def parse_price(raw: str) -> float:
    raw = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        v = float(raw)
        return v if v < 10_000_000 else 0.0   # filtre valeurs aberrantes
    except Exception:
        return 0.0

# ─── Scoring ──────────────────────────────────────────────────────────────────

def score_domain(domain: dict, similar_prices: list[float], trend: int) -> dict:
    name         = domain["domaine"].lower()
    ext          = domain["extension"]
    prix_achat   = domain.get("prix_achat_estime", 12)
    prix_source  = domain.get("prix_source", 0)

    # Valeur estimée
    all_prices = [p for p in similar_prices if 0 < p < 500_000]
    if prix_source > 0:
        all_prices.append(prix_source)

    valeur_estimee = 0
    if all_prices:
        sorted_p       = sorted(all_prices)
        median         = sorted_p[len(sorted_p) // 2]
        filtered       = [p for p in all_prices if p <= median * 8]
        valeur_estimee = int(sum(filtered) / len(filtered)) if filtered else 0

    # Score ratio (40%)
    ratio = valeur_estimee / max(prix_achat, 1)
    if ratio >= 100:    s_ratio = 100
    elif ratio >= 50:   s_ratio = 90
    elif ratio >= 20:   s_ratio = 75
    elif ratio >= 10:   s_ratio = 60
    elif ratio >= 5:    s_ratio = 40
    elif ratio >= 2:    s_ratio = 20
    else:               s_ratio = 5

    # Score liquidité (25%)
    nb = len(similar_prices)
    if nb >= 20:    s_liquidite = 100
    elif nb >= 10:  s_liquidite = 80
    elif nb >= 5:   s_liquidite = 60
    elif nb >= 2:   s_liquidite = 40
    elif nb >= 1:   s_liquidite = 20
    else:           s_liquidite = 0

    # Score tendance (20%)
    s_tendance = int(trend) if trend else 0

    # Score SEO (15%)
    s_seo, nb_backlinks = get_backlinks_score(name + ext)
    s_seo        = int(s_seo) if s_seo else 0
    nb_backlinks = int(nb_backlinks) if nb_backlinks else 0

    score_global = int(
        s_ratio     * 0.40 +
        s_liquidite * 0.25 +
        s_tendance  * 0.20 +
        s_seo       * 0.15
    )

    # Rationale
    parts = []
    if valeur_estimee > 0 and ratio >= 2:
        parts.append(f"valeur ~{valeur_estimee}€ pour {prix_achat}€ (x{int(ratio)})")
    if nb > 0:
        avg_s = int(sum(similar_prices) / len(similar_prices)) if similar_prices else 0
        parts.append(f"{nb} ventes similaires (moy. {avg_s}€)")
    if trend >= 40:
        parts.append(f"tendance {trend}/100")
    if nb_backlinks > 0:
        parts.append(f"{nb_backlinks} backlinks")
    if domain.get("domaine_source"):
        parts.append(f"variante de {domain['domaine_source']} vendu {int(prix_source)}€")
    rationale = " · ".join(parts) if parts else "Domaine disponible — données de marché limitées"

    return {
        **domain,
        "score_global":         score_global,
        "score_ratio":          s_ratio,
        "score_liquidite":      s_liquidite,
        "score_tendance":       s_tendance,
        "score_seo":            s_seo,
        "valeur_estimee":       valeur_estimee,
        "ratio_valeur_prix":    round(ratio, 1),
        "nb_ventes_similaires": nb,
        "prix_moyen_ventes":    int(sum(similar_prices) / len(similar_prices)) if similar_prices else 0,
        "tendance_score":       trend,
        "backlinks":            nb_backlinks,
        "rationale":            rationale,
        "lien_achat": (
            f"https://www.godaddy.com/domainsearch/find"
            f"?checkAvail=1&domainToCheck={name}{ext}"
        ),
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
            <strong>{o['domaine']}{o['extension']}</strong><br>
            <span style="font-size:11px;color:#999">
              variante de {o.get('domaine_source','—')}
              vendu {int(o.get('prix_source',0))}€
            </span>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            <span style="background:#EAF3DE;color:#27500A;padding:3px 10px;
                         border-radius:12px;font-weight:bold">
              {o['score_global']}/100
            </span>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center;font-weight:bold">
            x{o.get('ratio_valeur_prix','—')}
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            ~{o['prix_achat_estime']}€ → ~{o['valeur_estimee']}€
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;font-size:12px;color:#666">
            {o['rationale'][:120]}
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <a href="{o['lien_achat']}">Acheter →</a>
          </td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:800px;margin:auto">
      <h2 style="color:#1a1a1a">Domaines sous-évalués — opportunités</h2>
      <p style="color:#666">Scan du {datetime.now().strftime('%d/%m/%Y à %Hh%M')}</p>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#f5f5f5">
          <th style="padding:8px;text-align:left">Domaine</th>
          <th style="padding:8px">Score</th>
          <th style="padding:8px">Ratio</th>
          <th style="padding:8px">Achat → Valeur</th>
          <th style="padding:8px;text-align:left">Rationale</th>
          <th style="padding:8px">Lien</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </body></html>"""

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"[Domain Agent] {len(top)} opportunité(s) sous-évaluée(s)"
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
    log.info("=== Démarrage scan domain agent v5 ===")
    sh = get_sheet()
    ensure_sheets(sh)
    ws_opp = sh.worksheet("opportunites")
    ws_ref = sh.worksheet("ventes_reference")

    # Étape 1 : collecter les ventes de référence
    log.info("Collecte des ventes de référence...")
    ref_sales = fetch_all_reference_sales()

    if not ref_sales:
        log.error("Aucune vente de référence — arrêt")
        return

    # Sauvegarde dans Sheet
    now      = datetime.now().strftime("%Y-%m-%d %H:%M")
    ref_rows = [[now, s["domaine_full"], s["prix"], s["source"]] for s in ref_sales]
    ws_ref.append_rows(ref_rows[:150], value_input_option="RAW")
    log.info(f"{len(ref_sales)} ventes de référence sauvegardées")

    # Étape 2 : générer variantes
    log.info("Génération des variantes...")
    candidates: list[dict] = []
    seen: set[str]         = set()

    for sale in ref_sales[:60]:
        pattern  = extract_pattern(sale["domaine_full"])
        if not pattern:
            continue
        variants = generate_variants(pattern, sale["prix"])
        for v in variants:
            key = v["domaine"] + v["extension"]
            if key not in seen:
                seen.add(key)
                candidates.append(v)

    log.info(f"{len(candidates)} variantes générées")

    # Étape 3 : vérifier disponibilité + scorer
    all_scored: list[dict] = []
    random.shuffle(candidates)   # évite les biais de seed

    for candidate in candidates:
        if len(all_scored) >= 50:
            break

        domain_full = candidate["domaine"] + candidate["extension"]

        if not check_available(domain_full):
            continue

        log.info(f"Disponible : {domain_full} "
                 f"(source: {candidate['domaine_source']} → {int(candidate['prix_source'])}€)")

        keyword        = candidate["domaine"][:10]
        similar_prices = fetch_similar_prices(keyword)

        time.sleep(random.randint(2, 4))
        trend = get_trend_score(keyword)

        scored = score_domain(candidate, similar_prices, trend)
        all_scored.append(scored)
        time.sleep(1)

    # Étape 4 : écriture Sheet
    all_scored.sort(key=lambda x: x["score_global"], reverse=True)
    log.info(f"Total domaines scorés : {len(all_scored)}")

    rows_to_write = []
    for o in all_scored[:50]:
        rows_to_write.append([
            now, o["domaine"], o["extension"], o["prix_achat_estime"],
            o["score_global"], o["score_ratio"], o["score_liquidite"],
            o["score_tendance"], o["score_seo"],
            o["valeur_estimee"], o["ratio_valeur_prix"],
            o["nb_ventes_similaires"], o["prix_moyen_ventes"],
            o["tendance_score"], o["backlinks"],
            o.get("domaine_source", ""), o.get("prix_source", 0),
            o["rationale"], o["lien_achat"],
        ])

    if rows_to_write:
        ws_opp.append_rows(rows_to_write, value_input_option="RAW")
        log.info(f"{len(rows_to_write)} opportunités écrites dans Google Sheets")
    else:
        log.warning("Aucune opportunité trouvée ce scan")

    # Étape 5 : alertes email
    top_alerts = [o for o in all_scored if o["score_global"] >= SCORE_ALERT]
    if top_alerts:
        send_alert(top_alerts)
        log.info(f"{len(top_alerts)} alertes envoyées")
    else:
        log.info("Aucun domaine ne dépasse le seuil d'alerte")

    log.info("=== Scan terminé v5 ===")

if __name__ == "__main__":
    run()

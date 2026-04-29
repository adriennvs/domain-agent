"""
Domain Opportunity Agent — v4
Logique : détecter les inefficiences de marché (ratio valeur/prix)
Sources  : DNJournal (ventes premium), Namebio (ventes similaires),
           RDAP (disponibilité), Google Trends, OpenPageRank
Scoring  : ratio valeur/prix 40% · liquidité 25% · tendance 20% · SEO 15%
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

BUDGET_MAX   = 200      # € — filtre dur sur le prix d'achat
SCORE_ALERT  = 75       # seuil déclenchement email (abaissé car scoring revu)
MIN_SALE     = 300      # € — on ignore les ventes < 300€ sur DNJournal/Namebio
MAX_VARIANTS = 12       # nb de variantes générées par domaine source

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
            "date_collecte", "domaine", "prix_vente", "source", "secteur_estime"
        ]),
    ]:
        if name not in existing:
            ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers))
            ws.append_row(headers)
            log.info(f"Feuille créée : {name}")

# ─── Source 1 : DNJournal — ventes premium ────────────────────────────────────

def fetch_dnjournal_sales() -> list[dict]:
    """
    Scrape DNJournal weekly sales chart.
    Retourne les ventes récentes avec domaine + prix.
    """
    sales = []
    urls  = [
        "https://www.dnjournal.com/ytd-sales-charts.htm",
        "https://www.dnjournal.com/archive/domainsales/2024/20241231.htm",
    ]
    for url in urls:
        try:
            r    = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")

            # DNJournal structure : tableaux avec colonnes Domain / Price
            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                for row in rows[1:]:
                    cols = row.find_all("td")
                    if len(cols) < 2:
                        continue
                    domain_raw = cols[0].get_text(strip=True).lower()
                    price_raw  = cols[1].get_text(strip=True)
                    price      = parse_price(price_raw)
                    if price < MIN_SALE:
                        continue
                    # Nettoie le domaine
                    domain_clean = re.sub(r"[^a-z0-9.\-]", "", domain_raw)
                    if "." not in domain_clean or len(domain_clean) < 4:
                        continue
                    sales.append({
                        "domaine_full": domain_clean,
                        "prix":         price,
                        "source":       "dnjournal",
                    })
            time.sleep(1)
        except Exception as e:
            log.warning(f"DNJournal error ({url}): {e}")

    log.info(f"DNJournal → {len(sales)} ventes collectées (> {MIN_SALE}€)")
    return sales


def fetch_namebio_top_sales() -> list[dict]:
    """
    Complément : top ventes récentes Namebio toutes catégories.
    """
    sales = []
    try:
        r    = requests.get(
            "https://namebio.com/?s=&sales=1&datefilter=90",
            headers=HEADERS, timeout=15
        )
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table.table tbody tr")[:100]:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue
            domain_raw = cols[0].get_text(strip=True).lower()
            price      = parse_price(cols[1].get_text(strip=True))
            if price < MIN_SALE:
                continue
            domain_clean = re.sub(r"[^a-z0-9.\-]", "", domain_raw)
            if "." not in domain_clean:
                continue
            sales.append({
                "domaine_full": domain_clean,
                "prix":         price,
                "source":       "namebio",
            })
    except Exception as e:
        log.warning(f"Namebio top sales error: {e}")

    log.info(f"Namebio → {len(sales)} ventes collectées (> {MIN_SALE}€)")
    return sales

# ─── Extraction du pattern d'un domaine vendu ─────────────────────────────────

def extract_pattern(domain_full: str) -> dict:
    """
    Extrait le nom, l'extension, la longueur et les mots-clés d'un domaine.
    ex: "cashflow.io" → {name: "cashflow", ext: ".io", length: 8, words: ["cashflow"]}
    """
    parts = domain_full.rsplit(".", 1)
    if len(parts) != 2:
        return {}
    name = parts[0]
    ext  = "." + parts[1]
    # Découpe les mots (camelCase ou séparateur)
    words = re.findall(r"[a-z]+", name.lower())
    return {
        "name":   name,
        "ext":    ext,
        "length": len(name),
        "words":  words,
    }

# ─── Génération de variantes disponibles ──────────────────────────────────────

def generate_variants(pattern: dict, sold_price: float) -> list[dict]:
    """
    À partir d'un domaine vendu cher, génère des variantes proches
    potentiellement disponibles à prix bas.
    """
    if not pattern:
        return []

    name  = pattern["name"]
    words = pattern["words"]
    variants = []

    # Variantes d'extension
    for ext in [".com", ".io", ".fr", ".co", ".ai"]:
        if ext != pattern["ext"]:
            variants.append((name, ext))

    # Variantes de nom
    prefixes = ["get", "my", "use", "go", "the", "pro", "try", ""]
    suffixes = ["hq", "app", "hub", "pro", "now", "ly", "io", ""]

    for pre in prefixes[:3]:
        for suf in suffixes[:3]:
            for ext in [".com", ".io", ".fr"]:
                new_name = f"{pre}{name}{suf}".strip()
                if new_name != name and 3 <= len(new_name) <= 20:
                    variants.append((new_name, ext))

    # Variantes pluriel / singulier
    if name.endswith("s"):
        variants.append((name[:-1], pattern["ext"]))
        variants.append((name[:-1], ".com"))
    else:
        variants.append((name + "s", pattern["ext"]))
        variants.append((name + "s", ".com"))

    # Déduplique et limite
    seen = set()
    result = []
    for v in variants:
        key = v[0] + v[1]
        if key not in seen and key != pattern["name"] + pattern["ext"]:
            seen.add(key)
            result.append({
                "domaine":           v[0],
                "extension":         v[1],
                "prix_achat_estime": 12,
                "domaine_source":    pattern["name"] + pattern["ext"],
                "prix_source":       sold_price,
            })
    return result[:MAX_VARIANTS]

# ─── Vérification disponibilité RDAP ──────────────────────────────────────────

def check_available(domain_full: str) -> bool:
    """Retourne True si le domaine est disponible (404 = non enregistré)."""
    try:
        r = requests.get(
            f"https://rdap.org/domain/{domain_full}",
            timeout=8, headers=HEADERS
        )
        return r.status_code == 404
    except Exception:
        return False

# ─── Ventes similaires sur Namebio ────────────────────────────────────────────

def fetch_similar_sales(keyword: str) -> list[float]:
    """Retourne la liste des prix de ventes similaires pour un mot-clé."""
    prices = []
    try:
        r    = requests.get(
            f"https://namebio.com/?s={keyword}",
            headers=HEADERS, timeout=15
        )
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table.table tbody tr")[:20]:
            cols  = row.find_all("td")
            if len(cols) < 2:
                continue
            price = parse_price(cols[1].get_text(strip=True))
            if price > 0:
                prices.append(price)
    except Exception as e:
        log.warning(f"Namebio similar ({keyword}): {e}")
    return prices

# ─── Google Trends — appel HTTP direct ────────────────────────────────────────

def get_trend_score(keyword: str) -> int:
    kw = keyword.replace("-", " ")
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        r1 = session.get(
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
        return float(raw)
    except Exception:
        return 0.0

# ─── Scoring ──────────────────────────────────────────────────────────────────

def score_domain(domain: dict, similar_prices: list[float], trend: int) -> dict:
    """
    Scoring orienté inefficience de marché :
      ratio valeur/prix  40%
      liquidité          25%
      tendance           20%
      SEO                15%
    """
    name = domain["domaine"].lower()
    ext  = domain["extension"]

    prix_achat    = domain.get("prix_achat_estime", 12)
    prix_source   = domain.get("prix_source", 0)

    # --- Valeur estimée ---
    # Moyenne pondérée : prix source (domaine similaire vendu) + ventes Namebio
    all_prices = similar_prices.copy()
    if prix_source > 0:
        all_prices.append(prix_source)

    valeur_estimee = 0
    if all_prices:
        # Retire les outliers extrêmes (> 10x la médiane)
        sorted_p = sorted(all_prices)
        median   = sorted_p[len(sorted_p) // 2]
        filtered = [p for p in all_prices if p <= median * 10]
        valeur_estimee = int(sum(filtered) / len(filtered)) if filtered else 0

    # --- Score ratio (40%) ---
    ratio = valeur_estimee / max(prix_achat, 1)
    if ratio >= 100:   s_ratio = 100
    elif ratio >= 50:  s_ratio = 90
    elif ratio >= 20:  s_ratio = 75
    elif ratio >= 10:  s_ratio = 60
    elif ratio >= 5:   s_ratio = 40
    elif ratio >= 2:   s_ratio = 20
    else:              s_ratio = 5

    # --- Score liquidité (25%) ---
    # Mesure si ce type de domaine se vend régulièrement
    nb_ventes   = len(similar_prices)
    if nb_ventes >= 20:  s_liquidite = 100
    elif nb_ventes >= 10: s_liquidite = 80
    elif nb_ventes >= 5:  s_liquidite = 60
    elif nb_ventes >= 2:  s_liquidite = 40
    elif nb_ventes >= 1:  s_liquidite = 20
    else:                 s_liquidite = 0

    # --- Score tendance (20%) ---
    s_tendance = int(trend) if trend else 0

    # --- Score SEO (15%) ---
    s_seo, nb_backlinks = get_backlinks_score(name + ext)
    s_seo        = int(s_seo) if s_seo else 0
    nb_backlinks = int(nb_backlinks) if nb_backlinks else 0

    # --- Agrégation ---
    score_global = int(
        s_ratio     * 0.40 +
        s_liquidite * 0.25 +
        s_tendance  * 0.20 +
        s_seo       * 0.15
    )

    # --- Rationale ---
    parts = []
    if valeur_estimee > 0:
        parts.append(f"valeur estimée ~{valeur_estimee}€ pour {prix_achat}€ d'achat (x{int(ratio)})")
    if nb_ventes > 0:
        avg_similar = int(sum(similar_prices) / len(similar_prices)) if similar_prices else 0
        parts.append(f"{nb_ventes} ventes similaires (moy. {avg_similar}€)")
    if trend >= 50:
        parts.append(f"tendance Google {trend}/100")
    if nb_backlinks > 0:
        parts.append(f"{nb_backlinks} backlinks hérités")
    if domain.get("domaine_source"):
        parts.append(f"inspiré de {domain['domaine_source']} vendu {int(prix_source)}€")
    rationale = " · ".join(parts) if parts else "Domaine disponible — données insuffisantes"

    return {
        **domain,
        "score_global":         score_global,
        "score_ratio":          s_ratio,
        "score_liquidite":      s_liquidite,
        "score_tendance":       s_tendance,
        "score_seo":            s_seo,
        "valeur_estimee":       valeur_estimee,
        "ratio_valeur_prix":    round(ratio, 1),
        "nb_ventes_similaires": nb_ventes,
        "prix_moyen_ventes":    int(sum(similar_prices) / len(similar_prices)) if similar_prices else 0,
        "tendance_score":       trend,
        "backlinks":            nb_backlinks,
        "rationale":            rationale,
        "lien_achat":           (
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
              inspiré de {o.get('domaine_source','—')}
              vendu {int(o.get('prix_source',0))}€
            </span>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            <span style="background:#EAF3DE;color:#27500A;padding:3px 10px;
                         border-radius:12px;font-weight:bold">
              {o['score_global']}/100
            </span>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
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
      <h2 style="color:#1a1a1a">Opportunités domaines sous-évalués</h2>
      <p style="color:#666">Scan du {datetime.now().strftime('%d/%m/%Y à %Hh%M')}
         — score > {SCORE_ALERT}</p>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#f5f5f5">
          <th style="padding:8px;text-align:left">Domaine</th>
          <th style="padding:8px">Score</th>
          <th style="padding:8px">Ratio</th>
          <th style="padding:8px">Prix achat → valeur</th>
          <th style="padding:8px;text-align:left">Rationale</th>
          <th style="padding:8px">Lien</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#999;font-size:12px;margin-top:20px">
        Domain Agent v4 · Scan automatique
      </p>
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
    log.info("=== Démarrage scan domain agent v4 ===")
    sh = get_sheet()
    ensure_sheets(sh)
    ws_opp = sh.worksheet("opportunites")
    ws_ref = sh.worksheet("ventes_reference")

    # ── Étape 1 : collecter les ventes de référence ──────────────────────────
    log.info("Collecte des ventes de référence (DNJournal + Namebio)...")
    ref_sales = fetch_dnjournal_sales()

    # Complément Namebio si DNJournal insuffisant
    if len(ref_sales) < 20:
        ref_sales += fetch_namebio_top_sales()

    if not ref_sales:
        log.error("Aucune vente de référence collectée — arrêt")
        return

    # Sauvegarde des ventes de référence dans le Sheet
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ref_rows = [[now, s["domaine_full"], s["prix"], s["source"], ""] for s in ref_sales]
    ws_ref.append_rows(ref_rows[:100], value_input_option="RAW")
    log.info(f"{len(ref_sales)} ventes de référence sauvegardées")

    # ── Étape 2 : générer et filtrer les variantes ────────────────────────────
    log.info("Génération des variantes...")
    candidates: list[dict] = []
    seen: set[str]         = set()

    for sale in ref_sales[:80]:   # top 80 ventes comme sources
        pattern  = extract_pattern(sale["domaine_full"])
        variants = generate_variants(pattern, sale["prix"])
        for v in variants:
            key = v["domaine"] + v["extension"]
            if key not in seen:
                seen.add(key)
                candidates.append(v)

    log.info(f"{len(candidates)} variantes générées — vérification disponibilité...")

    # ── Étape 3 : vérifier disponibilité + scorer ─────────────────────────────
    all_scored: list[dict] = []

    for i, candidate in enumerate(candidates):
        domain_full = candidate["domaine"] + candidate["extension"]

        # Vérification RDAP
        if not check_available(domain_full):
            continue

        log.info(f"Disponible : {domain_full} (source: {candidate['domaine_source']} → {candidate['prix_source']}€)")

        # Ventes similaires Namebio
        keyword        = candidate["domaine"][:8]   # mot-clé court pour la recherche
        similar_prices = fetch_similar_sales(keyword)

        # Tendance Google
        time.sleep(random.randint(2, 4))
        trend = get_trend_score(keyword)
        log.info(f"Trend ({keyword}): {trend}")

        scored = score_domain(candidate, similar_prices, trend)
        all_scored.append(scored)

        # Pause pour ne pas surcharger les APIs
        time.sleep(1)

        # Limite à 60 domaines scorés par run pour rester dans les limites GitHub Actions
        if len(all_scored) >= 60:
            break

    # ── Étape 4 : écriture Google Sheets ─────────────────────────────────────
    all_scored.sort(key=lambda x: x["score_global"], reverse=True)
    log.info(f"Total domaines scorés : {len(all_scored)}")

    rows_to_write = []
    for o in all_scored[:50]:
        rows_to_write.append([
            now,
            o["domaine"],
            o["extension"],
            o["prix_achat_estime"],
            o["score_global"],
            o["score_ratio"],
            o["score_liquidite"],
            o["score_tendance"],
            o["score_seo"],
            o["valeur_estimee"],
            o["ratio_valeur_prix"],
            o["nb_ventes_similaires"],
            o["prix_moyen_ventes"],
            o["tendance_score"],
            o["backlinks"],
            o.get("domaine_source", ""),
            o.get("prix_source", 0),
            o["rationale"],
            o["lien_achat"],
        ])

    if rows_to_write:
        ws_opp.append_rows(rows_to_write, value_input_option="RAW")
        log.info(f"{len(rows_to_write)} opportunités écrites dans Google Sheets")
    else:
        log.warning("Aucune opportunité trouvée ce scan")

    # ── Étape 5 : alertes email ───────────────────────────────────────────────
    top_alerts = [o for o in all_scored if o["score_global"] >= SCORE_ALERT]
    if top_alerts:
        log.info(f"{len(top_alerts)} domaine(s) déclenchent une alerte")
        send_alert(top_alerts)
    else:
        log.info("Aucun domaine ne dépasse le seuil d'alerte")

    log.info("=== Scan terminé v4 ===")

if __name__ == "__main__":
    run()

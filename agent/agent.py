"""
Domain Opportunity Agent — v6
Sources : DomainsDB.info (API gratuite, domaines récents par mot-clé)
          EstiBot (estimation valeur domaine, gratuit 1000 req/jour)
          RDAP (disponibilité)
          Google Trends (HTTP direct)
          OpenPageRank (backlinks)
Scoring : ratio valeur/prix 40% · liquidité 25% · tendance 20% · SEO 15%
Zéro scraping — fonctionne depuis n'importe quelle IP
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
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

BUDGET_MAX  = 200
SCORE_ALERT = 70

SHEET_NAME  = os.getenv("GOOGLE_SHEET_NAME", "Domain Agent")
GMAIL_FROM  = os.getenv("GMAIL_FROM")
GMAIL_TO    = os.getenv("GMAIL_TO")
GMAIL_PASS  = os.getenv("GMAIL_APP_PASSWORD")
OPR_API_KEY = os.getenv("OPR_API_KEY", "")
GCP_CREDS   = os.getenv("GCP_SERVICE_ACCOUNT_JSON")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Mots-clés seeds — tous secteurs, couvrent les domaines à forte demande
SEED_KEYWORDS = [
    # Tech & Digital
    "ai", "cloud", "data", "tech", "digital", "cyber", "crypto",
    "saas", "api", "dev", "code", "software",
    # Business & Finance
    "pay", "trade", "market", "invest", "fund", "capital",
    "finance", "cash", "bank", "wealth",
    # Consumer & Brand
    "shop", "store", "hub", "pro", "app", "go", "get",
    "smart", "fast", "easy", "live", "now",
    # Health & Legal
    "health", "care", "legal", "law", "med",
]

# Extensions ciblées par ordre de valeur marché
TARGET_EXTENSIONS = [".com", ".io", ".ai", ".co", ".fr"]

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
            "popularite_mot_cle", "tendance_score", "backlinks",
            "rationale", "lien_achat"
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
            ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers))
            ws.append_row(headers)
            log.info(f"Feuille créée : {name}")

# ─── Source 1 : DomainsDB — domaines enregistrés récemment ───────────────────
# API publique gratuite, sans clé : https://domainsdb.info

def fetch_domains_by_keyword(keyword: str) -> list[dict]:
    """
    Retourne les domaines récemment enregistrés contenant ce mot-clé.
    Signal : si beaucoup de gens enregistrent des domaines avec ce mot,
    la demande est active → les variantes disponibles ont de la valeur.
    """
    results = []
    for zone in ["com", "io", "ai", "co", "fr"]:
        try:
            r = requests.get(
                "https://api.domainsdb.info/v1/domains/search",
                params={
                    "domain": keyword,
                    "zone":   zone,
                    "limit":  20,
                    "page":   0,
                },
                headers=HEADERS,
                timeout=15,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            for item in data.get("domains", []):
                name_full = item.get("domain", "")
                if not name_full:
                    continue
                # Filtre : le mot-clé doit être dans le nom (pas juste dans le TLD)
                name_part = name_full.rsplit(".", 1)[0] if "." in name_full else name_full
                if keyword.lower() not in name_part.lower():
                    continue
                if len(name_part) < 2 or len(name_part) > 20:
                    continue
                results.append({
                    "domain_full": name_full,
                    "name":        name_part,
                    "ext":         "." + zone,
                    "keyword":     keyword,
                })
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"DomainsDB error ({keyword}/{zone}): {e}")

    return results


def get_keyword_demand(keyword: str, all_domains: list[dict]) -> int:
    """
    Mesure la popularité d'un mot-clé en comptant combien de domaines
    enregistrés le contiennent. Plus il y en a, plus la demande est forte.
    Score 0-100.
    """
    count = sum(1 for d in all_domains if keyword.lower() in d["name"].lower())
    if count >= 50:   return 100
    elif count >= 30: return 85
    elif count >= 20: return 70
    elif count >= 10: return 55
    elif count >= 5:  return 40
    elif count >= 2:  return 25
    return 10

# ─── Source 2 : EstiBot — estimation valeur domaine ──────────────────────────
# Gratuit, 1000 req/jour : https://www.estibot.com

def get_estibot_value(domain_full: str) -> int:
    """
    Retourne la valeur estimée du domaine en USD (converti en €).
    EstiBot utilise un modèle ML basé sur les ventes historiques.
    """
    try:
        r = requests.get(
            f"https://www.estibot.com/appraise.php",
            params={"a": domain_full},
            headers=HEADERS,
            timeout=15,
        )
        # EstiBot retourne du JSON ou du texte selon l'endpoint
        # On parse la valeur depuis la réponse
        text = r.text
        # Cherche un pattern de valeur numérique
        match = re.search(r'"appraisal"\s*:\s*"?\$?([\d,]+)"?', text)
        if match:
            val = float(match.group(1).replace(",", ""))
            return int(val * 0.92)   # USD → EUR approximatif
        # Fallback : cherche tout nombre précédé de $
        match2 = re.search(r'\$\s*([\d,]+)', text)
        if match2:
            val = float(match2.group(1).replace(",", ""))
            return int(val * 0.92)
        return 0
    except Exception as e:
        log.warning(f"EstiBot error ({domain_full}): {e}")
        return 0


def estimate_value_heuristic(name: str, ext: str, keyword_demand: int) -> int:
    """
    Estimation heuristique de valeur si EstiBot échoue.
    Basée sur : longueur, extension, demande du mot-clé.
    """
    # Base selon extension
    ext_base = {".com": 500, ".io": 300, ".ai": 400, ".co": 200, ".fr": 150}
    base = ext_base.get(ext, 100)

    # Bonus longueur (plus court = plus cher)
    length = len(name)
    if length <= 4:     length_mult = 4.0
    elif length <= 6:   length_mult = 2.5
    elif length <= 8:   length_mult = 1.5
    elif length <= 10:  length_mult = 1.0
    else:               length_mult = 0.6

    # Bonus demande mot-clé
    demand_mult = 1 + (keyword_demand / 100)

    return int(base * length_mult * demand_mult)

# ─── Source 3 : RDAP — vérification disponibilité ────────────────────────────

def check_available(domain_full: str) -> bool:
    try:
        r = requests.get(
            f"https://rdap.org/domain/{domain_full}",
            timeout=8, headers=HEADERS
        )
        return r.status_code == 404
    except Exception:
        return False

# ─── Source 4 : Google Trends — HTTP direct ───────────────────────────────────

def get_trend_score(keyword: str) -> int:
    kw = keyword.replace("-", " ")
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.get(
            "https://trends.google.com/trends/explore",
            params={"q": kw, "date": "today 3-m", "geo": "", "hl": "fr"},
            timeout=15,
        )
        time.sleep(1)
        r2 = session.get(
            "https://trends.google.com/trends/api/explore",
            params={
                "hl": "fr", "tz": -60,
                "req": json.dumps({
                    "comparisonItem": [{"keyword": kw, "geo": "", "time": "today 3-m"}],
                    "category": 0, "property": ""
                }),
            },
            timeout=15,
        )
        raw = r2.text.lstrip(")]}'").strip()
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
            timeout=15,
        )
        raw3 = r3.text.lstrip(")]}'").strip()
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

# ─── Source 5 : OpenPageRank ──────────────────────────────────────────────────

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

# ─── Génération de variantes ──────────────────────────────────────────────────

def generate_available_variants(
    keyword: str,
    registered_domains: list[dict],
    keyword_demand: int,
) -> list[dict]:
    """
    Pour un mot-clé, génère des combinaisons de domaines
    et retourne ceux qui sont disponibles à l'achat.
    """
    prefixes = ["get", "my", "use", "go", "the", "pro", "be", ""]
    suffixes = ["hq", "app", "hub", "pro", "now", "ly", "ai", ""]

    candidates = set()
    for pre in prefixes[:4]:
        for suf in suffixes[:4]:
            for ext in TARGET_EXTENSIONS:
                name = f"{pre}{keyword}{suf}".strip()
                if 3 <= len(name) <= 18:
                    candidates.add((name, ext))

    # Ajoute des variantes depuis les domaines déjà enregistrés
    for d in registered_domains[:10]:
        name = d["name"]
        # Extension alternative
        for ext in TARGET_EXTENSIONS:
            if ext != d["ext"]:
                candidates.add((name, ext))
        # Pluriel
        if not name.endswith("s"):
            candidates.add((name + "s", d["ext"]))
            candidates.add((name + "s", ".com"))

    available = []
    for name, ext in list(candidates):
        domain_full = name + ext
        # Ne teste pas les domaines déjà enregistrés qu'on vient de collecter
        already_registered = any(
            d["domain_full"] == domain_full for d in registered_domains
        )
        if already_registered:
            continue
        if check_available(domain_full):
            available.append({
                "domaine":           name,
                "extension":         ext,
                "prix_achat_estime": 12,
                "keyword_source":    keyword,
                "keyword_demand":    keyword_demand,
            })
        time.sleep(0.3)

    log.info(f"Variants disponibles ({keyword}): {len(available)}")
    return available

# ─── Scoring ──────────────────────────────────────────────────────────────────

def score_domain(domain: dict, valeur: int, trend: int) -> dict:
    name         = domain["domaine"].lower()
    ext          = domain["extension"]
    prix_achat   = domain.get("prix_achat_estime", 12)
    kw_demand    = domain.get("keyword_demand", 0)

    # Valeur finale : EstiBot si dispo, sinon heuristique
    if valeur <= 0:
        valeur = estimate_value_heuristic(name, ext, kw_demand)

    # Score ratio valeur/prix (40%)
    ratio = valeur / max(prix_achat, 1)
    if ratio >= 100:   s_ratio = 100
    elif ratio >= 50:  s_ratio = 90
    elif ratio >= 20:  s_ratio = 75
    elif ratio >= 10:  s_ratio = 60
    elif ratio >= 5:   s_ratio = 40
    elif ratio >= 2:   s_ratio = 20
    else:              s_ratio = 5

    # Score liquidité (25%) — basé sur la demande du mot-clé
    s_liquidite = int(kw_demand) if kw_demand else 0

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
    if valeur > 0:
        parts.append(f"valeur estimée ~{valeur}€ pour {prix_achat}€ (x{int(ratio)})")
    if kw_demand >= 50:
        parts.append(f"mot-clé très demandé (popularité {kw_demand}/100)")
    elif kw_demand >= 25:
        parts.append(f"mot-clé en demande (popularité {kw_demand}/100)")
    if trend >= 40:
        parts.append(f"tendance Google {trend}/100")
    if nb_backlinks > 0:
        parts.append(f"{nb_backlinks} backlinks hérités")
    rationale = " · ".join(parts) if parts else "Domaine disponible — potentiel à confirmer"

    return {
        **domain,
        "score_global":      score_global,
        "score_ratio":       s_ratio,
        "score_liquidite":   s_liquidite,
        "score_tendance":    s_tendance,
        "score_seo":         s_seo,
        "valeur_estimee":    valeur,
        "ratio_valeur_prix": round(ratio, 1),
        "popularite_mot_cle": kw_demand,
        "tendance_score":    trend,
        "backlinks":         nb_backlinks,
        "rationale":         rationale,
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
              mot-clé : {o.get('keyword_source','—')}
              · popularité {o.get('popularite_mot_cle',0)}/100
            </span>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            <span style="background:#EAF3DE;color:#27500A;padding:3px 10px;
                         border-radius:12px;font-weight:bold">
              {o['score_global']}/100
            </span>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center;
                     font-weight:bold;font-size:16px">
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
      <p style="color:#999;font-size:12px;margin-top:20px">
        Domain Agent v6 · Scan automatique
      </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
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
    log.info("=== Démarrage scan domain agent v6 ===")
    sh = get_sheet()
    ensure_sheets(sh)
    ws_opp = sh.worksheet("opportunites")

    # Sélection aléatoire de 8 seeds parmi les 40 — varie à chaque run
    seeds = random.sample(SEED_KEYWORDS, min(8, len(SEED_KEYWORDS)))
    log.info(f"Seeds sélectionnés : {seeds}")

    all_scored: list[dict] = []
    seen_domains: set[str] = set()

    for keyword in seeds:
        log.info(f"=== Traitement keyword : {keyword} ===")

        # Étape 1 : domaines enregistrés récemment (signal de demande)
        registered = fetch_domains_by_keyword(keyword)
        log.info(f"DomainsDB ({keyword}) : {len(registered)} domaines enregistrés")

        # Étape 2 : mesure de la demande
        kw_demand = get_keyword_demand(keyword, registered)
        log.info(f"Demande ({keyword}) : {kw_demand}/100")

        # Étape 3 : tendance Google
        time.sleep(random.randint(2, 4))
        trend = get_trend_score(keyword)
        log.info(f"Tendance ({keyword}) : {trend}/100")

        # Étape 4 : variantes disponibles
        available = generate_available_variants(keyword, registered, kw_demand)

        # Étape 5 : estimation valeur + scoring
        for domain in available[:8]:   # max 8 par keyword
            full = domain["domaine"] + domain["extension"]
            if full in seen_domains:
                continue
            seen_domains.add(full)

            # EstiBot — estimation valeur
            valeur = get_estibot_value(full)
            log.info(f"EstiBot ({full}) : {valeur}€")
            time.sleep(0.5)

            scored = score_domain(domain, valeur, trend)
            all_scored.append(scored)

            if len(all_scored) >= 60:
                break

        time.sleep(2)
        if len(all_scored) >= 60:
            break

    # Écriture Google Sheets
    all_scored.sort(key=lambda x: x["score_global"], reverse=True)
    log.info(f"Total domaines scorés : {len(all_scored)}")

    now           = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows_to_write = []
    for o in all_scored[:50]:
        rows_to_write.append([
            now, o["domaine"], o["extension"], o["prix_achat_estime"],
            o["score_global"], o["score_ratio"], o["score_liquidite"],
            o["score_tendance"], o["score_seo"],
            o["valeur_estimee"], o["ratio_valeur_prix"],
            o["popularite_mot_cle"], o["tendance_score"], o["backlinks"],
            o["rationale"], o["lien_achat"],
        ])

    if rows_to_write:
        ws_opp.append_rows(rows_to_write, value_input_option="RAW")
        log.info(f"{len(rows_to_write)} opportunités écrites dans Google Sheets")
    else:
        log.warning("Aucune opportunité trouvée ce scan")

    # Alertes email
    top_alerts = [o for o in all_scored if o["score_global"] >= SCORE_ALERT]
    if top_alerts:
        send_alert(top_alerts)
        log.info(f"{len(top_alerts)} alertes envoyées")
    else:
        log.info("Aucun domaine ne dépasse le seuil d'alerte")

    log.info("=== Scan terminé v6 ===")

if __name__ == "__main__":
    run()

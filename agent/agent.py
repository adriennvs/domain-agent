"""
Domain Opportunity Agent — v7
Rapide : tourne en < 10 minutes
Sources : DomainsDB.info (API gratuite) + RDAP (disponibilité, timeout 3s)
Valeur  : heuristique (longueur + extension + demande mot-clé)
Scoring : ratio valeur/prix 40% · liquidité 25% · tendance 20% · SEO 15%
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

SCORE_ALERT    = 65
MAX_RDAP       = 30    # nb max de vérifications RDAP par run — évite les timeouts
MAX_SCORED     = 40    # nb max de domaines scorés par run

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

# Seeds tous secteurs — 6 sélectionnés aléatoirement par run
SEED_KEYWORDS = [
    "ai", "pay", "trade", "cloud", "data", "tech",
    "health", "legal", "shop", "fund", "hub", "pro",
    "market", "smart", "fast", "care", "lab", "go",
    "invest", "saas", "crypto", "digital", "code", "app",
]

TARGET_EXTENSIONS = [".com", ".io", ".ai", ".co", ".fr"]

# Valeur de base par extension (€) — basé sur moyennes marché
EXT_BASE = {".com": 600, ".io": 350, ".ai": 500, ".co": 200, ".fr": 150}

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
            "date_scan", "domaine", "extension", "prix_achat",
            "valeur_estimee", "ratio_x",
            "score_global", "score_ratio", "score_liquidite",
            "score_tendance", "score_seo",
            "demande_mot_cle", "tendance", "backlinks",
            "mot_cle_source", "rationale", "lien_achat"
        ]),
        ("portefeuille", [
            "date_achat", "domaine", "prix_achat", "plateforme",
            "prix_demande", "statut", "date_vente", "prix_vente", "pnl"
        ]),
    ]:
        if name not in existing:
            ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers))
            ws.append_row(headers)
            log.info(f"Feuille créée : {name}")

# ─── DomainsDB — signal de demande ────────────────────────────────────────────

def fetch_domainsdb(keyword: str) -> list[dict]:
    """Retourne les domaines récemment enregistrés contenant ce mot-clé."""
    results = []
    for zone in ["com", "io", "ai"]:
        try:
            r = requests.get(
                "https://api.domainsdb.info/v1/domains/search",
                params={"domain": keyword, "zone": zone, "limit": 25, "page": 0},
                headers=HEADERS, timeout=10,
            )
            if r.status_code != 200:
                continue
            for item in r.json().get("domains", []):
                name_full = item.get("domain", "").lower()
                if "." not in name_full:
                    continue
                name_part = name_full.rsplit(".", 1)[0]
                if keyword.lower() not in name_part:
                    continue
                if not (2 <= len(name_part) <= 20):
                    continue
                results.append({
                    "domain_full": name_full,
                    "name": name_part,
                    "ext": "." + zone,
                })
            time.sleep(0.5)
        except Exception as e:
            log.warning(f"DomainsDB ({keyword}/{zone}): {e}")
    log.info(f"DomainsDB ({keyword}): {len(results)} domaines enregistrés")
    return results

def demand_score(keyword: str, registered: list[dict]) -> int:
    """Score de demande 0-100 basé sur le nombre de domaines enregistrés."""
    n = len(registered)
    if n >= 60: return 100
    if n >= 40: return 85
    if n >= 25: return 70
    if n >= 15: return 55
    if n >= 8:  return 40
    if n >= 3:  return 25
    return 10

# ─── RDAP — disponibilité (timeout court) ─────────────────────────────────────

def is_available(domain_full: str) -> bool:
    """
    Vérifie si un domaine est disponible via RDAP.
    Timeout 3s — si ça ne répond pas vite, on considère pris.
    """
    try:
        r = requests.get(
            f"https://rdap.org/domain/{domain_full}",
            timeout=3, headers=HEADERS
        )
        return r.status_code == 404
    except Exception:
        return False

# ─── Estimation de valeur heuristique ─────────────────────────────────────────

def estimate_value(name: str, ext: str, kw_demand: int) -> int:
    """
    Estimation de valeur basée sur :
    - Extension (base de marché)
    - Longueur du nom (plus court = plus cher)
    - Demande du mot-clé
    """
    base   = EXT_BASE.get(ext, 100)
    length = len(name)

    if length <= 3:    lmult = 8.0
    elif length <= 5:  lmult = 4.0
    elif length <= 7:  lmult = 2.0
    elif length <= 9:  lmult = 1.2
    elif length <= 12: lmult = 0.8
    else:              lmult = 0.4

    dmult = 1.0 + (kw_demand / 100) * 1.5   # demande booste la valeur jusqu'à x2.5

    return max(50, int(base * lmult * dmult))

# ─── Google Trends — HTTP direct ──────────────────────────────────────────────

def get_trend(keyword: str) -> int:
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get(
            "https://trends.google.com/trends/explore",
            params={"q": keyword, "date": "today 3-m", "geo": "", "hl": "fr"},
            timeout=10,
        )
        time.sleep(1)
        r2 = s.get(
            "https://trends.google.com/trends/api/explore",
            params={
                "hl": "fr", "tz": -60,
                "req": json.dumps({
                    "comparisonItem": [{"keyword": keyword, "geo": "", "time": "today 3-m"}],
                    "category": 0, "property": "",
                }),
            },
            timeout=10,
        )
        raw = r2.text.lstrip(")]}'").strip()
        if not raw:
            return 0
        data  = json.loads(raw)
        token = data["widgets"][0]["token"]
        req   = data["widgets"][0]["request"]
        time.sleep(1)
        r3 = s.get(
            "https://trends.google.com/trends/api/widgetdata/multiline",
            params={"hl": "fr", "tz": -60, "req": json.dumps(req), "token": token},
            timeout=10,
        )
        raw3   = r3.text.lstrip(")]}'").strip()
        if not raw3:
            return 0
        vals = [
            pt["value"][0]
            for pt in json.loads(raw3)["default"]["timelineData"]
            if pt.get("value")
        ]
        if not vals:
            return 0
        avg   = sum(vals) / len(vals)
        delta = sum(vals[-4:]) / 4 - sum(vals[:4]) / 4
        return min(100, int(avg + max(0, delta * 2)))
    except Exception as e:
        log.warning(f"Trends ({keyword}): {e}")
        return 0

# ─── OpenPageRank ──────────────────────────────────────────────────────────────

def get_seo(domain_full: str) -> tuple[int, int]:
    if not OPR_API_KEY:
        return 0, 0
    try:
        r    = requests.get(
            "https://openpagerank.com/api/v1.0/getPageRank",
            params={"domains[]": domain_full},
            headers={"API-OPR": OPR_API_KEY},
            timeout=8,
        )
        resp = r.json()["response"][0]
        rank = int(resp.get("page_rank_integer") or 0)
        bl   = int(resp.get("rank") or 0)
        return min(100, rank * 12), bl
    except Exception:
        return 0, 0

# ─── Scoring ──────────────────────────────────────────────────────────────────

def score(domain: str, ext: str, kw: str, kw_demand: int, trend: int) -> dict:
    prix    = 12
    valeur  = estimate_value(domain, ext, kw_demand)
    ratio   = valeur / prix

    # Ratio valeur/prix (40%)
    if ratio >= 100:   sr = 100
    elif ratio >= 50:  sr = 90
    elif ratio >= 20:  sr = 75
    elif ratio >= 10:  sr = 60
    elif ratio >= 5:   sr = 40
    else:              sr = 20

    # Liquidité = demande mot-clé (25%)
    sl = kw_demand

    # Tendance (20%)
    st = trend

    # SEO (15%)
    seo_score, backlinks = get_seo(domain + ext)

    total = int(sr * 0.40 + sl * 0.25 + st * 0.20 + seo_score * 0.15)

    parts = [f"valeur ~{valeur}€ pour {prix}€ (x{int(ratio)})"]
    if kw_demand >= 50:
        parts.append(f"mot-clé populaire ({kw_demand}/100)")
    if trend >= 40:
        parts.append(f"tendance {trend}/100")
    if backlinks > 0:
        parts.append(f"{backlinks} backlinks")

    return {
        "domaine":         domain,
        "extension":       ext,
        "prix_achat":      prix,
        "valeur_estimee":  valeur,
        "ratio_x":         round(ratio, 1),
        "score_global":    total,
        "score_ratio":     sr,
        "score_liquidite": sl,
        "score_tendance":  st,
        "score_seo":       seo_score,
        "demande_mot_cle": kw_demand,
        "tendance":        trend,
        "backlinks":       backlinks,
        "mot_cle_source":  kw,
        "rationale":       " · ".join(parts),
        "lien_achat": (
            f"https://www.godaddy.com/domainsearch/find"
            f"?checkAvail=1&domainToCheck={domain}{ext}"
        ),
    }

# ─── Email ─────────────────────────────────────────────────────────────────────

def send_alert(opps: list[dict]):
    if not (GMAIL_FROM and GMAIL_TO and GMAIL_PASS):
        return
    top  = sorted(opps, key=lambda x: x["score_global"], reverse=True)[:5]
    rows = "".join(f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <strong>{o['domaine']}{o['extension']}</strong>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            <span style="background:#EAF3DE;color:#27500A;padding:2px 8px;
                         border-radius:10px;font-weight:bold">
              {o['score_global']}/100
            </span>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center;
                     font-weight:bold">x{o['ratio_x']}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            12€ → ~{o['valeur_estimee']}€
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;font-size:12px;color:#666">
            {o['rationale'][:100]}
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <a href="{o['lien_achat']}">Acheter →</a>
          </td>
        </tr>""" for o in top)

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:750px;margin:auto">
      <h2>Domaines sous-évalués — {datetime.now().strftime('%d/%m/%Y %Hh%M')}</h2>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#f5f5f5">
          <th style="padding:8px;text-align:left">Domaine</th>
          <th style="padding:8px">Score</th><th style="padding:8px">Ratio</th>
          <th style="padding:8px">Valeur</th>
          <th style="padding:8px;text-align:left">Rationale</th>
          <th style="padding:8px">Lien</th>
        </tr></thead><tbody>{rows}</tbody>
      </table>
      <p style="color:#aaa;font-size:11px;margin-top:16px">Domain Agent v7</p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Domain Agent] {len(top)} opportunité(s) — {datetime.now().strftime('%d/%m %Hh')}"
    msg["From"]    = GMAIL_FROM
    msg["To"]      = GMAIL_TO
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_FROM, GMAIL_PASS)
            s.send_message(msg)
        log.info(f"Email envoyé → {GMAIL_TO}")
    except Exception as e:
        log.error(f"Email error: {e}")

# ─── Main ──────────────────────────────────────────────────────────────────────

def run():
    log.info("=== Domain Agent v7 ===")
    sh = get_sheet()
    ensure_sheets(sh)
    ws = sh.worksheet("opportunites")

    seeds = random.sample(SEED_KEYWORDS, 6)
    log.info(f"Seeds : {seeds}")

    candidates   = []   # (name, ext, kw, kw_demand)
    seen         = set()
    rdap_count   = 0

    # ── Étape 1 : collecter les signaux de demande ────────────────────────────
    for kw in seeds:
        registered = fetch_domainsdb(kw)
        kw_demand  = demand_score(kw, registered)
        log.info(f"Demande ({kw}): {kw_demand}/100")

        # Génère des variantes autour du mot-clé
        prefixes = ["", "get", "my", "go", "pro"]
        suffixes = ["", "hq", "app", "hub", "now"]
        for pre in prefixes:
            for suf in suffixes:
                for ext in TARGET_EXTENSIONS:
                    name = f"{pre}{kw}{suf}".strip()
                    key  = name + ext
                    if key not in seen and 3 <= len(name) <= 15:
                        seen.add(key)
                        candidates.append((name, ext, kw, kw_demand))

        time.sleep(0.5)

    # Mélange pour éviter les biais de seed
    random.shuffle(candidates)
    log.info(f"{len(candidates)} variantes à vérifier (max {MAX_RDAP} RDAP)")

    # ── Étape 2 : vérifier disponibilité ─────────────────────────────────────
    available = []
    for name, ext, kw, kw_demand in candidates:
        if rdap_count >= MAX_RDAP:
            log.info(f"Limite RDAP atteinte ({MAX_RDAP})")
            break
        domain_full = name + ext
        rdap_count += 1
        if is_available(domain_full):
            log.info(f"Disponible : {domain_full}")
            available.append((name, ext, kw, kw_demand))
        time.sleep(0.3)

    log.info(f"{len(available)} domaines disponibles")

    # ── Étape 3 : tendances + scoring ─────────────────────────────────────────
    all_scored = []
    trend_cache: dict[str, int] = {}

    for name, ext, kw, kw_demand in available[:MAX_SCORED]:
        # Tendance (avec cache pour éviter les appels redondants)
        if kw not in trend_cache:
            time.sleep(random.randint(2, 4))
            trend_cache[kw] = get_trend(kw)
            log.info(f"Trend ({kw}): {trend_cache[kw]}/100")
        trend = trend_cache[kw]

        result = score(name, ext, kw, kw_demand, trend)
        all_scored.append(result)

    # ── Étape 4 : écriture Sheet ──────────────────────────────────────────────
    all_scored.sort(key=lambda x: x["score_global"], reverse=True)
    log.info(f"Total scorés : {len(all_scored)}")

    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for o in all_scored:
        rows.append([
            now, o["domaine"], o["extension"], o["prix_achat"],
            o["valeur_estimee"], o["ratio_x"],
            o["score_global"], o["score_ratio"], o["score_liquidite"],
            o["score_tendance"], o["score_seo"],
            o["demande_mot_cle"], o["tendance"], o["backlinks"],
            o["mot_cle_source"], o["rationale"], o["lien_achat"],
        ])

    if rows:
        ws.append_rows(rows, value_input_option="RAW")
        log.info(f"{len(rows)} lignes écrites dans Google Sheets")
    else:
        log.warning("Aucune opportunité trouvée")

    # ── Étape 5 : alertes email ───────────────────────────────────────────────
    alerts = [o for o in all_scored if o["score_global"] >= SCORE_ALERT]
    if alerts:
        send_alert(alerts)
        log.info(f"{len(alerts)} alertes envoyées")
    else:
        log.info("Aucune alerte — seuil non atteint")

    log.info("=== Scan terminé v7 ===")

if __name__ == "__main__":
    run()

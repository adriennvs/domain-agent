"""
Domain Opportunity Agent — v8
Sources :
  - Sedo RSS    → domaines EXPIRANTS (historique, enchère en cours)
  - DomainsDB   → domaines DISPONIBLES (jamais enregistrés)
  - RDAP        → vérification disponibilité (timeout 3s, max 30 checks)
  - Heuristique → estimation valeur
  - Google Trends (HTTP direct)
  - OpenPageRank (backlinks)
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
import xml.etree.ElementTree as ET
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

SCORE_ALERT  = 65
MAX_RDAP     = 30
MAX_SCORED   = 50

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

SEED_KEYWORDS = [
    "ai", "pay", "trade", "cloud", "data", "tech",
    "health", "legal", "shop", "fund", "hub", "pro",
    "market", "smart", "fast", "care", "lab", "go",
    "invest", "saas", "crypto", "digital", "code", "app",
]

TARGET_EXTENSIONS = [".com", ".com", ".com", ".ai", ".io"]

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
            "date_scan", "type", "domaine", "extension",
            "prix_achat", "date_fin_enchere", "prix_enchere",
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

# ─── Source 1 : Sedo RSS — domaines EXPIRANTS ─────────────────────────────────

def fetch_sedo_rss() -> list[dict]:
    """
    Récupère les domaines en enchère Sedo via leur flux RSS public.
    Retourne des domaines avec historique + date de fin d'enchère.
    """
    domains = []
    urls = [
        "https://sedo.com/us/buy-domains/expiring-domains/?rss=1",
        "https://sedo.com/us/buy-domains/expiring-domains/?rss=1&language=us",
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                log.warning(f"Sedo RSS status {r.status_code}")
                continue

            root = ET.fromstring(r.content)
            ns   = {"media": "http://search.yahoo.com/mrss/"}

            for item in root.findall(".//item"):
                title       = item.findtext("title", "").strip()
                link        = item.findtext("link", "").strip()
                description = item.findtext("description", "").strip()
                pub_date    = item.findtext("pubDate", "").strip()

                # Extrait le nom de domaine depuis le titre
                domain_full = title.lower().strip()
                if "." not in domain_full or len(domain_full) > 60:
                    continue

                parts = domain_full.rsplit(".", 1)
                if len(parts) != 2:
                    continue
                name = parts[0]
                ext  = "." + parts[1]

                if len(name) < 2 or len(name) > 25:
                    continue

                # Extrait le prix depuis la description
                price = 0.0
                price_match = re.search(r"\$\s*([\d,]+)", description)
                if price_match:
                    price = float(price_match.group(1).replace(",", ""))

                # Extrait la date de fin d'enchère
                end_date = ""
                date_match = re.search(
                    r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\w+ \d{1,2},?\s*\d{4})",
                    description
                )
                if date_match:
                    end_date = date_match.group(1)
                elif pub_date:
                    end_date = pub_date[:16]

                domains.append({
                    "type":            "EXPIRANT",
                    "domaine":         name,
                    "extension":       ext,
                    "prix_achat":      max(12, int(price * 0.92)),  # USD→EUR
                    "date_fin_enchere": end_date,
                    "prix_enchere":    int(price * 0.92),
                    "lien_achat":      link or f"https://sedo.com/search/?keyword={name}",
                    "mot_cle_source":  "sedo_rss",
                    "keyword_demand":  60,  # score de liquidité de base pour domaines avec historique
                })

            log.info(f"Sedo RSS → {len(domains)} domaines expirants")
            if domains:
                break

        except Exception as e:
            log.warning(f"Sedo RSS error: {e}")

    return domains

# ─── Source 2 : DomainsDB — domaines DISPONIBLES ──────────────────────────────

def fetch_domainsdb(keyword: str) -> list[dict]:
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
                    "name":        name_part,
                    "ext":         "." + zone,
                })
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"DomainsDB ({keyword}/{zone}): {e}")
    return results

def demand_score(registered: list[dict]) -> int:
    n = len(registered)
    if n >= 60: return 100
    if n >= 40: return 85
    if n >= 25: return 70
    if n >= 15: return 55
    if n >= 8:  return 40
    if n >= 3:  return 25
    return 10

# ─── RDAP — disponibilité ─────────────────────────────────────────────────────

def is_available(domain_full: str) -> bool:
    try:
        r = requests.get(
            f"https://rdap.org/domain/{domain_full}",
            timeout=3, headers=HEADERS
        )
        return r.status_code == 404
    except Exception:
        return False

# ─── Estimation valeur heuristique ────────────────────────────────────────────

def estimate_value(name: str, ext: str, kw_demand: int) -> int:
    base   = EXT_BASE.get(ext, 100)
    length = len(name)
    if length <= 3:    lm = 8.0
    elif length <= 5:  lm = 4.0
    elif length <= 7:  lm = 2.0
    elif length <= 9:  lm = 1.2
    elif length <= 12: lm = 0.8
    else:              lm = 0.4
    dm = 1.0 + (kw_demand / 100) * 1.5
    return max(50, int(base * lm * dm))

# ─── Google Trends ────────────────────────────────────────────────────────────

def get_trend(keyword: str) -> int:
    kw = keyword.replace("-", " ")
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get(
            "https://trends.google.com/trends/explore",
            params={"q": kw, "date": "today 3-m", "geo": "", "hl": "fr"},
            timeout=10,
        )
        time.sleep(1)
        r2 = s.get(
            "https://trends.google.com/trends/api/explore",
            params={
                "hl": "fr", "tz": -60,
                "req": json.dumps({
                    "comparisonItem": [{"keyword": kw, "geo": "", "time": "today 3-m"}],
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
            params={"hl": "fr", "tz": -60,
                    "req": json.dumps(req), "token": token},
            timeout=10,
        )
        raw3 = r3.text.lstrip(")]}'").strip()
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

# ─── OpenPageRank ─────────────────────────────────────────────────────────────

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

def score_domain(domain: dict, trend: int) -> dict:
    name       = domain["domaine"].lower()
    ext        = domain["extension"]
    prix_achat = domain.get("prix_achat", 12)
    kw_demand  = domain.get("keyword_demand", 0)
    dtype      = domain.get("type", "DISPONIBLE")

    valeur = estimate_value(name, ext, kw_demand)

    # Bonus valeur pour domaines expirants (historique prouvé)
    if dtype == "EXPIRANT":
        valeur = int(valeur * 1.4)

    ratio = valeur / max(prix_achat, 1)

    # Score ratio (40%)
    if ratio >= 100:   sr = 100
    elif ratio >= 50:  sr = 90
    elif ratio >= 20:  sr = 75
    elif ratio >= 10:  sr = 60
    elif ratio >= 5:   sr = 40
    elif ratio >= 2:   sr = 20
    else:              sr = 5

    # Score liquidité (25%)
    sl = int(kw_demand) if kw_demand else 0

    # Score tendance (20%)
    st = int(trend) if trend else 0

    # Score SEO (15%)
    seo_score, backlinks = get_seo(name + ext)
    seo_score = int(seo_score) if seo_score else 0
    backlinks = int(backlinks) if backlinks else 0

    score_global = int(sr * 0.40 + sl * 0.25 + st * 0.20 + seo_score * 0.15)

    # Rationale
    parts = [f"valeur ~{valeur}€ pour {prix_achat}€ (x{int(ratio)})"]
    if dtype == "EXPIRANT":
        parts.append("domaine avec historique")
        if domain.get("date_fin_enchere"):
            parts.append(f"enchère jusqu'au {domain['date_fin_enchere']}")
    if kw_demand >= 50:
        parts.append(f"mot-clé populaire ({kw_demand}/100)")
    if trend >= 40:
        parts.append(f"tendance {trend}/100")
    if backlinks > 0:
        parts.append(f"{backlinks} backlinks")

    return {
        **domain,
        "score_global":      score_global,
        "score_ratio":       sr,
        "score_liquidite":   sl,
        "score_tendance":    st,
        "score_seo":         seo_score,
        "valeur_estimee":    valeur,
        "ratio_x":           round(ratio, 1),
        "demande_mot_cle":   kw_demand,
        "tendance":          trend,
        "backlinks":         backlinks,
        "rationale":         " · ".join(parts),
    }

# ─── Email ────────────────────────────────────────────────────────────────────

def send_alert(opps: list[dict]):
    if not (GMAIL_FROM and GMAIL_TO and GMAIL_PASS):
        return
    top  = sorted(opps, key=lambda x: x["score_global"], reverse=True)[:5]
    rows = "".join(f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <strong>{o['domaine']}{o['extension']}</strong><br>
            <span style="font-size:11px;background:{'#FAEEDA' if o['type']=='EXPIRANT' else '#F1EFE8'};
                         padding:1px 6px;border-radius:4px">
              {o['type']}
            </span>
            {f"<span style='font-size:11px;color:#888'> · enchère {o['date_fin_enchere']}</span>" if o.get('date_fin_enchere') else ""}
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            <span style="background:#EAF3DE;color:#27500A;padding:2px 8px;
                         border-radius:10px;font-weight:bold">
              {o['score_global']}/100
            </span>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center;font-weight:bold">
            x{o['ratio_x']}
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            {o['prix_achat']}€ → ~{o['valeur_estimee']}€
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;font-size:12px;color:#666">
            {o['rationale'][:100]}
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <a href="{o['lien_achat']}">Voir →</a>
          </td>
        </tr>""" for o in top)

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:750px;margin:auto">
      <h2>Domain Agent — {datetime.now().strftime('%d/%m/%Y %Hh%M')}</h2>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#f5f5f5">
          <th style="padding:8px;text-align:left">Domaine</th>
          <th style="padding:8px">Score</th><th style="padding:8px">Ratio</th>
          <th style="padding:8px">Valeur</th>
          <th style="padding:8px;text-align:left">Rationale</th>
          <th style="padding:8px">Lien</th>
        </tr></thead><tbody>{rows}</tbody>
      </table>
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

# ─── Main ─────────────────────────────────────────────────────────────────────

def run():
    log.info("=== Domain Agent v8 ===")
    sh = get_sheet()
    ensure_sheets(sh)
    ws = sh.worksheet("opportunites")

    all_candidates = []
    seen           = set()
    trend_cache    = {}

    # ── Source 1 : Sedo RSS — domaines EXPIRANTS ──────────────────────────────
    log.info("Collecte Sedo RSS...")
    sedo_domains = fetch_sedo_rss()
    for d in sedo_domains[:30]:
        key = d["domaine"] + d["extension"]
        if key not in seen:
            seen.add(key)
            all_candidates.append(d)

    log.info(f"{len(all_candidates)} domaines expirants Sedo")

    # ── Source 2 : DomainsDB — domaines DISPONIBLES ───────────────────────────
    log.info("Collecte DomainsDB...")
    seeds      = random.sample(SEED_KEYWORDS, 6)
    rdap_count = 0

    ddb_candidates = []
    for kw in seeds:
        registered = fetch_domainsdb(kw)
        kw_demand  = demand_score(registered)
        log.info(f"DomainsDB ({kw}): {len(registered)} enregistrés, demande {kw_demand}/100")

        prefixes = ["", "get", "my", "go", "pro"]
        suffixes = ["", "hq", "app", "hub", "now"]
        for pre in prefixes:
            for suf in suffixes:
                for ext in TARGET_EXTENSIONS:
                    name = f"{pre}{kw}{suf}".strip()
                    key  = name + ext
                    if key not in seen and 3 <= len(name) <= 15:
                        seen.add(key)
                        ddb_candidates.append({
                            "type":            "DISPONIBLE",
                            "domaine":         name,
                            "extension":       ext,
                            "prix_achat":      12,
                            "date_fin_enchere": "",
                            "prix_enchere":    0,
                            "mot_cle_source":  kw,
                            "keyword_demand":  kw_demand,
                            "lien_achat": (
                                f"https://www.godaddy.com/domainsearch/find"
                                f"?checkAvail=1&domainToCheck={name}{ext}"
                            ),
                        })
        time.sleep(0.5)

    # Vérification RDAP sur les disponibles
    random.shuffle(ddb_candidates)
    for d in ddb_candidates:
        if rdap_count >= MAX_RDAP:
            break
        rdap_count += 1
        if is_available(d["domaine"] + d["extension"]):
            log.info(f"Disponible : {d['domaine']}{d['extension']}")
            all_candidates.append(d)
        time.sleep(0.3)

    log.info(f"Total candidats : {len(all_candidates)}")

    # ── Scoring ───────────────────────────────────────────────────────────────
    all_scored = []
    for d in all_candidates[:MAX_SCORED]:
        kw = d.get("mot_cle_source", d["domaine"][:8])
        if kw not in trend_cache:
            time.sleep(random.randint(2, 4))
            trend_cache[kw] = get_trend(kw)
            log.info(f"Trend ({kw}): {trend_cache[kw]}/100")
        scored = score_domain(d, trend_cache[kw])
        all_scored.append(scored)

    # ── Écriture Sheet ────────────────────────────────────────────────────────
    all_scored.sort(key=lambda x: x["score_global"], reverse=True)
    log.info(f"Total scorés : {len(all_scored)}")

    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for o in all_scored:
        rows.append([
            now,
            o.get("type", "DISPONIBLE"),
            o["domaine"],
            o["extension"],
            o.get("prix_achat", 12),
            o.get("date_fin_enchere", ""),
            o.get("prix_enchere", 0),
            o["valeur_estimee"],
            o["ratio_x"],
            o["score_global"],
            o["score_ratio"],
            o["score_liquidite"],
            o["score_tendance"],
            o["score_seo"],
            o["demande_mot_cle"],
            o["tendance"],
            o["backlinks"],
            o.get("mot_cle_source", ""),
            o["rationale"],
            o["lien_achat"],
        ])

    if rows:
        ws.append_rows(rows, value_input_option="RAW")
        log.info(f"{len(rows)} lignes écrites")
    else:
        log.warning("Aucune opportunité trouvée")

    # ── Alertes ───────────────────────────────────────────────────────────────
    alerts = [o for o in all_scored if o["score_global"] >= SCORE_ALERT]
    if alerts:
        send_alert(alerts)
        log.info(f"{len(alerts)} alertes envoyées")
    else:
        log.info("Aucune alerte")

    log.info("=== Scan terminé v8 ===")

if __name__ == "__main__":
    run()

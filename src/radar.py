#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radar de anuncios de kleinanzeigen.de → alertas por Telegram.
Ejecutado por GitHub Actions (cron horario). Estado en JSON versionado por git.

Principios:
  * Sondeo ligero: solo página 1 por query, ordenada por más recientes.
  * Ficha del anuncio solo si es nuevo o si su tarjeta cambió.
  * Ante 403/429/captcha: abortar (nunca reintentar en bucle).
  * Sin login, sin cookies persistentes: solo páginas públicas.
Ver README.md para montaje y pruebas.
"""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from bs4 import BeautifulSoup
from curl_cffi import requests as creq

TZ_OUT = ZoneInfo("Europe/Madrid")   # horario de mensajes y state
TZ_SITE = ZoneInfo("Europe/Berlin")  # horario del sitio (mismo offset que Madrid)
MESES = ["ene", "feb", "mar", "abr", "may", "jun",
         "jul", "ago", "sep", "oct", "nov", "dic"]
SORT_CANDIDATES = ["sorter=newestOfferTime", "sortby=newest", ""]
EMOJI = {"nuevo": "🆕", "bajada": "📉", "subida": "📈", "precio_aparecido": "💰",
         "retirado": "🗑️", "republicado": "🔁", "editado": "✏️", "pausa": "🛑",
         "baseline": "✅", "latido": "📋"}
BUNDES = ("Baden-Württemberg|Bayern|Berlin|Brandenburg|Bremen|Hamburg|Hessen|"
          "Mecklenburg-Vorpommern|Niedersachsen|Nordrhein-Westfalen|Rheinland-Pfalz|"
          "Saarland|Sachsen-Anhalt|Sachsen|Schleswig-Holstein|Thüringen")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = bool(os.environ.get("RADAR_DRY_RUN")) or not (TELEGRAM_TOKEN and TELEGRAM_CHAT)
FAST = bool(os.environ.get("RADAR_FAST"))
FORCE_HEARTBEAT = bool(os.environ.get("RADAR_FORCE_HEARTBEAT"))
COMMIT_MSG_PATH = Path("/tmp/radar_commit_msg")


# ---------------------------------------------------------------- utilidades
def log(msg: str) -> None:
    print(msg, flush=True)


def now_madrid() -> datetime:
    return datetime.now(TZ_OUT)


def today() -> date:
    return now_madrid().date()


def to_iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def from_iso(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def hash_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


def fmt_eur(v) -> str:
    return f"{v:,.0f}".replace(",", ".") + " €"


def fmt_days(n: int) -> str:
    if n <= 0:
        return "hoy"
    return f"{n} día" + ("s" if n != 1 else "")


def fmt_date(d: date | None, ref: date | None = None) -> str:
    if d is None:
        return "?"
    ref = ref or today()
    if d == ref:
        return "hoy"
    if d == ref - timedelta(days=1):
        return "ayer"
    s = f"{d.day} {MESES[d.month - 1]}"
    if d.year != ref.year:
        s += f" {d.year}"
    return s


# ---------------------------------------------------------------- precios
def extract_price_token(text: str) -> str:
    if "Zu verschenken" in (text or ""):
        return "Zu verschenken"
    m = re.search(r"\d[\d.,]*\s*€\s*(?:VB\b)?", text or "")
    if m:
        return m.group(0).strip()
    if re.search(r"\bVB\b", text or ""):
        return "VB"
    return ""


def parse_price_text(tok: str) -> dict:
    """'4.500 €'→4500 · '4.500 € VB'→4500+VB · 'VB'→None+VB · 'Zu verschenken'→0"""
    p = {"value": None, "negotiable": False, "free": False}
    tok = (tok or "").strip()
    if not tok:
        return p
    if "verschenken" in tok.lower():
        p["free"], p["value"] = True, 0
        return p
    if "VB" in tok:
        p["negotiable"] = True
    m = re.search(r"\d[\d.,]*", tok)
    if m:
        s = m.group(0).replace(".", "").replace(",", ".")
        try:
            v = float(s)
            p["value"] = int(v) if v == int(v) else v
        except ValueError:
            pass
    return p


def price_label(p: dict | None) -> str:
    p = p or {}
    if p.get("free"):
        return "Gratis 🎁"
    v = p.get("value")
    if v is None:
        return "VB" if p.get("negotiable") else "sin precio"
    return fmt_eur(v) + (" VB" if p.get("negotiable") else "")


def ad_price_label(ad: dict) -> str:
    p = ad.get("price") or {}
    if p.get("free"):
        return "Gratis 🎁 (Zu verschenken)"
    if p.get("value") is None:
        if p.get("negotiable"):
            return "VB (sin cifra)"
        return "Sin precio (anuncio de búsqueda)" if ad.get("is_search") else "Sin precio"
    return fmt_eur(p["value"]) + (" VB" if p.get("negotiable") else "")


# ---------------------------------------------------------------- fechas del sitio
DATE_TOKEN_RE = re.compile(
    r"(Heute(?:\s*,?\s*\d{1,2}:\d{2})?|Gestern(?:\s*,?\s*\d{1,2}:\d{2})?|"
    r"vor\s+\d+\s+(?:Tagen?|Stunden?|Wochen?|Monaten?)|\d{1,2}\.\d{1,2}\.\d{4})"
)


def parse_site_date(text: str) -> date | None:
    t = (text or "").strip().lower()
    if not t:
        return None
    td = today()
    if t.startswith("heute"):
        return td
    if t.startswith("gestern"):
        return td - timedelta(days=1)
    m = re.search(r"vor\s+(\d+)\s+(tag|tagen|stunde|stunden|woche|wochen|monat|monaten)", t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        if u.startswith("stunde"):
            return td
        if u.startswith("woche"):
            n *= 7
        if u.startswith("monat"):
            n *= 30
        return td - timedelta(days=n)
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", t)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- títulos
def norm_title(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9äöüß]+", " ", (s or "").lower())).strip()


def title_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()


def title_compatible(old: str, new: str) -> bool:
    """True si 'new' es 'old' con añadidos: el alt de la imagen del listado
    ensucia el título con 'Región - Ciudad'. Evita abrir la ficha cada hora
    por un falso cambio de título."""
    o, n = norm_title(old), norm_title(new)
    if not o or not n:
        return True
    return n == o or n.startswith(o) or o.startswith(n)


def diff_snippet(old: str, new: str) -> str:
    if not old or not new:
        return f"«{(new or old or '')[:200]}…»"
    sm = difflib.SequenceMatcher(None, old, new)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            frag = (new[j1:j2] or old[i1:i2]).strip()
            if frag:
                return f"«…{frag[:200]}…»"
    return "cambio menor"


# ---------------------------------------------------------------- config y state
def load_config() -> dict:
    with open("config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("queries", ["alleweder"])
    if not cfg["queries"]:
        raise ValueError("config.yaml: 'queries' está vacío")
    cfg.setdefault("state_file", "state.pilot.json")
    cfg.setdefault("report_file", "PILOT_REPORT.md")
    cfg.setdefault("base_url", "https://www.kleinanzeigen.de")
    cfg.setdefault("sort_param", "auto")
    cfg.setdefault("min_pause_s", 2.0)
    cfg.setdefault("max_pause_s", 5.0)
    cfg.setdefault("startup_jitter_max_min", 10)
    cfg.setdefault("request_timeout_s", 15)
    cfg.setdefault("detail_recheck_hours", 36)
    cfg.setdefault("detail_recheck_max_per_run", 2)
    cfg.setdefault("price_threshold_eur", None)
    cfg.setdefault("proxy_url", "")
    cfg.setdefault("photo_send_bytes", True)
    cfg.setdefault("chart_enabled", True)
    cfg.setdefault("chart_min_distinct_prices", 2)
    alerts = cfg.setdefault("alerts", {})
    for k in ("nuevo", "bajada", "subida", "precio_aparecido", "retirado",
              "republicado", "editado", "fallo", "latido"):
        alerts.setdefault(k, True)
    return cfg


def new_state(cfg: dict) -> dict:
    return {
        "state_version": 2,
        "meta": {
            "created": now_madrid().isoformat(timespec="seconds"),
            "queries": cfg["queries"],
            "sort_param_detected": None,
            "consecutive_blocked": 0,
            "pause_until": None,
            "last_heartbeat": None,
            "robots": {},
            "events": [],
        },
        "ads": {},
    }


def load_state(path: str) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[state] ERROR leyendo {path}: {e} — se reinicia el estado "
            "(el anterior sigue en el historial de git)")
        return None


def save_state(path: str, state: dict) -> None:
    tmp = Path(path + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def add_event(state: dict, etype: str, ad_id, text: str) -> dict:
    ev = {"ts": now_madrid().isoformat(timespec="seconds"), "type": etype,
          "id": ad_id, "text": (text or "")[:200]}
    evs = state["meta"].setdefault("events", [])
    evs.append(ev)
    del evs[:-300]
    return ev


def priced_points(hist) -> list:
    return [(from_iso(d), v) for d, v in (hist or []) if v is not None]


def history_line(hist) -> str:
    pts = priced_points(hist)
    if not pts:
        return "sin precios registrados"

    def one(d, v):
        return f"{fmt_eur(v)} ({fmt_date(d)})"

    if len(pts) > 5:
        parts = [one(*pts[0]), "…", one(*pts[-2]), one(*pts[-1])]
    else:
        parts = [one(*p) for p in pts]
    return " → ".join(parts)


def last_priced(hist):
    pts = priced_points(hist)
    return pts[-1][1] if pts else None


def first_priced(hist):
    pts = priced_points(hist)
    return pts[0][1] if pts else None


# ---------------------------------------------------------------- HTTP
class Fetcher:
    """curl_cffi con impersonación de Chrome, peticiones secuenciales
    y pausas aleatorias. Registra cada código HTTP en el log."""

    def __init__(self, cfg: dict):
        proxies = None
        if cfg.get("proxy_url"):
            proxies = {"http": cfg["proxy_url"], "https": cfg["proxy_url"]}
        self.s = creq.Session(impersonate="chrome", proxies=proxies)
        self.cfg = cfg
        self.n = 0
        self.log: list = []
        self._first = True

    def get(self, url: str, referer: str | None = None):
        if not self._first:
            time.sleep(random.uniform(self.cfg["min_pause_s"], self.cfg["max_pause_s"]))
        self._first = False
        headers = {"Accept-Language": "de-DE,de;q=0.9,en;q=0.7"}
        if referer:
            headers["Referer"] = referer
        t0 = time.time()
        r = self.s.get(url, headers=headers, timeout=self.cfg["request_timeout_s"])
        dt = time.time() - t0
        self.n += 1
        self.log.append((url, r.status_code, dt))
        log(f"[HTTP {r.status_code}] {url} ({dt:.1f} s)")
        return r


def looks_like_block(r) -> bool:
    if r.status_code in (403, 429):
        return True
    if r.status_code == 200:
        head = r.text[:5000].lower()
        return any(s in head for s in ("just a moment", "challenge-platform",
                                       "are you a human", "captcha",
                                       "enable javascript and cookies"))
    return False


def search_url(cfg: dict, query: str, sort_param: str) -> str:
    slug = re.sub(r"\s+", "-", query.strip().lower())
    url = f"{cfg['base_url'].rstrip('/')}/s-{slug}/k0"
    if sort_param:
        url += "?" + sort_param
    return url


# ---------------------------------------------------------------- parsing
def extract_card(el, base_url: str) -> dict | None:
    ad_id = el.get("data-ad-id")
    a = el.find("a", href=re.compile(r"/s-anzeige/"))
    if a is None:
        return None
    href = a.get("href", "")
    if not ad_id:
        m = re.search(r"/s-anzeige/[^/]+/(\d+)", href)
        ad_id = m.group(1) if m else None
    if not ad_id:
        return None

    title = ""
    h = el.find(["h2", "h3"]) or el.select_one('[class*="title"]')
    if h is not None:
        title = h.get_text(" ", strip=True)
    img = el.find("img")
    if not title and img is not None:
        title = (img.get("alt") or "").strip()
    if not title:
        title = (a.get("title") or a.get("aria-label") or "").strip()
    if not title:
        title = a.get_text(" ", strip=True)[:80]
    title = title.strip()

    ntext = el.get_text(" ", strip=True)
    low = ntext.lower()
    # precio: primero el elemento específico, luego todo el texto de la tarjeta
    pel = el.select_one('[class*="price"]')
    price_tok = extract_price_token(pel.get_text(" ", strip=True)) if pel is not None else ""
    if not price_tok:
        price_tok = extract_price_token(ntext)
    price = parse_price_text(price_tok)
    dm = DATE_TOKEN_RE.search(ntext)
    publish = parse_site_date(dm.group(0)) if dm else None

    loc = ""
    lm = re.search(r"\b\d{5}\s+[A-Za-zÄÖÜäöüß.\-()]{2,40}", ntext)
    if lm:
        loc = lm.group(0).strip()
    else:
        km = re.search(r"\bKreis\s+[A-Za-zäöüß.\-]{2,40}", ntext)
        if km:
            loc = km.group(0).strip()
        else:
            bm = re.search(rf"([A-ZÄÖÜ][A-Za-zäöüß.\- ]{{2,30}}),\s*({BUNDES})", ntext)
            if bm:
                loc = f"{bm.group(1).strip()} ({bm.group(2)})"

    img_url = ""
    if img is not None:
        img_url = img.get("src") or img.get("data-src") or ""
        if not img_url:
            img_url = (img.get("srcset") or "").split(",")[0].strip().split(" ")[0]
        if img_url.startswith("//"):
            img_url = "https:" + img_url

    return {
        "id": str(ad_id),
        "title": title,
        "price": price,
        "publish": publish,
        "location": loc,
        "reserved": "reserviert" in low,
        "shipping": "versand" in low,
        "commercial": "gewerblich" in low,
        "is_search": "gesuch" in low,
        "detail_url": href if href.startswith("http") else base_url.rstrip("/") + href,
        "img_url": img_url,
    }


def parse_search(html: str, base_url: str) -> dict:
    """Página 1 de resultados. Multi-fallback: data-ad-id → enlaces /s-anzeige/.
    structure_ok=False ⇒ presunto bloqueo o rediseño (no confundir con 0 resultados)."""
    soup = BeautifulSoup(html, "html.parser")
    out = {"cards": [], "structure_ok": False, "more_pages": False,
           "total": None, "method": "?"}
    out["more_pages"] = bool(re.search(r"seite[:=]2\b|Seite\s*2\s*von", html))

    nodes = soup.select("[data-ad-id]")
    method = "attr data-ad-id"
    if not nodes:
        seen_ids, nodes = set(), []
        for a in soup.select('a[href*="/s-anzeige/"]'):
            m = re.search(r"/s-anzeige/[^/]+/(\d+)", a.get("href", ""))
            if not m or m.group(1) in seen_ids:
                continue
            seen_ids.add(m.group(1))
            # contenedor de la tarjeta completa: el <article>/<li> del anuncio,
            # no el mero enlace (así el texto incluye precio, ubicación e insignias)
            container = a.find_parent(["article", "li"]) or a.parent or a
            nodes.append(container)
        method = "href /s-anzeige/"

    for el in nodes:
        try:
            c = extract_card(el, base_url)
        except Exception as e:
            log(f"[parse] tarjeta descartada: {e}")
            continue
        if c:
            out["cards"].append(c)

    text_all = soup.get_text(" ", strip=True)
    m = (re.search(r"\(([\d.,]+)\)\s*Anzeigen", text_all)
         or re.search(r"([\d.,]+)\s*(?:Anzeigen|Ergebnisse|Treffer)\s*(?:gefund\w*)?", text_all))
    if m:
        try:
            out["total"] = int(m.group(1).replace(".", ""))
        except ValueError:
            pass
    low = text_all.lower()
    if out["cards"] or out["total"] == 0 or any(
            s in low for s in ("keine anzeigen", "keine treffer", "nichts gefunden",
                               "keine ergebnisse", "leider nichts")):
        out["structure_ok"] = True
    out["method"] = method
    return out


def parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    def og(prop):
        t = soup.find("meta", attrs={"property": prop})
        return (t.get("content") or "").strip() if t else ""

    h1 = soup.find("h1")
    title = (h1.get_text(" ", strip=True) if h1 else "") or og("og:title")

    desc = ""
    d = (soup.select_one('[id*="viewad-description"]')
         or soup.select_one('[class*="viewad-description"]'))
    if d is not None:
        desc = re.sub(r"[ \t]+", " ", d.get_text("\n", strip=True))
    if not desc:
        desc = og("og:description") or og("description")
    desc = re.sub(r"(?i)^beschreibung\s*", "", desc).strip()
 
    price = None
    pel = soup.select_one('[id*="price"]') or soup.select_one('[class*="price"]')
    if pel is not None:
        price = parse_price_text(extract_price_token(pel.get_text(" ", strip=True)))

    seller = ""
    ce = (soup.select_one('[id*="viewad-contact"]')
          or soup.select_one('[class*="membercard"]')
          or soup.select_one('[class*="seller"]'))
    if ce is not None:
        sa = ce.find("a")
        if sa is not None:
            seller = sa.get_text(" ", strip=True)
        if not seller:
            seller = ce.get_text(" ", strip=True)[:40]
    if not seller:
        sa = soup.select_one('a[href*="/s-seiten/"]')
        if sa is not None:
            seller = sa.get_text(" ", strip=True)
    if seller.lower() in ("zum profil", "profil", "anbieter", "kontakt", "nachricht",
                          "mitglied", "verkäufer", "verkaeufer", "user", "member"):
        seller = ""   # texto de un botón, no un nombre: no sirve para el matching
                           
    loc_el = (soup.select_one("#viewad-locality")
              or soup.select_one('[class*="locality"]')
              or soup.select_one('[id*="locality"]'))
    location = loc_el.get_text(" ", strip=True) if loc_el is not None else ""

    low = soup.get_text(" ", strip=True).lower()
    return {
        "title": title,
        "description": desc,
        "price": price,
        "location": location,
        "seller_name": seller,
        "seller_type": "Gewerblich" if "gewerblich" in low else ("Privat" if "privat" in low else None),
        "photo": og("og:image"),
        "shipping": bool(soup.select_one('[class*="shipping"], [id*="shipping"]'))
                    or "versand möglich" in low,
        "reserved": "reserviert" in low,
    }


def fetch_detail(fetcher: Fetcher, cfg: dict, url: str) -> dict | None:
    try:
        r = fetcher.get(url, referer=cfg["base_url"] + "/")
    except Exception as e:
        log(f"[http] excepción en ficha: {e}")
        return None
    if r.status_code != 200:
        return {"status": r.status_code}
    if looks_like_block(r):
        return {"status": 403}
    d = parse_detail(r.text)
    d["status"] = 200
    return d


def fetch_photo(fetcher: Fetcher, url: str) -> bytes | None:
    if not url:
        return None
    try:
        r = fetcher.get(url)
        ct = (r.headers or {}).get("content-type", "")
        if r.status_code == 200 and len(r.content) < 8_000_000 \
                and (ct.startswith("image") or r.content[:2] in (b"\xff\xd8", b"\x89P")):
            return r.content
    except Exception as e:
        log(f"[foto] error: {e}")
    return None


# ---------------------------------------------------------------- Telegram
def tg_post(method: str, data=None, files=None):
    if DRY_RUN:
        preview = (data or {}).get("text") or (data or {}).get("caption") \
            or (json.dumps(data, ensure_ascii=False)[:300] if data else str(list((files or {}).keys())))
        log(f"[DRY-RUN TG] {method}: {str(preview)[:600]}")
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        r = creq.post(url, data=data, files=files, timeout=30)
        if r.status_code != 200:
            log(f"[TG] {method} → {r.status_code}: {r.text[:200]}")
        return r
    except Exception as e:
        log(f"[TG] {method} excepción: {e}")
        return None


def send_text(text: str) -> None:
    tg_post("sendMessage", {"chat_id": TELEGRAM_CHAT, "text": text[:3900]})


def send_alert(text: str, photo: bytes | None = None, chart: bytes | None = None) -> None:
    """Texto + foto y/o gráfica. Caption si cabe; si no, el texto va en mensaje aparte."""
    if photo is None and chart is None:
        send_text(text)
        return
    caption = text if len(text) <= 1000 else None
    if photo is not None and chart is not None:
        media = [{"type": "photo", "media": "attach://f0"},
                 {"type": "photo", "media": "attach://f1"}]
        if caption:
            media[0]["caption"] = caption
        files = {"f0": ("ad.jpg", photo, "image/jpeg"),
                 "f1": ("chart.png", chart, "image/png")}
        r = tg_post("sendMediaGroup", {"chat_id": TELEGRAM_CHAT, "media": json.dumps(media)}, files)
        ok = r is not None and getattr(r, "status_code", 0) == 200
    else:
        blob, fname, mime = ((photo, "ad.jpg", "image/jpeg") if photo is not None
                             else (chart, "chart.png", "image/png"))
        data = {"chat_id": TELEGRAM_CHAT}
        if caption:
            data["caption"] = caption
        r = tg_post("sendPhoto", data, {"photo": (fname, blob, mime)})
        ok = r is not None and getattr(r, "status_code", 0) == 200
    if not ok or caption is None:
        send_text(text)   # el texto siempre llega, cueste lo que cueste


# ---------------------------------------------------------------- gráfica
def make_chart_png(hist, title: str, cfg: dict) -> bytes | None:
    pts = priced_points(hist)
    if not cfg.get("chart_enabled", True):
        return None
    if len({v for _, v in pts}) < cfg.get("chart_min_distinct_prices", 2):
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        xs = list(range(len(pts)))
        ys = [v for _, v in pts]
        labels = [fmt_date(d) for d, _ in pts]
        fig, ax = plt.subplots(figsize=(8, 4), dpi=100)
        ax.plot(xs, ys, marker="o", linewidth=2, color="#c0392b")
        ax.set_xticks(xs, labels, rotation=30, ha="right")
        ax.grid(alpha=0.3)
        ax.set_ylabel("Preis (€)")
        ax.set_title(title[:60], fontsize=11)
        ax.annotate(fmt_eur(ys[-1]), (xs[-1], ys[-1]), textcoords="offset points",
                    xytext=(0, 8), ha="right")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log(f"[chart] error: {e}")
        return None


# ---------------------------------------------------------------- mensajes
def published_suffix(ad: dict) -> str:
    pd = from_iso(ad.get("publish_date"))
    word = "publicado"
    if pd is None:
        pd = datetime.fromisoformat(ad["first_seen"]).date()
        word = "en seguimiento"
    n = (today() - pd).days
    if n <= 0:
        return " · ⏱️ publicado hoy"
    return f" · ⏱️ {n} día{'s' if n != 1 else ''} {word}"


def price_change_message(cfg, kind, ad, old_price, new_price) -> str:
    delta = (new_price.get("value") or 0) - (old_price.get("value") or 0)
    sign = "−" if delta < 0 else "+"
    head = "📉 Bajada de precio" if delta < 0 else "📈 Subida de precio"
    L = [f"{head} {sign}{fmt_eur(abs(delta))}", ad["title"]]
    init = first_priced(ad.get("history"))
    cur = f"💰 {price_label(new_price)} (antes {price_label(old_price)}"
    if init is not None and init != new_price.get("value"):
        cur += f" · inicial {fmt_eur(init)}"
    L.append(cur + ")")
    L.append(f"📊 {history_line(ad.get('history'))}")
    L.append(f"📍 {ad.get('location') or '?'} · {ad.get('seller_type') or '?'}" + published_suffix(ad))
    thr = cfg.get("price_threshold_eur")
    if thr and new_price.get("value") is not None and old_price.get("value") is not None \
            and new_price["value"] <= thr < old_price["value"]:
        L.append(f"🔥 Ha cruzado a la baja el umbral configurado (≤ {fmt_eur(thr)}).")
    L.append(f"🔗 {ad['detail_url']}")
    return "\n".join(L)


def price_appeared_message(ad, price) -> str:
    L = [f"💰 Precio aparecido: {price_label(price)}", ad["title"],
         f"📍 {ad.get('location') or '?'} · {ad.get('seller_type') or '?'}" + published_suffix(ad),
         f"🔗 {ad['detail_url']}"]
    return "\n".join(L)


def edit_message(ad, notes) -> str:
    L = [f"✏️ Editado — {ad['title']}"]
    for what, old, new in notes:
        if what == "descripción":
            L.append(f"• Descripción modificada: {new}")
        elif what == "reserviert":
            L.append(f"• {new}")
        else:
            L.append(f"• {what.capitalize()}: «{old}» → «{new}»")
    L.append(f"🔗 {ad['detail_url']}")
    return "\n".join(L)


def retire_message(ad) -> str:
    fs = datetime.fromisoformat(ad["first_seen"]).date()
    n = (today() - fs).days
    dur = "activo hoy" if n <= 0 else f"activo {n} día{'s' if n != 1 else ''}"
    lp = last_priced(ad.get("history"))
    L = ["🗑️ Retirado (o vendido)", ad["title"]]
    if lp is not None:
        L.append(f"💰 Precio final: {price_label(ad.get('price') or {})} · {dur} "
                 f"(desde {fmt_date(fs)})")
    else:
        L.append(f"💰 Sin precio en todo su seguimiento · {dur} (desde {fmt_date(fs)})")
    L.append(f"🔗 {ad['detail_url']} (puede que ya no abra)")
    return "\n".join(L)


# ---------------------------------------------------------------- lógica central
def find_republication(ads: dict, exclude_id: str, seller: str, title: str):
    best = None
    for aid, a in ads.items():
        if aid == exclude_id or a.get("status") != "retired":
            continue
        if not a.get("seller_name"):
            continue
        if a["seller_name"].strip().lower() != seller.strip().lower():
            continue
        r = title_ratio(a.get("title", ""), title)
        if r >= 0.72 and (best is None or r > best[0]):
            best = (r, a)
    return best[1] if best else None


def process_new_ad(state, cfg, fetcher, card, sources, first_run, events_run,
                   allow_detail=True) -> None:
    ads = state["ads"]
    ad_id = card["id"]
    prev = ads.get(ad_id)          # None, o registro retirado (reaparición)
    reappeared = prev is not None
    want = lambda k: (not first_run) and cfg["alerts"].get(k, True)

    d = fetch_detail(fetcher, cfg, card["detail_url"]) if allow_detail else None
    d = d if (d and d.get("status") == 200) else {}
    desc = d.get("description") or ""
    seller = d.get("seller_name") or ""
    photo_url = d.get("photo") or card.get("img_url") or ""

    price = card["price"]
    if price.get("value") is None and (d.get("price") or {}).get("value") is not None:
        price = d["price"]     # la ficha enseña un precio que la tarjeta no mostró

    title = (d.get("title") or card["title"] or "(sin título)").strip()

    photo_bytes = None
    if not first_run and cfg["photo_send_bytes"] and photo_url:
        photo_bytes = fetch_photo(fetcher, photo_url)

    now_iso = now_madrid().isoformat(timespec="seconds")
    ads[ad_id] = {
        "id": ad_id,
        "title": title,
        "detail_url": card["detail_url"],
        "location": card["location"] or d.get("location") or "",
        "seller_name": seller,
        "seller_type": "Gewerblich" if card["commercial"] else (d.get("seller_type") or "Privat"),
        "shipping": bool(card["shipping"] or d.get("shipping")),
        "is_search": bool(card["is_search"]),
        "reserved": bool(card["reserved"] or d.get("reserved")),
        "publish_date": to_iso(card["publish"]) or (prev or {}).get("publish_date"),
        "price": price,
        "history": ((prev or {}).get("history") or []) + [[to_iso(today()), price.get("value")]],
        "description": desc,
        "description_hash": hash_text(desc),
        "photo_url": photo_url,
        "status": "active",
        "first_seen": (prev or {}).get("first_seen") or now_iso,
        "last_seen": now_iso,
        "last_detail_check": now_iso,
        "queries": sorted(set((prev or {}).get("queries", [])) | sources.get(ad_id, set())),
    }
    ad = ads[ad_id]

    if first_run:
        add_event(state, "baseline", ad_id, title)
        return

    # ---- mensaje 🆕 ----
    L = ["🆕 Nuevo anuncio"]
    if reappeared:
        L.append(f"♻️ Reaparece un anuncio ya visto (mismo ID). "
                 f"Histórico previo: {history_line(prev.get('history'))}")
    L.append(title)
    L.append(f"💰 {ad_price_label(ad)}")
    meta = f"📍 {ad['location'] or '?'} · {ad['seller_type'] or '?'}"
    if ad["shipping"]:
        meta += " · 📦 Versand möglich"
    L.append(meta)
    if ad.get("publish_date"):
        dd = (today() - from_iso(ad["publish_date"])).days
        L.append(f"📅 Publicado: {fmt_date(from_iso(ad['publish_date']))} (hace {fmt_days(dd)})")
    L.append(f"🔗 {ad['detail_url']}")
    if desc:
        L.append("")
        ext = desc[:300] + ("…" if len(desc) > 300 else "")
        L.append(f"«{ext}»")

    others = [a for a in ads.values() if a["status"] == "active" and a["id"] != ad_id
              and (a["price"] or {}).get("value") is not None]
    if price.get("value") is not None and (
            not others or price["value"] < min(a["price"]["value"] for a in others)):
        L.append(f"🥇 Es el más barato de los {len(others) + 1} anuncios con precio en seguimiento.")
    thr = cfg.get("price_threshold_eur")
    if thr and price.get("value") is not None and price["value"] <= thr:
        L.append(f"🔥 Por debajo del umbral configurado (≤ {fmt_eur(thr)}).")

    if want("republicado") and seller:
        rep = find_republication(ads, ad_id, seller, title)
        if rep:
            lp = last_priced(rep.get("history"))
            L.append("")
            L.append("🔁 Posible republicación: mismo vendedor y título casi idéntico "
                     "a un anuncio ya visto.")
            if lp is not None:
                L.append(f"No llegó a venderse a {fmt_eur(lp)} — pista útil para negociar.")
            L.append(f"Histórico anterior: {history_line(rep.get('history'))}")
            events_run.append(add_event(state, "republicado", ad_id, title))

    if want("nuevo"):
        send_alert("\n".join(L), photo=photo_bytes)
    events_run.append(add_event(state, "nuevo", ad_id, title))


def process_existing_ad(state, cfg, fetcher, ad, card, sources, first_run,
                        events_run) -> bool:
    """Actualiza un anuncio activo. Devuelve True si se abrió su ficha."""
    now_iso = now_madrid().isoformat(timespec="seconds")
    ad["last_seen"] = now_iso
    ad["queries"] = sorted(set(ad.get("queries", [])) | sources.get(ad["id"], set()))

    old_price = ad.get("price") or {}
    old_title = (ad.get("title") or "").strip()
    old_reserved = bool(ad.get("reserved"))
    old_shipping = bool(ad.get("shipping"))
    old_desc = ad.get("description") or ""
    new_price = card["price"] or {}
    card_title = (card.get("title") or "").strip()

    price_kind = None
    if old_price.get("value") != new_price.get("value") \
            or old_price.get("free") != new_price.get("free"):
        if old_price.get("value") is None and new_price.get("value") is not None:
            price_kind = "aparecido"
        elif old_price.get("value") is not None and new_price.get("value") is None:
            price_kind = "desaparecido"
        else:
            price_kind = "cambio"
    elif old_price.get("negotiable") != new_price.get("negotiable"):
        price_kind = "flag_vb"

    reserved_changed = card["reserved"] != old_reserved
    # el listado "ensucia" el título (Región - Ciudad del alt de la imagen):
    # solo abrimos ficha si cambia de verdad
    titles_compatible = title_compatible(old_title, card_title)
    need_detail = price_kind in ("aparecido", "desaparecido", "cambio") \
        or (bool(card_title) and not titles_compatible) \
        or reserved_changed

    d: dict = {}
    desc_new = ""
    corrected = False
    if need_detail:
        detail = fetch_detail(fetcher, cfg, ad["detail_url"])
        if detail and detail.get("status") == 200:
            d = detail
            desc_new = d.get("description") or ""
            dp = d.get("price")
            if price_kind == "desaparecido" and dp and dp.get("value") is not None:
                new_price, price_kind, corrected = dp, None, True
                log("[warn] la tarjeta no mostraba precio, pero la ficha sí — no alerto")
            elif price_kind == "cambio" and dp and dp.get("value") is not None \
                    and dp.get("value") == old_price.get("value"):
                new_price, price_kind, corrected = dp, None, True
                log("[warn] la ficha confirma el precio anterior — no alerto de cambio")
            elif dp and dp.get("value") is not None and price_kind in ("cambio", "aparecido"):
                new_price = dp
        else:
            log(f"[warn] ficha de {ad['id']} no accesible "
                f"({(detail or {}).get('status')}); uso la tarjeta")

    if reserved_changed and d and bool(d.get("reserved")) == old_reserved:
        reserved_changed = False
        log("[warn] la insignia Reserviert de la tarjeta no coincide con la ficha — ignoro")

    if d:
        final_title = (d.get("title") or old_title or card_title).strip()
    else:
        final_title = card_title if (card_title and not titles_compatible) else old_title
    title_changed = bool(final_title) and final_title != old_title
    desc_changed = bool(desc_new) and hash_text(desc_new) != ad.get("description_hash")

    # ---- aplicar al estado ----
    if price_kind in ("cambio", "aparecido", "desaparecido"):
        ad["price"] = new_price
        ad.setdefault("history", []).append([to_iso(today()), new_price.get("value")])
    elif price_kind == "flag_vb" or corrected:
        ad["price"] = new_price
    if title_changed:
        ad["title"] = final_title
    if d.get("location"):
        ad["location"] = d["location"]
    elif card["location"]:
        ad["location"] = card["location"]
    if card["publish"]:
        ad["publish_date"] = to_iso(card["publish"])
    if card["commercial"]:
        ad["seller_type"] = "Gewerblich"
    elif d.get("seller_type"):
        ad["seller_type"] = d["seller_type"]
    if d.get("seller_name"):
        ad["seller_name"] = d["seller_name"]
    if d.get("photo"):
        ad["photo_url"] = d["photo"]
    if d:
        ad["shipping"] = bool(ad.get("shipping") or card["shipping"] or d.get("shipping"))
    elif card["shipping"]:
        ad["shipping"] = True
    ad["reserved"] = bool(d.get("reserved")) if d else bool(card["reserved"])
    if need_detail:
        ad["last_detail_check"] = now_iso
        if desc_changed:
            ad["description"] = desc_new
            ad["description_hash"] = hash_text(desc_new)

    if first_run:
        return need_detail

    # ---- alertas ----
    if price_kind == "cambio":
        delta = (new_price.get("value") or 0) - (old_price.get("value") or 0)
        kind = "bajada" if delta < 0 else "subida"
        if cfg["alerts"].get(kind, True):
            photo = (fetch_photo(fetcher, ad.get("photo_url"))
                     if cfg["photo_send_bytes"] and ad.get("photo_url") else None)
            send_alert(price_change_message(cfg, kind, ad, old_price, new_price),
                       photo=photo, chart=make_chart_png(ad.get("history"), ad["title"], cfg))
        events_run.append(add_event(
            state, kind, ad["id"],
            f"{fmt_eur(old_price.get('value'))} → {fmt_eur(new_price.get('value'))}"))
    elif price_kind == "aparecido" and cfg["alerts"].get("precio_aparecido", True):
        send_text(price_appeared_message(ad, new_price))
        events_run.append(add_event(state, "precio_aparecido", ad["id"],
                                    fmt_eur(new_price.get("value"))))

    notes = []
    if price_kind == "desaparecido":
        notes.append(("precio", price_label(old_price), "sin precio"))
    elif price_kind == "flag_vb":
        notes.append(("precio", price_label(old_price), price_label(new_price)))
    if title_changed:
        notes.append(("título", old_title, final_title))
    if desc_changed:
        notes.append(("descripción", "", diff_snippet(old_desc, desc_new)))
    if reserved_changed:
        notes.append(("reserviert", "Reserviert",
                      "ya no está Reserviert ✅" if old_reserved else "ahora está Reserviert"))
    if card["shipping"] and not old_shipping:
        notes.append(("envío", "sin envío", "ahora admite envío 📦"))
    if notes and cfg["alerts"].get("editado", True):
        send_text(edit_message(ad, notes))
        events_run.append(add_event(state, "editado", ad["id"], notes[0][0]))
    return need_detail


def retire_ad(state, cfg, ad, first_run, events_run) -> None:
    ad["status"] = "retired"
    ad["last_seen"] = now_madrid().isoformat(timespec="seconds")
    if first_run:
        return
    if cfg["alerts"].get("retirado", True):
        send_text(retire_message(ad))
    events_run.append(add_event(state, "retirado", ad["id"], ad["title"]))

def run_detail_rechecks(state, cfg, fetcher, events_run, already_checked: set) -> None:
    """Re-visita fichas (rotación, máx. N por ejecución) para detectar ediciones
    de descripción y otros cambios que la tarjeta no refleja."""
    limit_h = cfg.get("detail_recheck_hours", 0)
    if limit_h <= 0:
        return
    now = now_madrid()
    due = []
    for a in state["ads"].values():
        if a.get("status") != "active" or a["id"] in already_checked:
            continue
        try:
            dt = datetime.fromisoformat(a["last_detail_check"]) if a.get("last_detail_check") else None
        except ValueError:
            dt = None
        if dt is None or (now - dt).total_seconds() / 3600 >= limit_h:
            due.append((dt or datetime.min.replace(tzinfo=TZ_OUT), a))
    due.sort(key=lambda t: t[0])
    for _, ad in due[: cfg.get("detail_recheck_max_per_run", 2)]:
        log(f"[recheck] ficha de {ad['id']} ({ad['title'][:40]})")
        detail = fetch_detail(fetcher, cfg, ad["detail_url"])
        ad["last_detail_check"] = now.isoformat(timespec="seconds")
        if not detail or detail.get("status") != 200:
            continue
        d = detail
        notes = []
        if d.get("seller_name"):
            ad["seller_name"] = d["seller_name"]
        if d.get("title") and d["title"] != ad["title"]:
            notes.append(("título", ad["title"], d["title"]))
            ad["title"] = d["title"]
        desc = d.get("description") or ""
        if desc and hash_text(desc) != ad.get("description_hash"):
            notes.append(("descripción", "", diff_snippet(ad.get("description") or "", desc)))
            ad["description"] = desc
            ad["description_hash"] = hash_text(desc)
        old = ad.get("price") or {}
        dp = d.get("price") or {}
        if dp.get("value") is not None:
            if old.get("value") is not None and dp["value"] != old.get("value"):
                kind = "bajada" if dp["value"] < old["value"] else "subida"
                ad["price"] = dp
                ad.setdefault("history", []).append([to_iso(today()), dp["value"]])
                if cfg["alerts"].get(kind, True):
                    send_alert(price_change_message(cfg, kind, ad, old, dp),
                               chart=make_chart_png(ad.get("history"), ad["title"], cfg))
                events_run.append(add_event(
                    state, kind, ad["id"],
                    f"{fmt_eur(old.get('value'))} → {fmt_eur(dp['value'])}"))
            elif old.get("value") is None:
                ad["price"] = dp
                ad.setdefault("history", []).append([to_iso(today()), dp["value"]])
                if cfg["alerts"].get("precio_aparecido", True):
                    send_text(price_appeared_message(ad, dp))
                events_run.append(add_event(state, "precio_aparecido", ad["id"],
                                            fmt_eur(dp["value"])))
        if notes and cfg["alerts"].get("editado", True):
            send_text(edit_message(ad, notes))
            events_run.append(add_event(state, "editado", ad["id"], notes[0][0]))

def handle_all_blocked(state, cfg, qres, events_run) -> None:
    """Escalera: 1º registra · 2º ⚠️ · 3º pausa 12 h. Nunca reintentos en bucle."""
    meta = state["meta"]
    cb = meta.get("consecutive_blocked", 0) + 1
    meta["consecutive_blocked"] = cb
    reason = ", ".join(sorted({str(v.get("reason")) for v in qres.values()}))
    events_run.append(add_event(state, "bloqueo", None, f"motivo: {reason}"))
    log(f"[bloqueo] ejecución bloqueada ({reason}). Consecutivos: {cb}")
    if cb == 2 and cfg["alerts"].get("fallo", True):
        send_text("⚠️ Radar bloqueado (2.ª vez consecutiva)\n"
                  f"kleinanzeigen.de no ha respondido correctamente ({reason}).\n"
                  "Se aborta sin tocar los anuncios. Próximo intento: la siguiente hora.\n"
                  "Si hay un 3.er bloqueo seguido, el radar se pausará 12 h.")
    elif cb >= 3:
        pu = now_madrid() + timedelta(hours=12)
        meta["pause_until"] = pu.isoformat()
        events_run.append(add_event(state, "pausa", None, f"hasta {pu:%Y-%m-%d %H:%M}"))
        if cfg["alerts"].get("fallo", True):
            send_text("🛑 Radar en pausa 12 h (3 bloqueos consecutivos)\n"
                      f"Se ignorarán las ejecuciones hasta: {pu:%d %b %H:%M} (Europe/Madrid). "
                      "Al reanudar se resetean los contadores.\n"
                      "Si se repite, valora configurar `proxy_url` en config.yaml.")


def maybe_heartbeat(state, cfg, events_run) -> None:
    """Domingo ~12:00 Europe/Madrid (la ventana cubre el jitter y el cambio DST)."""
    meta = state["meta"]
    now = now_madrid()
    if FORCE_HEARTBEAT:
        due = True
    else:
        last_d = (meta.get("last_heartbeat") or "")[:10]
        due = (now.weekday() == 6 and 11 <= now.hour < 14
               and last_d != now.date().isoformat())
    if not due:
        return
    meta["last_heartbeat"] = now.isoformat(timespec="seconds")
    if not (FORCE_HEARTBEAT or cfg["alerts"].get("latido", True)):
        return
    ads = state["ads"]
    active = [a for a in ads.values() if a["status"] == "active"]
    priced = [a for a in active if (a["price"] or {}).get("value") is not None]
    all_pts = [v for a in ads.values() for _, v in priced_points(a.get("history"))]
    week_ago = now - timedelta(days=7)
    week = [e for e in meta.get("events", [])
            if datetime.fromisoformat(e["ts"]) >= week_ago]
    counts: dict = {}
    for e in week:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    L = ["📋 Latido semanal — el radar sigue vivo",
         f"Queries: {', '.join(cfg['queries'])} · Estado: {cfg['state_file']}",
         f"Anuncios en seguimiento: {len(ads)} "
         f"({len(active)} activos, {len(ads) - len(active)} retirados)"]
    if priced:
        c = min(priced, key=lambda a: a["price"]["value"])
        L.append(f"Con precio: {len(priced)} · el más barato activo: "
                 f"{fmt_eur(c['price']['value'])} — {c['title'][:60]}")
    if all_pts:
        L.append(f"Mínimo histórico visto: {fmt_eur(min(all_pts))}")
    L.append("Esta semana: " + (" · ".join(f"{EMOJI.get(k, '')}{k} {v}".strip()
                                           for k, v in sorted(counts.items())) or "sin cambios"))
    for e in week[-5:]:
        if e["type"] in EMOJI:
            L.append(f"   {EMOJI[e['type']]} {e['text'][:70]} "
                     f"({fmt_date(datetime.fromisoformat(e['ts']).date())})")
    L.append(f"Bloqueos consecutivos: {meta.get('consecutive_blocked', 0)}")
    send_text("\n".join(L))
    Path("keepalive.txt").write_text(
        f"último latido: {now.isoformat(timespec='seconds')}\n", encoding="utf-8")
    events_run.append(add_event(state, "latido", None, f"{len(active)} activos"))


def maybe_check_robots(fetcher, cfg, state) -> None:
    rob = state["meta"].get("robots") or {}
    if rob.get("checked"):
        try:
            if (now_madrid() - datetime.fromisoformat(rob["checked"])) < timedelta(days=7):
                return
        except ValueError:
            pass
    try:
        r = fetcher.get(cfg["base_url"].rstrip("/") + "/robots.txt")
    except Exception as e:
        log(f"[robots] error: {e}")
        return
    if r.status_code != 200:
        log(f"[robots] HTTP {r.status_code} — lo registro y sigo")
        return
    log("[robots] contenido (primeras líneas):\n    " + "\n    ".join(r.text.splitlines()[:25]))
    rules = []
    for l in r.text.splitlines():
        m = re.match(r"(?i)disallow:\s*(\S+)", l.strip())
        if m:
            rules.append(m.group(1))
    slug = re.sub(r"\s+", "-", cfg["queries"][0].strip().lower())
    muestras = [f"/s-{slug}/k0", "/s-anzeige/x/1-1"]

    def _bloq(path):
        return any(path.startswith(rv.split("*")[0]) for rv in rules if rv != "/")

    afectadas = [p for p in muestras if _bloq(p)]
    if afectadas:
        log(f"[robots] ⚠️ robots.txt BLOQUEA nuestras URL ({afectadas}) — detener y revisar")
    else:
        log(f"[robots] ✓ {len(set(rules))} reglas Disallow; ninguna afecta a nuestras "
            f"URL de búsqueda ni de ficha")
    state["meta"]["robots"] = {"checked": now_madrid().isoformat(timespec="seconds"),
                               "blocked_us": bool(afectadas), "n_rules": len(set(rules))}


def detect_sort_param(fetcher, cfg, query) -> str:
    for cand in SORT_CANDIDATES:
        try:
            r = fetcher.get(search_url(cfg, query, cand), referer=cfg["base_url"] + "/")
        except Exception as e:
            log(f"[sort] {cand!r}: excepción {e}")
            continue
        if r.status_code != 200 or looks_like_block(r):
            log(f"[sort] {cand!r}: HTTP {r.status_code}")
            continue
        parsed = parse_search(r.text, cfg["base_url"])
        if not parsed["structure_ok"]:
            log(f"[sort] {cand!r}: estructura no reconocida")
            continue
        dates = [c["publish"] for c in parsed["cards"] if c["publish"]]
        if len(dates) < 2 or all(a >= b for a, b in zip(dates, dates[1:])):
            log(f"[sort] orden elegido: {cand!r}")
            return cand
        log(f"[sort] {cand!r}: fechas no descendentes, pruebo otra opción")
    log("[sort] no pude verificar el orden; uso sin parámetro")
    return ""


def write_report(state, cfg) -> None:
    now = now_madrid()
    ads = list(state["ads"].values())
    active = [a for a in ads if a["status"] == "active"]
    retired = [a for a in ads if a["status"] != "active"]
    all_pts = [v for a in ads for _, v in priced_points(a.get("history"))]
    L = [f"# Informe del radar — {' · '.join(cfg['queries'])}", "",
         f"> Autogenerado por `src/radar.py` (no editar a mano). "
         f"Actualizado: {now:%d %b %Y %H:%M} (Europe/Madrid).", ""]
    L.append(f"**Anuncios en seguimiento:** {len(ads)} · activos: {len(active)} · "
             f"retirados: {len(retired)}")
    if all_pts:
        L.append(f"**Mínimo histórico visto:** {fmt_eur(min(all_pts))} · "
                 f"**máximo:** {fmt_eur(max(all_pts))}")
    priced = [a for a in active if (a["price"] or {}).get("value") is not None]
    if priced:
        c = min(priced, key=lambda a: a["price"]["value"])
        L.append(f"**Más barato activo:** {fmt_eur(c['price']['value'])} — "
                 f"[{c['title'][:60]}]({c['detail_url']})")
    L.append("")

    def row(a):
        t = (a["title"] or "").replace("|", "\\|")[:60]
        loc = (a.get("location") or "?").replace("|", "\\|")
        flags = []
        if a.get("shipping"):
            flags.append("📦")
        if a.get("reserved"):
            flags.append("Reserviert")
        if a.get("is_search"):
            flags.append("Gesuch")
        st = "🗑️ retirado" if a["status"] != "active" else (" ".join(flags) or "activo")
        pub = fmt_date(from_iso(a.get("publish_date")))
        fs = fmt_date(datetime.fromisoformat(a["first_seen"]).date())
        ls = fmt_date(datetime.fromisoformat(a["last_seen"]).date())
        return (f"| [{t}]({a['detail_url']}) | {ad_price_label(a)} | "
                f"{history_line(a.get('history'))} | {pub} | {loc} · {a.get('seller_type') or '?'} | "
                f"{fs} → {ls} | {st} |")

    active.sort(key=lambda a: ((a["price"] or {}).get("value") is None,
                               (a["price"] or {}).get("value") or 0))
    retired.sort(key=lambda a: a.get("last_seen") or "", reverse=True)
    for titulo, grupo in (("## Activos", active), ("## Retirados", retired)):
        if not grupo:
            continue
        L += [titulo, "",
              "| Anuncio | Precio | Histórico | Publicado | Ubicación · vendedor | Visto | Estado |",
              "|---|---|---|---|---|---|---|"]
        L += [row(a) for a in grupo]
    Path(cfg["report_file"]).write_text("\n".join(L) + "\n", encoding="utf-8")


def finish(cfg, state, fetcher, events_run) -> None:
    save_state(cfg["state_file"], state)
    write_report(state, cfg)
    summary = " · ".join(
        f"{EMOJI.get(e['type'], '')}{e['type']} {e['text'][:40]}".strip()
        for e in events_run) or "sin cambios"
    try:
        COMMIT_MSG_PATH.write_text(
            f"radar: {now_madrid():%Y-%m-%d %H:%M} — {summary}"[:200], encoding="utf-8")
    except Exception:
        pass
    codes: dict = {}
    for _, c, _ in fetcher.log:
        codes[c] = codes.get(c, 0) + 1
    log(f"[resumen] peticiones HTTP: {fetcher.n} "
        f"({', '.join(f'{k}×{v}' for k, v in sorted(codes.items())) or 'ninguna'}) · "
        f"eventos: {len(events_run)} · {summary[:150]}")


def run() -> None:
    if DRY_RUN:
        log("[modo] sin TELEGRAM_TOKEN/CHAT_ID → DRY-RUN: los mensajes se imprimen aquí, "
            "no se envían (y no hay jitter).")
    cfg = load_config()
    if not FAST and not DRY_RUN and cfg.get("startup_jitter_max_min", 0) > 0:
        w = random.uniform(0, cfg["startup_jitter_max_min"] * 60)
        log(f"[jitter] espera aleatoria inicial: {w:.0f} s")
        time.sleep(w)

    state = load_state(cfg["state_file"])
    first_run = state is None
    if first_run:
        state = new_state(cfg)
        log(f"[state] primera ejecución: creo {cfg['state_file']} (baseline silencioso)")
    meta = state["meta"]

    if meta.get("pause_until"):
        pu = datetime.fromisoformat(meta["pause_until"])
        if now_madrid() < pu:
            log(f"[pausa] bloqueado hasta {pu:%d %b %H:%M} — esta ejecución no hace peticiones.")
            return
        log("[pausa] fin de la pausa: reseteo contadores de bloqueo.")
        meta["pause_until"] = None
        meta["consecutive_blocked"] = 0

    fetcher = Fetcher(cfg)
    events_run: list = []

    maybe_check_robots(fetcher, cfg, state)

    sort = cfg.get("sort_param", "auto")
    if sort == "auto":
        sort = meta.get("sort_param_detected")
        if sort is None:
            sort = detect_sort_param(fetcher, cfg, cfg["queries"][0])
            meta["sort_param_detected"] = sort
    log(f"[sort] parámetro en uso: {sort!r}")

    # ---- sondeo ligero: página 1 por query ----
    qres: dict = {}
    for q in cfg["queries"]:
        try:
            r = fetcher.get(search_url(cfg, q, sort), referer=cfg["base_url"] + "/")
        except Exception as e:
            log(f"[search] {q}: excepción {e}")
            qres[q] = {"ok": False, "reason": "excepción de red"}
            continue
        if looks_like_block(r):
            qres[q] = {"ok": False, "reason": f"HTTP {r.status_code}/bloqueo"}
            log(f"[search] {q}: BLOQUEADO (HTTP {r.status_code})")
            continue
        if r.status_code != 200:
            qres[q] = {"ok": False, "reason": f"HTTP {r.status_code}"}
            log(f"[search] {q}: HTTP {r.status_code}")
            continue
        parsed = parse_search(r.text, cfg["base_url"])
        log(f"[search] {q}: {len(parsed['cards'])} tarjetas · total={parsed['total']} · "
            f"más páginas={parsed['more_pages']} · método={parsed['method']}")
        if parsed["cards"]:
            c0 = parsed["cards"][0]
            log(f"[search]   muestra: {c0['title'][:60]!r} · {price_label(c0['price'])} · {c0['location']}")
        if not parsed["structure_ok"]:
            qres[q] = {"ok": False, "reason": "página sin estructura conocida (captcha/rediseño)"}
            continue
        qres[q] = {"ok": True, "cards": parsed["cards"],
                   "single_page": not parsed["more_pages"]}

    n_ok = sum(1 for v in qres.values() if v.get("ok"))
    if n_ok == 0:
        handle_all_blocked(state, cfg, qres, events_run)
        finish(cfg, state, fetcher, events_run)
        return
    if n_ok < len(cfg["queries"]):
        log(f"[warn] solo {n_ok}/{len(cfg['queries'])} queries OK — proceso lo visible "
            "y no marco retirados de las queries fallidas")
    elif meta.get("consecutive_blocked"):
        log("[bloqueo] ejecución correcta: reseteo el contador de bloqueos")
        meta["consecutive_blocked"] = 0

    # verificación barata del orden (solo corrige el parámetro para la PRÓXIMA ejecución)
    first_ok = next((v for v in qres.values() if v.get("ok")), None)
    if first_ok and cfg.get("sort_param", "auto") == "auto":
        dates = [c["publish"] for c in first_ok["cards"] if c["publish"]]
        if len(dates) >= 2 and any(a < b for a, b in zip(dates, dates[1:])):
            log("[sort] las fechas NO salen descendentes — pruebo alternativas")
            for cand in [c for c in SORT_CANDIDATES if c != sort]:
                try:
                    r = fetcher.get(search_url(cfg, cfg["queries"][0], cand))
                except Exception:
                    continue
                if r.status_code != 200:
                    continue
                d2 = [c["publish"] for c in parse_search(r.text, cfg["base_url"])["cards"]
                      if c["publish"]]
                if len(d2) >= 2 and all(a >= b for a, b in zip(d2, d2[1:])):
                    log(f"[sort] cambio a {cand!r} para la próxima ejecución")
                    meta["sort_param_detected"] = cand
                    break

    # ---- dedup entre queries por ID ----
    seen: dict = {}
    sources: dict = {}
    for q, res in qres.items():
        if not res.get("ok"):
            continue
        for c in res["cards"]:
            seen.setdefault(c["id"], c)
            sources.setdefault(c["id"], set()).add(q)

    detail_fetched: set = set()
    for i, (ad_id, card) in enumerate(seen.items()):
        ad = state["ads"].get(ad_id)
        if ad is None or ad.get("status") != "active":
            allow_detail = (not first_run) or i < 20   # tope de fichas en la baseline
            process_new_ad(state, cfg, fetcher, card, sources, first_run, events_run,
                           allow_detail)
            if allow_detail:
                detail_fetched.add(ad_id)
        elif process_existing_ad(state, cfg, fetcher, ad, card, sources, first_run,
                                 events_run):
            detail_fetched.add(ad_id)

    # ---- retirados ----
    for ad_id, ad in list(state["ads"].items()):
        if ad.get("status") != "active" or ad_id in seen:
            continue
        srcs = [q for q in (ad.get("queries") or []) if q in qres] or list(qres)
        if not all(qres.get(q, {}).get("ok") for q in srcs):
            log(f"[retirado?] {ad_id}: alguna query fuente falló — no lo doy por retirado")
            continue
        if all(qres.get(q, {}).get("single_page") for q in srcs):
            retire_ad(state, cfg, ad, first_run, events_run)
            continue
        log(f"[retirado?] {ad_id} no está en la página 1 — compruebo su ficha")
        try:
            r = fetcher.get(ad["detail_url"])
        except Exception as e:
            log(f"[retirado?] ficha inaccesible ({e}) — lo dejo activo")
            continue
        if r.status_code in (404, 410):
            retire_ad(state, cfg, ad, first_run, events_run)
        elif r.status_code == 200 and not looks_like_block(r):
            ad["last_seen"] = now_madrid().isoformat(timespec="seconds")
            log(f"[retirado?] {ad_id} sigue vivo (cayó de la página 1)")
        else:
            log(f"[retirado?] ficha HTTP {r.status_code} — lo dejo activo")

    if not first_run:
        run_detail_rechecks(state, cfg, fetcher, events_run, detail_fetched)

    maybe_heartbeat(state, cfg, events_run)

    if first_run:
        n = len([a for a in state["ads"].values() if a["status"] == "active"])
        items = [f"• {a['title'][:60]} — {ad_price_label(a)} · {a.get('location') or '?'}"
                 for a in list(state["ads"].values())[:15]]
        if len(state["ads"]) > 15:
            items.append(f"… y {len(state['ads']) - 15} más")
        send_text(f"✅ Radar activado — {n} anuncios en seguimiento\n"
                  f"Queries: {', '.join(cfg['queries'])} · Estado: {cfg['state_file']}\n\n"
                  + "\n".join(items))
        events_run.append(add_event(state, "baseline", None, f"{n} anuncios"))

    finish(cfg, state, fetcher, events_run)


def crash_alert(tb: str) -> None:
    last = "\n".join(tb.splitlines()[-8:])[:900]
    text = ("⚠️ Radar: error inesperado\n" + last +
            "\n\nLa ejecución se aborta sin tocar el estado. "
            "Próximo intento: la siguiente hora. Revisa el log de GitHub Actions.")
    if DRY_RUN:
        log(f"[DRY-RUN TG] crash:\n{text}")
        return
    try:
        creq.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                  data={"chat_id": TELEGRAM_CHAT, "text": text[:3900]}, timeout=30)
    except Exception as e:
        log(f"[TG] no pude avisar del fallo: {e}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        run()
    except Exception:
        tb = traceback.format_exc()
        log("[FATAL] " + tb)
        try:
            crash_alert(tb)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()

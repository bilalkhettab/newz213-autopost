#!/usr/bin/env python3
"""Newz 213 autopost: one carousel (news card + app promo) per run, at most.

Run every 30 minutes. Each run:
  1. checks the schedule (daily cap, minimum gap, active hours in Algiers time)
  2. fetches the 12 RSS feeds the app reads
  3. groups articles about the same story (port of the app's StoryClusterer)
  4. keeps stories covered by 2+ outlets that were not posted yet
  5. drops sensitive topics (keyword blocklist, then the model's own check)
  6. writes the headline and caption (Claude Haiku 5.5, or a template if no API key)
  7. renders the news card with headless Chrome, converts to JPEG
  8. pushes the image to GitHub (public URL) and publishes the carousel
     through the Instagram API

Usage:
  python autopost.py --dry-run         # everything except push + publish
  python autopost.py                   # real run (needs IG_USER_ID, IG_TOKEN)
  python autopost.py --force           # ignore the schedule (still respects the cap)

Environment:
  ANTHROPIC_API_KEY   optional; without it the template writer is used
  IG_USER_ID          Instagram professional account id
  IG_TOKEN            long-lived Instagram access token
  PUBLIC_BASE_URL     where public/ is served, e.g.
                      https://raw.githubusercontent.com/<owner>/<repo>/main/public
  DAILY_CAP (10), MIN_GAP_MIN (90), ACTIVE_FROM (7), ACTIVE_TO (23)
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import math
import os
import re
import shutil
import string
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
PUBLIC = ROOT / "public"
TEMPLATES = ROOT / "templates"
ALGIERS = ZoneInfo("Africa/Algiers")
UA = "Mozilla/5.0 (Newz213 autopost)"
MODEL = "claude-haiku-5-5"

# Same outlets, feeds and colors as the app (DZ360/Core/Sources.swift).
SOURCES = {
    "tsa": ("TSA", "fr", "https://www.tsa-algerie.com/feed/", "#0B5D3B"),
    "algerie360": ("Algérie360", "fr", "https://www.algerie360.com/feed/", "#E2231A"),
    "observalgerie": ("Observ'Algérie", "fr", "https://www.observalgerie.com/feed/", "#27496D"),
    "algerieeco": ("Algérie Eco", "fr", "https://www.algerie-eco.com/feed/", "#C9692C"),
    "patriotique": ("Algérie Patriotique", "fr", "https://www.algeriepatriotique.com/feed/", "#4B4453"),
    "ennahar-fr": ("Ennahar", "fr", "https://www.ennaharonline.com/fr/feed/", "#1C1C1E"),
    "dzfoot": ("DZFoot", "fr", "https://www.dzfoot.com/feed", "#2E7D32"),
    "echorouk": ("الشروق", "ar", "https://www.echoroukonline.com/feed", "#C8102E"),
    "ennahar": ("النهار", "ar", "https://www.ennaharonline.com/feed/", "#111111"),
    "elkhabar": ("الخبر", "ar", "https://www.elkhabar.com/feed", "#C9692C"),
    "elbilad": ("البلاد", "ar", "https://www.elbilad.net/feed", "#1F6FB2"),
    "awras": ("أوراس", "ar", "https://www.awras.com/feed/", "#7A1F2B"),
}

# Topics the page never posts (see newz213/CLAUDE.md). Checked before any model call.
BLOCK_PATTERNS = [
    # French (word starts, so "parti" does not catch "partie")
    r"\barmée", r"\bmilitaire", r"\bterroris", r"\barrest", r"\binterpell", r"\bsuspect", r"\bcondamn",
    r"\bprison", r"\bmeurtr", r"\bassassin", r"\bviol\b", r"\bviolé", r"\bdrogue", r"\bstupéfiant", r"\bpsychotrope",
    r"\btrafic de\b", r"\btrafiquant", r"\bcorruption", r"\btebboune", r"président de la république",
    r"\bélection", r"\bpartis? politique", r"\bopposition\b", r"\bhirak", r"\bmaroc", r"\bmakhzen", r"\bisraël",
    r"\bgaza\b", r"\biran\b", r"\bdécès", r"\bdécéd", r"\bmorts?\b", r"\bnoyade", r"\bnoyé", r"\baccident",
    r"\bincendie", r"\bsuicide", r"\btrump", r"\bpoutine", r"\bxi jinping", r"\bnetanyahou",
    r"\bguerre", r"\bsanction", r"\bambassad", r"\bdiplomati", r"\bla paix\b",
    # Arabic (plain substrings)
    "الجيش", "عسكري", "إرهاب", "توقيف", "حجز كمية", "مشتبه", "سجن", "قتل", "اغتصاب", "مخدرات", "مهلوسات",
    "فساد", "تبون", "رئيس الجمهورية", "انتخاب", "حزب", "المغرب", "المخزن", "إسرائيل", "الاستيطان",
    "غزة", "إيران", "وفاة", "غرق", "حادث", "حريق", "انتحار", "نتنياهو", "ترمب", "ترامب",
]
BLOCK_RE = re.compile("|".join(BLOCK_PATTERNS), re.IGNORECASE)

# ---------------------------------------------------------------- feeds

def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s)
    s = re.sub(r"<[^>]+>", " ", html.unescape(s))
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_date(raw: str) -> dt.datetime | None:
    raw = raw.strip()
    try:  # RFC 822, "Fri, 20 Nov 2026 09:13:31 +0000"
        d = email.utils.parsedate_to_datetime(raw)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    m = re.match(r"(\d{1,2}):(\d{2})\s*\|\s*(\d{1,2})-(\d{1,2})-(\d{4})", raw)  # El Khabar / El Bilad, Algiers time
    if m:
        hh, mm, d, mo, y = map(int, m.groups())
        return dt.datetime(y, mo, d, hh, mm, tzinfo=ALGIERS)
    return None


def has_arabic(s: str) -> bool:
    return any("؀" <= c <= "ۿ" for c in s)


def fetch_articles() -> list[dict]:
    out = []
    for sid, (name, lang, url, _) in SOURCES.items():
        try:
            xml = http_get(url).decode("utf-8", "ignore")
        except Exception as e:  # one dead feed must not stop the run
            print(f"  feed {sid}: {e}")
            continue
        for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
            t = re.search(r"<title>(.*?)</title>", item, re.S)
            l = re.search(r"<link>(.*?)</link>", item, re.S)
            p = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
            if not (t and l and p):
                continue
            title, published = clean(t.group(1)), parse_date(clean(p.group(1)))
            if not title or not published:
                continue
            body = re.search(r"<content:encoded>(.*?)</content:encoded>", item, re.S) or \
                re.search(r"<description>(.*?)</description>", item, re.S)
            summary = clean(body.group(1)) if body else ""
            summary = re.sub(r"(The post|L’article|L'article) .*$", "", summary).strip()
            img = re.search(r'(?:url="|src=")(https?://[^"]+\.(?:jpe?g|png|webp)[^"]*)', item)
            # Arabic outlets sometimes publish in French: go by the script, like the app.
            art_lang = "ar" if (lang == "ar" and has_arabic(title)) else "fr"
            out.append({
                "source": sid, "title": title, "summary": summary[:600],
                "link": html.unescape(clean(l.group(1))), "published": published,
                "lang": art_lang, "image": img.group(1) if img else None,
            })
    return out

# ---------------------------------------------------------------- clustering (port of StoryClusterer.swift)

FR_STOP = set("""les des une dans pour par sur avec aux est son ses sont qui que quoi dont ont pas plus mais tout tous
toute toutes cette ces cet leur leurs elle elles ils nous vous entre apres avant selon comme fait faire etre avoir deux
trois lors sans sous vers chez aussi deja encore tres bien peu ainsi alors depuis contre autre autres meme lui ete sera
seront etait algerie algerien algerienne algeriens algeriennes pays annee jour jours nouveau nouvelle premier premiere
post appeared first""".split())
AR_STOP = set("""في من علي الي عن مع هذا هذه ذلك تلك التي الذي الذين كان كانت بعد قبل حتي خلال بين عند اجل ان او لم لن قد
ما لا هو هي تم كما حول ضد منذ عبر اكثر كل جزاير جزايري وطن وطني يوم جديد اول سنه عام""".split())


def to_western_digits(s: str) -> str:
    return "".join(chr(ord(c) - 0x0660 + 48) if "٠" <= c <= "٩"
                   else chr(ord(c) - 0x06F0 + 48) if "۰" <= c <= "۹" else c for c in s)


def fr_stem(w: str) -> str:
    for suf in ["ements", "ement", "ations", "ation", "euses", "euse", "eurs", "eur", "ees", "ee", "es", "s", "x"]:
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def ar_stem(w: str) -> str:
    for pre in ["وال", "فال", "بال", "كال", "لل", "ال"]:
        if len(w) > len(pre) + 2 and w.startswith(pre):
            w = w[len(pre):]
            break
    if len(w) > 4 and w[0] in "وف":
        w = w[1:]
    for suf in ["ات", "ون", "ين", "ها", "ه", "ي"]:
        if len(w) > len(suf) + 2 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    return w


def tokens(text: str, lang: str) -> tuple[list[str], set[str]]:
    text = to_western_digits(text)
    nums = {n for n in re.findall(r"\d+(?:[.,]\d+)?", text) if not (n.isdigit() and 2020 <= int(n) <= 2030)}
    if lang == "fr":
        t = unicodedata.normalize("NFD", text.lower())
        t = "".join(c for c in t if unicodedata.category(c) != "Mn")
        t = re.sub(r"\b[ldjmnstc]'|\b(qu|jusqu|lorsqu|puisqu)'", " ", t).replace("’", " ")
        words = [fr_stem(w) for w in re.split(r"[^\w]+", t) if w and w not in FR_STOP]
    else:
        t = re.sub("[ً-ْٰـ]", "", text)
        t = re.sub("[أإآٱ]", "ا", t).replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
        words = [ar_stem(w) for w in re.split(r"[^\w]+", t) if w]
        words = [w for w in words if w not in AR_STOP]
    words = [w for w in words if len(w) >= 3 and not w.isdigit()]
    return words, nums


def cluster(articles: list[dict], threshold: float = 0.22, window_h: float = 36) -> list[list[dict]]:
    docs = []
    for a in articles:
        tw, tn = tokens(a["title"], a["lang"])
        bw, bn = tokens(a["summary"][:220], a["lang"])
        tf: dict[str, float] = {}
        for w in tw:
            tf[w] = tf.get(w, 0) + 2
        for w in bw:
            tf[w] = tf.get(w, 0) + 1
        docs.append((a, tf, tn | bn))
    df: dict[str, dict[str, int]] = {}
    count: dict[str, int] = {}
    for a, tf, _ in docs:
        count[a["lang"]] = count.get(a["lang"], 0) + 1
        d = df.setdefault(a["lang"], {})
        for w in tf:
            d[w] = d.get(w, 0) + 1
    vecs = []
    for a, tf, nums in docs:
        n = count[a["lang"]]
        v = {w: (1 + math.log(f)) * math.log((n + 1) / df[a["lang"]].get(w, 1)) for w, f in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values()))
        if norm:
            v = {k: x / norm for k, x in v.items()}
        vecs.append((a, v, nums))

    def sim(x, y) -> float:
        small, large = (x[1], y[1]) if len(x[1]) < len(y[1]) else (y[1], x[1])
        dot = sum(val * large[w] for w, val in small.items() if w in large)
        if x[2] and y[2]:
            dot *= 0.75 if x[2].isdisjoint(y[2]) else 1.15
        return dot

    groups: list[list] = []
    for doc in sorted(vecs, key=lambda d: d[0]["published"]):
        best = None
        for i, g in enumerate(groups):
            newest = g[-1][0]
            if newest["lang"] != doc[0]["lang"]:
                continue
            if (doc[0]["published"] - newest["published"]).total_seconds() > window_h * 3600:
                continue
            score = max(sim(doc, m) for m in g)
            if score >= threshold and (best is None or score > best[1]):
                best = (i, score)
        if best:
            groups[best[0]].append(doc)
        else:
            groups.append([doc])
    return [sorted((d[0] for d in g), key=lambda a: a["published"], reverse=True) for g in groups]

# ---------------------------------------------------------------- selection

def story_key(story: list[dict]) -> str:
    return hashlib.sha1("|".join(sorted(a["link"] for a in story)).encode()).hexdigest()[:12]


def blocked(story: list[dict]) -> str | None:
    text = " ".join(a["title"] + " " + a["summary"][:300] for a in story)
    m = BLOCK_RE.search(text)
    return m.group(0) if m else None


def candidates(stories: list[list[dict]], state: dict, now: dt.datetime) -> list[list[dict]]:
    seen_links = {l for p in state["posts"] for l in p.get("links", [])} | set(state.get("skipped_links", []))
    out = []
    for s in stories:
        outlets = {a["source"] for a in s}
        if len(outlets) < int(os.environ.get("MIN_OUTLETS", 2)):
            continue
        if (now - s[0]["published"]).total_seconds() > 14 * 3600:  # stale
            continue
        if any(a["link"] in seen_links for a in s):
            continue
        out.append(s)
    # More outlets first, then the freshest.
    out.sort(key=lambda s: (len({a["source"] for a in s}), s[0]["published"]), reverse=True)
    return out

# ---------------------------------------------------------------- writing

SYSTEM = """You write posts for @newz.213, an Instagram page for Newz 213, a free iPhone app that gathers the 12 main Algerian newspapers.
You get several articles from different Algerian papers about the same story. Write one news post about it.

Rules:
- Use only facts present in the articles. Never invent numbers, names, dates or quotes. If papers disagree, say less.
- Language: write everything in the language given (fr = French, ar = Modern Standard Arabic as used by Algerian papers).
- Neutral, factual tone. No question to the reader, no emoji, no em dashes, no "follow us".
- Refuse (publish=false) if the story is about: politics, elections, the president, the army, security forces operations,
  crimes or arrests, court cases, named private individuals, deaths, accidents, disasters with victims,
  religion disputes, or tensions with other countries. Also refuse if the articles are too thin to say anything concrete.
- headline: max 70 characters, a plain statement of the news, no quotation marks, no source name.
- subline: max 110 characters, one concrete detail (a date, a figure, a place) from the articles.
- section: one word, e.g. Économie, Sport, Société, Transport, Météo, Santé, Culture, Tech (or the Arabic equivalent).
- caption_body: 2 to 3 short sentences that add the key facts beyond the headline.
- hashtags: exactly 3, without the # sign, no spaces inside (use _ in Arabic)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "publish": {"type": "boolean"},
        "reason": {"type": "string"},
        "headline": {"type": "string"},
        "subline": {"type": "string"},
        "section": {"type": "string"},
        "caption_body": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["publish", "reason", "headline", "subline", "section", "caption_body", "hashtags"],
    "additionalProperties": False,
}


def write_with_claude(story: list[dict], lang: str) -> dict | None:
    import anthropic  # imported here so the template mode needs no package

    client = anthropic.Anthropic()
    arts = "\n\n".join(
        f"[{SOURCES[a['source']][0]}, {a['published'].astimezone(ALGIERS):%d/%m %H:%M}]\n{a['title']}\n{a['summary'][:500]}"
        for a in story[:5])
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": f"Language: {lang}\n\nArticles:\n\n{arts}"}],
        )
    except anthropic.RateLimitError as e:
        print(f"  claude: rate limited ({e}); skipping this run")
        return None
    except anthropic.APIStatusError as e:
        print(f"  claude: API error {e.status_code}: {e.message}")
        return None
    except anthropic.APIConnectionError as e:
        print(f"  claude: connection error: {e}")
        return None
    if resp.stop_reason == "refusal":
        return {"publish": False, "reason": "model refusal"}
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        print("  claude: unreadable JSON")
        return None
    u = resp.usage
    print(f"  claude: {u.input_tokens} in / {u.output_tokens} out tokens")
    return out


def write_with_template(story: list[dict], lang: str) -> dict:
    lead = story[-1]  # earliest report
    head = lead["title"]
    if len(head) > 90:
        head = head[:88].rsplit(" ", 1)[0] + "…"
    first = re.split(r"(?<=[.!؟?])\s", lead["summary"])[0][:200] if lead["summary"] else ""
    return {"publish": True, "reason": "template", "headline": head, "subline": "",
            "section": "Actualité" if lang == "fr" else "أخبار", "caption_body": first,
            "hashtags": ["Algérie", "Actualité", "Newz213"] if lang == "fr" else ["الجزائر", "أخبار", "Newz213"]}


def build_caption(post: dict, story: list[dict], lang: str) -> str:
    names = []
    for a in story:
        n = SOURCES[a["source"]][0]
        if n not in names:
            names.append(n)
    tags = " ".join("#" + re.sub(r"\s+", "_", h.strip().lstrip("#")) for h in post["hashtags"][:3])
    if lang == "fr":
        src = ("Sources : " if len(names) > 1 else "Source : ") + ", ".join(names) + "."
        app = "Toute la presse algérienne sur Newz 213, gratuit sur iPhone, lien en bio."
    else:
        src = ("المصادر: " if len(names) > 1 else "المصدر: ") + "، ".join(names) + "."
        app = "كل الصحف الجزائرية في Newz 213، مجاني على iPhone، الرابط في البايو."
    head = post["headline"].strip()
    if head and head[-1] not in ".!?…؟":
        head += "."
    parts = [head, post["caption_body"].strip(), src + " " + app, tags]
    caption = "\n\n".join(p for p in parts if p)
    return caption.replace("—", ",").replace("–", "-")

# ---------------------------------------------------------------- rendering

def chrome() -> str:
    for c in [os.environ.get("CHROME_BIN", ""), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("google-chrome") or "", shutil.which("chromium") or "", shutil.which("chromium-browser") or ""]:
        if c and Path(c).exists():
            return c
    sys.exit("Chrome not found (set CHROME_BIN)")


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def render_card(post: dict, story: list[dict], lang: str, out_jpg: Path) -> None:
    from PIL import Image

    names, chips = [], []
    for a in story:
        name, slang, _, color = SOURCES[a["source"]]
        if name in names:
            continue
        names.append(name)
        label = name if slang == "ar" else name.upper()
        cls = ' class="ar"' if has_arabic(name) else ""
        chips.append(f'<span{cls} style="--c:{color}">{esc(label)}</span>')
    now = dt.datetime.now(ALGIERS)
    months_fr = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
    months_ar = ["جانفي", "فيفري", "مارس", "أفريل", "ماي", "جوان", "جويلية", "أوت", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
    date = f"{now.day} {months_fr[now.month - 1]} {now.year}".upper() if lang == "fr" else \
        f"{now.day} {months_ar[now.month - 1]} {now.year}"
    page = string.Template((TEMPLATES / "card.html").read_text(encoding="utf-8")).substitute(
        lang=lang, dir="rtl" if lang == "ar" else "ltr", date=esc(date), section=esc(post["section"]),
        headline=esc(post["headline"]), subline=esc(post.get("subline", "")),
        sources_label="Selon" if lang == "fr" else "حسب",
        chips="".join(chips),
        foot=("Toute la presse algérienne · <b>Newz 213</b>" if lang == "fr"
              else "كل الصحف الجزائرية · <b>Newz 213</b>"))
    tmp_html = TEMPLATES / "_render.html"
    tmp_png = TEMPLATES / "_render.png"
    tmp_html.write_text(page, encoding="utf-8")
    subprocess.run([chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                    "--force-device-scale-factor=1", "--window-size=1080,1350", "--virtual-time-budget=4000",
                    f"--screenshot={tmp_png}", tmp_html.as_uri()],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    Image.open(tmp_png).convert("RGB").save(out_jpg, "JPEG", quality=92)
    tmp_html.unlink(missing_ok=True)
    tmp_png.unlink(missing_ok=True)

# ---------------------------------------------------------------- publishing

def git_push(paths: list[Path], message: str) -> None:
    subprocess.run(["git", "add", *map(str, paths)], cwd=ROOT, check=True)
    subprocess.run(["git", "-c", "user.name=newz213-bot", "-c", "user.email=bot@users.noreply.github.com",
                    "commit", "-m", message], cwd=ROOT, check=True)
    subprocess.run(["git", "push"], cwd=ROOT, check=True)


def ig(method: str, path: str, **params) -> dict:
    base = f"https://graph.instagram.com/{os.environ.get('IG_API_VERSION', 'v21.0')}"
    params["access_token"] = os.environ["IG_TOKEN"]
    data = urllib.parse.urlencode(params).encode()
    url = f"{base}/{path}" + ("" if method == "POST" else "?" + data.decode())
    req = urllib.request.Request(url, data=data if method == "POST" else None, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Instagram API {e.code}: {e.read().decode(errors='ignore')[:400]}") from None


def wait_ready(container: str) -> None:
    for _ in range(30):
        status = ig("GET", container, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {container} status {status}")
        time.sleep(5)
    raise RuntimeError(f"container {container} not ready after 150 s")


def wait_public(url: str) -> None:
    for _ in range(24):
        try:
            urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA}), timeout=15)
            return
        except Exception:
            time.sleep(5)
    raise RuntimeError(f"image not reachable: {url}")


def publish(image_urls: list[str], caption: str) -> str:
    uid = os.environ["IG_USER_ID"]
    children = []
    for u in image_urls:
        wait_public(u)
        c = ig("POST", f"{uid}/media", image_url=u, is_carousel_item="true")["id"]
        wait_ready(c)
        children.append(c)
    parent = ig("POST", f"{uid}/media", media_type="CAROUSEL", children=",".join(children), caption=caption)["id"]
    wait_ready(parent)
    return ig("POST", f"{uid}/media_publish", creation_id=parent)["id"]

# ---------------------------------------------------------------- schedule + state

def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"posts": [], "skipped_links": []}


def save_state(state: dict) -> None:
    state["skipped_links"] = state.get("skipped_links", [])[-800:]
    state["posts"] = state["posts"][-300:]
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def schedule_ok(state: dict, now: dt.datetime, force: bool) -> tuple[bool, str]:
    cap = int(os.environ.get("DAILY_CAP", 10))
    gap = int(os.environ.get("MIN_GAP_MIN", 90))
    start, end = int(os.environ.get("ACTIVE_FROM", 7)), int(os.environ.get("ACTIVE_TO", 23))
    local = now.astimezone(ALGIERS)
    today = [p for p in state["posts"] if dt.datetime.fromisoformat(p["at"]).astimezone(ALGIERS).date() == local.date()]
    if len(today) >= cap:
        return False, f"daily cap reached ({len(today)}/{cap})"
    if force:
        return True, "forced"
    if not (start <= local.hour < end):
        return False, f"outside active hours ({local:%H:%M} Algiers)"
    if state["posts"]:
        last = dt.datetime.fromisoformat(state["posts"][-1]["at"])
        mins = (now - last).total_seconds() / 60
        if mins < gap:
            return False, f"last post {mins:.0f} min ago (< {gap})"
    return True, f"{len(today)}/{cap} today"


def cleanup_public(keep_days: int = 7) -> list[Path]:
    removed = []
    cutoff = dt.date.today() - dt.timedelta(days=keep_days)
    for d in PUBLIC.iterdir():
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name) and dt.date.fromisoformat(d.name) < cutoff:
            shutil.rmtree(d)
            removed.append(d)
    return removed

# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="render only: no git push, no Instagram call")
    ap.add_argument("--force", action="store_true", help="ignore gap and active hours (not the daily cap)")
    ap.add_argument("--max-tries", type=int, default=3, help="stories to try per run before giving up")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    state = load_state()
    ok, why = schedule_ok(state, now, args.force or args.dry_run)
    print(f"schedule: {why}")
    if not ok:
        return

    arts = fetch_articles()
    stories = cluster(arts)
    cands = candidates(stories, state, now)
    print(f"feeds: {len(arts)} articles, {len(stories)} stories, {len(cands)} new multi-outlet candidates")

    use_ai = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    tries = 0
    for story in cands:
        if tries >= args.max_tries:
            break
        lang = story[0]["lang"]
        names = ", ".join(sorted({SOURCES[a['source']][0] for a in story}))
        print(f"- [{lang}] {story[-1]['title'][:90]}  ({names})")
        kw = blocked(story)
        if kw:
            print(f"  skipped: blocklist '{kw}'")
            state.setdefault("skipped_links", []).extend(a["link"] for a in story)
            continue
        tries += 1
        post = write_with_claude(story, lang) if use_ai else write_with_template(story, lang)
        if post is None:
            continue  # transient failure: try again next run
        if not post.get("publish"):
            print(f"  skipped by model: {post.get('reason')}")
            state.setdefault("skipped_links", []).extend(a["link"] for a in story)
            continue

        key = story_key(story)
        day = now.astimezone(ALGIERS).date().isoformat()
        card = (ROOT / "out" if args.dry_run else PUBLIC / day) / f"{key}.jpg"
        render_card(post, story, lang, card)
        caption = build_caption(post, story, lang)
        print(f"  card: {card.relative_to(ROOT)}\n  headline: {post['headline']}")

        if args.dry_run:
            (card.with_suffix(".txt")).write_text(caption, encoding="utf-8")
            print("  dry run: caption saved next to the card, nothing published")
            continue

        base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
        removed = cleanup_public()
        record = {"at": now.isoformat(), "key": key, "lang": lang, "headline": post["headline"],
                  "sources": sorted({a["source"] for a in story}), "links": [a["link"] for a in story],
                  "writer": "claude" if use_ai else "template"}
        state["posts"].append({**record, "status": "pending"})
        save_state(state)
        git_push([card, STATE, *removed], f"post: {post['headline'][:60]}")
        media_id = publish([f"{base}/{day}/{key}.jpg", f"{base}/promo-{lang}.jpg"], caption)
        state["posts"][-1].update(status="published", media_id=media_id)
        save_state(state)
        git_push([STATE], f"published {media_id}")
        print(f"  published: {media_id}")
        return

    save_state(state)
    if not args.dry_run and os.environ.get("GITHUB_ACTIONS"):
        subprocess.run(["git", "add", str(STATE)], cwd=ROOT)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
            git_push([STATE], "state: skipped stories")
    print("nothing posted this run")


if __name__ == "__main__":
    main()

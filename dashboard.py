#!/usr/bin/env python3
"""Newz 213 manual posting desk.

Prepares carousels on this Mac; you post them yourself from Instagram.
Run:  .venv/bin/python dashboard.py   (or double-click dashboard.command)
Then open http://localhost:8213

- Lists today's stories from the 12 feeds (same clustering and topic blocklist as the bot).
- "Prepare" picks the photo, writes the text (Claude Haiku if ANTHROPIC_API_KEY is in the
  environment or in a parent .env, else the paper's own headline) and renders the card.
- Edit headline, detail line, section and caption, then "Re-render".
- Download the two slides (card + promo), copy the caption, post on Instagram (4:5).
- "Mark as posted" records it in state.json (and pushes it) so the bot never repeats it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env() -> None:
    """Read KEY=VALUE lines from the nearest .env files up the tree (never printed)."""
    for d in [ROOT, *ROOT.parents]:
        f = d / ".env"
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
import autopost as ap  # noqa: E402  (after the env is loaded)

PORT = int(os.environ.get("DASHBOARD_PORT", 8213))
WORK = ROOT / "out" / "manual"
RENDER_LOCK = threading.Lock()  # render_card uses shared temp files
STORIES: dict[str, list[dict]] = {}
FETCHED_AT: float = 0


def use_ai() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def job_dir(key: str) -> Path:
    return WORK / dt.datetime.now(ap.ALGIERS).date().isoformat() / key


def story_info(key: str, s: list[dict], state: dict) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    names = []
    for a in s:
        n = ap.SOURCES[a["source"]][0]
        if n not in names:
            names.append(n)
    posted = {l for p in state["posts"] for l in p.get("links", [])}
    return {
        "id": key, "lang": s[0]["lang"], "title": s[0]["title"], "sources": names,
        "papers": len({a["source"] for a in s}),
        "age_min": int((now - s[-1]["published"]).total_seconds() // 60),
        "posted": any(a["link"] in posted for a in s),
        "prepared": (job_dir(key) / "post.json").exists(),
        "links": [a["link"] for a in s][:4],
        "articles": [{"paper": ap.SOURCES[a["source"]][0], "title": a["title"], "link": a["link"],
                      "lang": a["lang"]} for a in s],
    }


def list_stories(refresh: bool) -> list[dict]:
    global FETCHED_AT
    if refresh or not STORIES or time.time() - FETCHED_AT > 600:
        stories = ap.cluster(ap.fetch_articles())
        now = dt.datetime.now(dt.timezone.utc)
        STORIES.clear()
        for s in stories:
            if (now - s[0]["published"]).total_seconds() > 30 * 3600 or ap.blocked(s):
                continue
            STORIES[ap.story_key(s)] = s
        FETCHED_AT = time.time()
    state = ap.load_state()
    out = [story_info(k, s, state) for k, s in STORIES.items()]
    out.sort(key=lambda x: (x["posted"], -x["papers"], x["age_min"]))
    return out


def render(key: str, post: dict) -> dict:
    s = STORIES[key]
    d = job_dir(key)
    lang = post["lang"]
    with RENDER_LOCK:
        tmp = ap.TEMPLATES / "_photo.jpg"
        shutil.copy(d / "photo.jpg", tmp)
        ap.render_card(post, s, lang, d / "card.jpg", tmp, post["photo_src"])
    shutil.copy(ap.PUBLIC / f"promo-{lang}.jpg", d / "promo.jpg")
    if not post.get("caption_edited"):
        post["caption"] = ap.build_caption(post, s, lang)
    (d / "caption.txt").write_text(post["caption"], encoding="utf-8")
    (d / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=1), encoding="utf-8")
    return view(key, post)


def view(key: str, post: dict) -> dict:
    v = int(time.time())
    day = job_dir(key).parent.name
    base = f"/files/{day}/{key}"
    return {**post, "id": key, "card": f"{base}/card.jpg?v={v}", "promo": f"{base}/promo.jpg?v={v}"}


def prepare(key: str) -> dict:
    s = STORIES.get(key)
    if not s:
        return {"error": "Story not found. Refresh the list."}
    d = job_dir(key)
    if (d / "post.json").exists():
        return view(key, json.loads((d / "post.json").read_text(encoding="utf-8")))
    lang = s[0]["lang"]
    with RENDER_LOCK:
        photo, src = ap.pick_photo(s)
        if photo is None:
            return {"error": "No usable photo for this story (feed and article page). Pick another one."}
        d.mkdir(parents=True, exist_ok=True)
        shutil.move(str(photo), d / "photo.jpg")
    post = ap.write_with_claude(s, lang) if use_ai() else ap.write_with_template(s, lang)
    if post is None:
        post = ap.write_with_template(s, lang)
    refused = None
    if not post.get("publish", True):
        refused = post.get("reason") or "refused"
        post = {**ap.write_with_template(s, lang), "reason": refused}
    post.update(lang=lang, photo_src=src, writer="claude" if use_ai() and not refused else "template",
                refused=refused)
    return render(key, post)


def rewrite(key: str) -> dict:
    """Write the text again with Claude (keeps the photo)."""
    s = STORIES.get(key)
    d = job_dir(key)
    if not s or not (d / "post.json").exists():
        return {"error": "Prepare the story first."}
    if not use_ai():
        return {"error": "No ANTHROPIC_API_KEY in .env: can't rewrite with Claude."}
    old = json.loads((d / "post.json").read_text(encoding="utf-8"))
    post = ap.write_with_claude(s, old["lang"])
    if post is None:
        return {"error": "Claude didn't answer. Try again."}
    refused = None if post.get("publish", True) else (post.get("reason") or "refused")
    if refused:
        post = {**ap.write_with_template(s, old["lang"])}
    post.update(lang=old["lang"], photo_src=old["photo_src"], writer="template" if refused else "claude",
                refused=refused, caption_edited=False)
    return render(key, post)


def rerender(key: str, fields: dict) -> dict:
    d = job_dir(key)
    if key not in STORIES or not (d / "post.json").exists():
        return {"error": "Prepare the story first."}
    post = json.loads((d / "post.json").read_text(encoding="utf-8"))
    for k in ("headline", "subline", "section"):
        if k in fields:
            post[k] = fields[k].strip()
    if "caption" in fields and fields["caption"].strip() != post.get("caption", "").strip():
        post["caption"] = fields["caption"].strip()
        post["caption_edited"] = True
    if fields.get("reset_caption"):
        post["caption_edited"] = False
    return render(key, post)


def mark_posted(key: str, posted: bool = True) -> dict:
    """Record (or un-record) a story as posted by hand. Works with or without "Prepare"."""
    s = STORIES.get(key)
    if not s:
        return {"error": "Story not found. Refresh the list."}
    f = job_dir(key) / "post.json"
    post = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"lang": s[0]["lang"], "headline": s[0]["title"]}
    subprocess.run(["git", "pull", "-q", "--rebase", "--autostash"], cwd=ROOT)
    state = ap.load_state()
    links = {a["link"] for a in s}
    if posted:
        if any(p.get("key") == key or links & set(p.get("links", [])) for p in state["posts"]):
            return {"ok": True, "note": "Already marked as posted."}
        state["posts"].append({
            "at": dt.datetime.now(dt.timezone.utc).isoformat(), "key": key, "lang": post["lang"],
            "headline": post["headline"], "sources": sorted({a["source"] for a in s}),
            "links": sorted(links), "writer": "manual", "status": "manual"})
        msg, note = f"manual post: {post['headline'][:60]}", "Marked as posted"
    else:
        before = len(state["posts"])
        state["posts"] = [p for p in state["posts"]
                          if not (p.get("status") == "manual" and (p.get("key") == key or links & set(p.get("links", []))))]
        if len(state["posts"]) == before:
            return {"error": "Only stories marked by hand can be unmarked (the bot's own posts stay)."}
        msg, note = f"unmark manual post: {post['headline'][:60]}", "Unmarked"
    ap.save_state(state)
    try:
        ap.git_push([ap.STATE], msg)
        return {"ok": True, "note": note + " (saved for the bot too)."}
    except Exception:
        return {"ok": True, "note": note + " on this Mac (will sync with the next push)."}


def today_summary() -> dict:
    state = ap.load_state()
    now = dt.datetime.now(ap.ALGIERS)
    today = [p for p in state["posts"]
             if dt.datetime.fromisoformat(p["at"]).astimezone(ap.ALGIERS).date() == now.date()]
    last = state["posts"][-1]["at"] if state["posts"] else None
    mins = None
    if last:
        mins = int((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).total_seconds() // 60)
    return {"today": len(today), "last_min": mins, "now": now.strftime("%H:%M"), "ai": use_ai()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code: int = 200) -> None:
        self.send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                self.send(200, (ROOT / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/stories":
                self.json({"stories": list_stories(q.get("refresh") == ["1"]), **today_summary()})
            elif u.path == "/api/post":
                key = q["id"][0]
                f = job_dir(key) / "post.json"
                self.json(view(key, json.loads(f.read_text(encoding="utf-8"))) if f.exists() else {})
            elif u.path.startswith("/files/"):
                rel = urllib.parse.unquote(u.path[len("/files/"):])
                f = (WORK / rel).resolve()
                if WORK.resolve() not in f.parents or not f.exists():
                    return self.send(404, b"not found", "text/plain")
                self.send(200, f.read_bytes(), "image/jpeg")
            else:
                self.send(404, b"not found", "text/plain")
        except Exception as e:
            self.json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            key = body.get("id", "")
            if u.path == "/api/prepare":
                self.json(prepare(key))
            elif u.path == "/api/rewrite":
                self.json(rewrite(key))
            elif u.path == "/api/rerender":
                self.json(rerender(key, body))
            elif u.path == "/api/posted":
                self.json(mark_posted(key, body.get("posted", True)))
            elif u.path == "/api/reveal":
                subprocess.run(["open", "-R", str(job_dir(key) / "card.jpg")])
                self.json({"ok": True})
            else:
                self.send(404, b"not found", "text/plain")
        except Exception as e:
            self.json({"error": f"{type(e).__name__}: {e}"}, 500)


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"Newz 213 desk: http://localhost:{PORT}  (writer: {'Claude Haiku' if use_ai() else 'paper headline'})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

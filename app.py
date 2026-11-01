#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multilingual Translator — single-file Streamlit app
===================================================
Text + file (TXT / DOCX / PDF / EPUB), 40+ languages, auto-detect,
multi-engine failover, persistent cache, no API keys required.

SINGLE SCRIPT: no requirements.txt needed. Missing packages are installed
automatically on first start (works on Streamlit Community Cloud and locally).

Deploy:
  1. Push this file to a GitHub repo as `app.py`.
  2. share.streamlit.io -> New app -> pick repo, branch, main file `app.py` -> Deploy.
  3. You get a public link like https://<your-app>.streamlit.app

Run locally:
    pip install streamlit
    streamlit run app.py
"""
import os, sys, re, json, time, random, hashlib, threading, subprocess, importlib.util, posixpath
import tempfile, traceback
import urllib.parse, html as _html
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import streamlit as st
import requests

st.set_page_config(page_title="Multilingual Translator", page_icon="🌐", layout="wide")


# ---------------------------------------------------------------------------
# Self-contained bootstrap: installs missing packages automatically so the
# whole app lives in this one file (no requirements.txt required).
# ---------------------------------------------------------------------------
def _pip(args):
    """Install packages using whatever installer exists in this environment:
    pip -> uv (searched in common locations) -> ensurepip -> get-pip.py."""
    import shutil
    args = list(args)
    py = sys.executable
    errs = []

    def run(cmd, label):
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=900)
            importlib.invalidate_caches()
            return True
        except Exception as e:
            msg = (getattr(e, "stderr", None) or str(e)).strip().splitlines()
            errs.append(f"[{label}] " + (msg[-1] if msg else "failed"))
            return False

    pipbase = [py, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    if run(pipbase + args, "pip"):
        return
    if run(pipbase + ["--break-system-packages"] + args, "pip --break-system-packages"):
        return

    # uv (Streamlit Cloud builds its venv with uv, but it may not be on PATH)
    home = os.path.expanduser("~")
    cands = [shutil.which("uv"), os.path.join(os.path.dirname(py), "uv"),
             os.path.join(home, ".local", "bin", "uv"), "/usr/local/bin/uv",
             "/usr/bin/uv", "/home/adminuser/.local/bin/uv", "/home/appuser/.local/bin/uv"]
    for uv in dict.fromkeys(c for c in cands if c and os.path.isfile(c)):
        if run([uv, "pip", "install", "-q", "--python", py] + args, "uv"):
            return

    # create pip inside the venv, then retry
    if run([py, "-m", "ensurepip", "--upgrade"], "ensurepip") and run(pipbase + args, "pip(after ensurepip)"):
        return
    try:
        gp = os.path.join(tempfile.gettempdir(), "get-pip.py")
        r = requests.get("https://bootstrap.pypa.io/get-pip.py", timeout=60)
        r.raise_for_status()
        with open(gp, "wb") as f:
            f.write(r.content)
        if run([py, gp, "-q"], "get-pip") and run(pipbase + args, "pip(after get-pip)"):
            return
    except Exception as e:
        errs.append(f"[get-pip] {e}")

    raise RuntimeError(" | ".join(errs)[-600:] +
                       "  >>> Fix: add a requirements.txt to your GitHub repo (see app instructions).")


def _have(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError, AttributeError):
        return False


# (import name, pip name, required?)
_DEPS = (
    ("requests", "requests", True),
    ("langdetect", "langdetect", True),
    ("docx", "python-docx", False),
    ("pymupdf", "pymupdf", False),
    ("bs4", "beautifulsoup4", False),
    ("lxml", "lxml", False),
    ("translators", "translators", False),
)


@st.cache_resource(show_spinner="First start: installing dependencies (one-time, ~1-2 min)…")
def _bootstrap():
    failed = {}
    for m, p, req in _DEPS:
        if _have(m):
            continue
        try:
            _pip([p])
        except Exception as e:
            failed[p] = str(e)
            print(f"[bootstrap] could not install {p}: {e}")
    return failed


_BOOT_FAILED = _bootstrap()


import requests
try:
    from langdetect import detect as _ldetect, DetectorFactory
    DetectorFactory.seed = 0
except Exception:                      # fallback: script-based guess only
    _ldetect = None
log = print

OUT_DIR = os.path.join(tempfile.gettempdir(), "multilang_translator_out")
os.makedirs(OUT_DIR, exist_ok=True)


def _import_pymupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        import fitz as pymupdf
        return pymupdf


def _safe_base(p):
    return re.sub(r"[^\w.\-]+", "_", os.path.splitext(os.path.basename(p))[0]) or "book"


def _out_path(path, tgt, ext):
    d = os.path.dirname(os.path.abspath(path)) or OUT_DIR
    return os.path.join(d, f"{_safe_base(path)}_{tgt}{ext}")


# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------
LANGUAGE_NAMES = {
    "en": "English", "hi": "Hindi", "fr": "French", "es": "Spanish", "de": "German",
    "ta": "Tamil", "te": "Telugu", "bn": "Bengali", "mr": "Marathi", "gu": "Gujarati",
    "kn": "Kannada", "ml": "Malayalam", "pa": "Punjabi", "or": "Odia", "ur": "Urdu",
    "ar": "Arabic", "zh-CN": "Chinese (Simplified)", "zh-TW": "Chinese (Traditional)",
    "ja": "Japanese", "ko": "Korean", "ru": "Russian", "pt": "Portuguese", "it": "Italian",
    "nl": "Dutch", "tr": "Turkish", "vi": "Vietnamese", "th": "Thai", "id": "Indonesian",
    "pl": "Polish", "el": "Greek", "he": "Hebrew", "sv": "Swedish", "da": "Danish",
    "fi": "Finnish", "no": "Norwegian", "cs": "Czech", "ro": "Romanian", "hu": "Hungarian",
    "uk": "Ukrainian", "fa": "Persian", "sw": "Swahili", "tl": "Filipino",
}
NAME_TO_CODE = {v: k for k, v in LANGUAGE_NAMES.items()}
NAME_TO_CODE["Auto-detect"] = "auto"
SOURCE_CHOICES = ["Auto-detect"] + sorted(LANGUAGE_NAMES.values())
TARGET_CHOICES = sorted(LANGUAGE_NAMES.values())
SCRIPT_RE = {
    "hi": re.compile(r"[\u0900-\u097F]"), "mr": re.compile(r"[\u0900-\u097F]"),
    "bn": re.compile(r"[\u0980-\u09FF]"), "ta": re.compile(r"[\u0B80-\u0BFF]"),
    "te": re.compile(r"[\u0C00-\u0C7F]"), "gu": re.compile(r"[\u0A80-\u0AFF]"),
    "kn": re.compile(r"[\u0C80-\u0CFF]"), "ml": re.compile(r"[\u0D00-\u0D7F]"),
    "pa": re.compile(r"[\u0A00-\u0A7F]"), "or": re.compile(r"[\u0B00-\u0B7F]"),
    "ur": re.compile(r"[\u0600-\u06FF]"), "ar": re.compile(r"[\u0600-\u06FF]"),
    "he": re.compile(r"[\u0590-\u05FF]"), "ru": re.compile(r"[\u0400-\u04FF]"),
    "uk": re.compile(r"[\u0400-\u04FF]"), "el": re.compile(r"[\u0370-\u03FF]"),
    "th": re.compile(r"[\u0E00-\u0E7F]"), "zh-CN": re.compile(r"[\u4E00-\u9FFF]"),
    "zh-TW": re.compile(r"[\u4E00-\u9FFF]"), "ja": re.compile(r"[\u3040-\u30FF\u4E00-\u9FFF]"),
    "ko": re.compile(r"[\uAC00-\uD7AF]"),
}
_LD_ALIAS = {"zh-cn": "zh-CN", "zh-tw": "zh-TW", "iw": "he", "nb": "no", "nn": "no", "fil": "tl"}


def auto_detect(text):
    if not text or not text.strip():
        return "en"
    try:
        if _ldetect is None:
            raise RuntimeError("langdetect unavailable")
        raw = _ldetect(text)
    except Exception:
        best, hits = "en", 0
        for code, rx in SCRIPT_RE.items():
            n = len(rx.findall(text[:2000]))
            if n > hits:
                best, hits = code, n
        return best
    code = _LD_ALIAS.get(raw.lower(), raw.lower())
    if code in LANGUAGE_NAMES:
        return code
    for k in LANGUAGE_NAMES:
        if k.split("-")[0] == code:
            return k
    return "en"


# ---------------------------------------------------------------------------
# Progress (state only; no UI calls — Streamlit polling handles rendering)
# ---------------------------------------------------------------------------
class Progress:
    def __init__(self):
        self._lock = threading.Lock()
        self.current = 0.0
        self.total = 1.0
        self.msg = "Ready"
        self.done = False
        self.error = False

    def set(self, cur, total, msg=None):
        with self._lock:
            self.current = cur
            self.total = max(1e-9, total)
            if msg is not None:
                self.msg = msg

    def finish(self, msg="Complete"):
        with self._lock:
            self.done, self.error = True, False
            self.current = self.total
            self.msg = msg

    def fail(self, msg="Error"):
        with self._lock:
            self.error = True
            self.msg = msg

    def pct(self):
        with self._lock:
            return max(0, min(100, int(100 * self.current / self.total)))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class Config:
    max_retries: int = 1
    request_timeout: float = 20.0
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    mymemory_email: str = ""    # optional boost for MyMemory quota (not a key)
    cache_path: str = field(
        default_factory=lambda: os.path.join(tempfile.gettempdir(),
                                             "translation_cache_multilang.json"))
    engine_order: Tuple[str, ...] = (
        "google", "bing", "ts_google", "ts_bing",
        "ts_deepl", "ts_yandex", "ts_papago", "ts_baidu", "ts_alibaba",
        "duckduckgo", "apertium",
        "mymemory", "libretranslate", "lingva",
        "argos",
    )
    default_engine_limits: Dict[str, float] = field(
        default_factory=lambda: {"min_interval": 0.3, "max_concurrent": 3,
                                 "rest_every": 0, "rest_seconds": 0.0})
    engine_limits: Dict[str, Dict[str, float]] = field(default_factory=lambda: {
        "google":         {"min_interval": 0.05, "max_concurrent": 10, "rest_every": 1000, "rest_seconds": 3.0},
        "bing":           {"min_interval": 0.08, "max_concurrent": 8,  "rest_every": 500,  "rest_seconds": 5.0},
        "ts_google":      {"min_interval": 0.10, "max_concurrent": 8,  "rest_every": 500,  "rest_seconds": 3.0},
        "ts_bing":        {"min_interval": 0.10, "max_concurrent": 8,  "rest_every": 500,  "rest_seconds": 5.0},
        "ts_deepl":       {"min_interval": 0.30, "max_concurrent": 3},
        "ts_yandex":      {"min_interval": 0.50, "max_concurrent": 2,  "rest_every": 50,   "rest_seconds": 10.0},
        "ts_papago":      {"min_interval": 0.30, "max_concurrent": 3},
        "ts_baidu":       {"min_interval": 0.30, "max_concurrent": 3},
        "ts_alibaba":     {"min_interval": 0.30, "max_concurrent": 3},
        "duckduckgo":     {"min_interval": 0.50, "max_concurrent": 2},
        "apertium":       {"min_interval": 0.20, "max_concurrent": 5},
        "mymemory":       {"min_interval": 0.30, "max_concurrent": 2,  "rest_every": 100,  "rest_seconds": 10.0},
        "libretranslate": {"min_interval": 0.30, "max_concurrent": 2,  "rest_every": 100,  "rest_seconds": 10.0},
        "lingva":         {"min_interval": 0.15, "max_concurrent": 4},
        "argos":          {"min_interval": 0.0,  "max_concurrent": 1},
    })
    max_wait_all_down: float = 20.0
    save_every: int = 25


# ---------------------------------------------------------------------------
# Chunking helpers
# ---------------------------------------------------------------------------
_SENT = re.compile(r"[.!?\u0964][\"'\u201d\u2019)\]]*\s+")
_CLAUSE = re.compile(r"[,;:\u2014\u2013][\"'\u201d\u2019)\]]*\s+")
_SPACE = re.compile(r"\s+")


def split_text(text, limit):
    if limit < 20:
        limit = 20
    if len(text) <= limit:
        return [text]
    chunks, i, n = [], 0, len(text)
    while i < n:
        if n - i <= limit:
            chunks.append(text[i:])
            break
        window = text[i:i + limit]
        cut = 0
        for pat in (_SENT, _CLAUSE, _SPACE):
            last = None
            for m in pat.finditer(window):
                last = m
            if last and last.end() >= limit * 0.25:
                cut = last.end()
                break
        step = cut or limit
        chunks.append(text[i:i + step])
        i += step
    return chunks


def split_for_engine(text, max_chars, max_bytes):
    limit = max_chars
    while True:
        pieces = split_text(text, limit)
        if not max_bytes or all(len(p.encode()) <= max_bytes for p in pieces) or limit <= 20:
            return pieces
        limit = int(limit * 0.7)


def _split_ws(text):
    core = text.strip()
    if not core:
        return text, "", ""
    start = text.index(core[0])
    return text[:start], core, text[start + len(core):]


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, min_interval, max_concurrent, rest_every=0, rest_seconds=0.0):
        self.min_interval = min_interval
        self.max_concurrent = max_concurrent
        self.rest_every = rest_every
        self.rest_seconds = rest_seconds
        self._lock = threading.Lock()
        self._last_start = 0.0
        self._count = 0
        self._in_flight = 0
        self._sem = threading.Semaphore(max_concurrent)

    def can_start_now(self, max_wait=2.0):
        with self._lock:
            if self._in_flight >= self.max_concurrent:
                return False
            return self._last_start + self.min_interval - time.time() <= max_wait

    def next_start_time(self):
        with self._lock:
            return max(self._last_start + self.min_interval, time.time())

    def wait_turn(self):
        with self._lock:
            now = time.time()
            start = max(self._last_start + self.min_interval, now)
            self._last_start = start
            self._count += 1
            if self.rest_every and self._count % self.rest_every == 0:
                self._last_start += self.rest_seconds
        wait = start - time.time()
        if wait > 0:
            time.sleep(wait)
        self._sem.acquire()
        with self._lock:
            self._in_flight += 1

    def release(self):
        self._sem.release()
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------
class TranslationError(Exception):
    pass


class FatalEngineError(Exception):
    pass


class RateLimitError(Exception):
    pass


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class Engine:
    name = "base"
    max_chars = 3000
    max_bytes = None

    def __init__(self, cfg):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})

    def _check(self, r):
        if r.status_code in (403, 429):
            raise RateLimitError(f"HTTP {r.status_code}")
        r.raise_for_status()


class GoogleEngine(Engine):
    name = "google"
    max_chars = 4500

    def translate(self, t, src, tgt):
        r = self.s.post("https://translate.googleapis.com/translate_a/single",
                        params={"client": "gtx", "sl": src or "auto", "tl": tgt, "dt": "t"},
                        data={"q": t}, timeout=self.cfg.request_timeout)
        self._check(r)
        return "".join(s[0] for s in r.json()[0] if s and s[0])


class BingEngine(Engine):
    name = "bing"
    max_chars = 4500
    CODE_MAP = {"zh-CN": "zh-Hans", "zh-TW": "zh-Hant", "tl": "fil", "no": "nb"}

    def __init__(self, cfg):
        super().__init__(cfg)
        self._tok = None
        self._exp = 0
        self._lock = threading.Lock()

    def _token(self):
        with self._lock:
            if self._tok and time.time() < self._exp:
                return self._tok
            r = self.s.get("https://edge.microsoft.com/translate/auth",
                           timeout=self.cfg.request_timeout)
            self._check(r)
            tok = r.text.strip()
            if not tok or len(tok) < 40:
                raise RuntimeError("no Bing token")
            self._tok, self._exp = tok, time.time() + 8 * 60
            return tok

    def translate(self, t, src, tgt):
        tok = self._token()
        params = {"api-version": "3.0", "to": self.CODE_MAP.get(tgt, tgt)}
        if src and src != "auto":
            params["from"] = self.CODE_MAP.get(src, src)
        r = self.s.post("https://api.cognitive.microsofttranslator.com/translate",
                        params=params,
                        headers={"Authorization": "Bearer " + tok,
                                 "Content-Type": "application/json"},
                        data=json.dumps([{"Text": t}]),
                        timeout=self.cfg.request_timeout)
        if r.status_code == 401:
            self._tok = None
        self._check(r)
        return r.json()[0]["translations"][0]["text"]


class MyMemoryEngine(Engine):
    name = "mymemory"
    max_chars = 400
    max_bytes = 480

    def translate(self, t, src, tgt):
        p = {"q": t, "langpair": f"{src or 'en'}|{tgt}"}
        if self.cfg.mymemory_email:
            p["de"] = self.cfg.mymemory_email
        r = self.s.get("https://api.mymemory.translated.net/get", params=p,
                       timeout=self.cfg.request_timeout)
        self._check(r)
        j = r.json()
        out = (j.get("responseData") or {}).get("translatedText", "") or ""
        st_ = str(j.get("responseStatus", ""))
        if "MYMEMORY WARNING" in out.upper() or st_ == "429":
            raise FatalEngineError("quota")
        if st_ != "200":
            raise RuntimeError(f"MyMemory {st_}")
        return _html.unescape(out)


class LingvaEngine(Engine):
    name = "lingva"
    max_chars = 900
    INSTANCES = ["https://lingva.ml", "https://lingva.thedaviddelta.com",
                 "https://translate.plausibility.cloud"]

    def translate(self, t, src, tgt):
        last = None
        insts = self.INSTANCES[:]
        random.shuffle(insts)
        for b in insts:
            try:
                r = self.s.get(f"{b}/api/v1/{src or 'auto'}/{tgt}/{urllib.parse.quote(t, safe='')}",
                               timeout=self.cfg.request_timeout)
                self._check(r)
                out = r.json().get("translation")
                if out:
                    return out
            except Exception as e:
                last = e
        raise last or RuntimeError("all Lingva failed")


class LibreTranslateEngine(Engine):
    name = "libretranslate"
    max_chars = 2000
    INSTANCES = ["https://translate.cutie.dating", "https://translate.fedilab.app",
                 "https://translate.disroot.org", "https://lt.vern.cc"]
    CODE_MAP = {"zh-CN": "zh", "zh-TW": "zh", "no": "nb", "tl": "tl"}

    def translate(self, t, src, tgt):
        last = None
        insts = self.INSTANCES[:]
        random.shuffle(insts)
        s = self.CODE_MAP.get(src, src) or "auto"
        g = self.CODE_MAP.get(tgt, tgt)
        for b in insts:
            try:
                r = self.s.post(f"{b}/translate",
                                json={"q": t, "source": s, "target": g, "format": "text"},
                                timeout=self.cfg.request_timeout)
                self._check(r)
                out = r.json().get("translatedText", "")
                if out:
                    return out
            except Exception as e:
                last = e
        raise last or RuntimeError("all LibreTranslate failed")


class TranslatorsEngine(Engine):
    """Wraps `translators` package — all platforms used here are keyless."""
    max_chars = 1500
    max_bytes = 4000

    def __init__(self, cfg, platform):
        super().__init__(cfg)
        self.platform = platform
        import translators as ts
        self.ts = ts

    @property
    def name(self):
        return f"ts_{self.platform}"

    def translate(self, t, src, tgt):
        try:
            out = self.ts.translate_text(t, translator=self.platform,
                                         from_language=src or "auto",
                                         to_language=tgt, timeout=self.cfg.request_timeout)
        except TypeError:
            out = self.ts.translate_text(t, translator=self.platform,
                                         from_language=src or "auto",
                                         to_language=tgt)
        if not out:
            raise RuntimeError(f"{self.platform} empty")
        return out


class DuckDuckGoEngine(Engine):
    name = "duckduckgo"
    max_chars = 2000
    max_bytes = 6000

    def translate(self, t, src, tgt):
        r = self.s.get("https://duckduckgo.com/",
                       params={"q": "translate", "ia": "web"},
                       timeout=self.cfg.request_timeout)
        self._check(r)
        m = re.search(r'vqd="([^"]+)"', r.text) or re.search(r"vqd=([\d-]+)", r.text)
        if not m:
            raise RuntimeError("DDG vqd not found")
        vqd = m.group(1)
        r2 = self.s.get("https://duckduckgo.com/translation.js",
                        params={"vqd": vqd, "from": src or "auto",
                                "to": tgt, "q": t},
                        timeout=self.cfg.request_timeout)
        self._check(r2)
        try:
            data = r2.json()
        except Exception:
            raise RuntimeError(f"DDG non-JSON: {r2.text[:120]}")
        out = data.get("translated") or data.get("translation") or data.get("result")
        if not out:
            raise RuntimeError(f"DDG unexpected: {data}")
        return out


class ApertiumEngine(Engine):
    name = "apertium"
    max_chars = 3000
    max_bytes = 8000

    def translate(self, t, src, tgt):
        s = src or "en"
        r = self.s.get("https://apertium.org/apy/translate",
                       params={"q": t, "langpair": f"{s}|{tgt}"},
                       timeout=self.cfg.request_timeout)
        self._check(r)
        data = r.json()
        if data.get("responseStatus") != 200:
            raise RuntimeError(f"Apertium status {data.get('responseStatus')}")
        return data["responseData"]["translatedText"]


class ArgosEngine(Engine):
    name = "argos"
    max_chars = 5000
    max_bytes = None

    def __init__(self, cfg):
        super().__init__(cfg)
        import argostranslate.translate
        self._at = argostranslate.translate

    def _ensure_package(self, from_code, to_code):
        import argostranslate.package
        try:
            for p in argostranslate.package.get_installed_packages():
                if p.from_code == from_code and p.to_code == to_code:
                    return
            argostranslate.package.update_package_index()
            for p in argostranslate.package.get_available_packages():
                if p.from_code == from_code and p.to_code == to_code:
                    argostranslate.package.install_from_path(p.download())
                    return
        except Exception as e:
            raise RuntimeError(f"Argos install failed: {e}")
        raise RuntimeError(f"Argos no package {from_code}->{to_code}")

    def translate(self, t, src, tgt):
        s = src if src and src != "auto" else "en"
        self._ensure_package(s, tgt)
        return self._at.translate(t, s, tgt)


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------
class Translator:
    def __init__(self, cfg=None):
        self.cfg = cfg or Config()
        engines = []
        have_ts = _have("translators")
        for n in self.cfg.engine_order:
            try:
                if n.startswith("ts_"):
                    if not have_ts:
                        continue
                    engines.append(TranslatorsEngine(self.cfg, n[3:]))
                elif n == "google":
                    engines.append(GoogleEngine(self.cfg))
                elif n == "bing":
                    engines.append(BingEngine(self.cfg))
                elif n == "mymemory":
                    engines.append(MyMemoryEngine(self.cfg))
                elif n == "libretranslate":
                    engines.append(LibreTranslateEngine(self.cfg))
                elif n == "lingva":
                    engines.append(LingvaEngine(self.cfg))
                elif n == "duckduckgo":
                    engines.append(DuckDuckGoEngine(self.cfg))
                elif n == "apertium":
                    engines.append(ApertiumEngine(self.cfg))
                elif n == "argos":
                    engines.append(ArgosEngine(self.cfg))
                else:
                    log(f"[skip {n}] unknown engine name")
            except Exception as e:
                log(f"[skip {n}] {e}")
        if not engines:
            raise RuntimeError("No engines available")
        self.engines = engines
        self.lock = threading.Lock()
        self.state = {e.name: {"fails": 0, "until": 0.0, "cool": self.cfg.cooldown_seconds}
                      for e in engines}
        self.stats = defaultdict(lambda: {"ok": 0, "fail": 0})
        self.rate = {}
        for e in engines:
            lim = dict(self.cfg.default_engine_limits)
            lim.update(self.cfg.engine_limits.get(e.name, {}))
            self.rate[e.name] = RateLimiter(float(lim["min_interval"]),
                                            int(lim["max_concurrent"]),
                                            int(lim.get("rest_every", 0)),
                                            float(lim.get("rest_seconds", 0)))
        self._rr_lock = threading.Lock()
        self._rr = 0
        self._dirty = 0
        self.cache = {}
        self._load()

    @staticmethod
    def _key(t, src, tgt):
        return hashlib.sha1(f"{src}:{tgt}:{t}".encode()).hexdigest()

    def _load(self):
        try:
            if os.path.isfile(self.cfg.cache_path):
                with open(self.cfg.cache_path, encoding="utf-8") as f:
                    self.cache = json.load(f)
        except Exception as e:
            log(f"cache load: {e}")

    def save(self):
        try:
            with self.lock:
                snap = dict(self.cache)
                self._dirty = 0
            tmp = self.cfg.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, self.cfg.cache_path)
        except Exception as e:
            log(f"cache save: {e}")

    def _available(self, e):
        with self.lock:
            return time.time() >= self.state[e.name]["until"]

    def _ok(self, e):
        with self.lock:
            st_ = self.state[e.name]
            st_["fails"] = 0
            st_["cool"] = self.cfg.cooldown_seconds
            self.stats[e.name]["ok"] += 1

    def _fail(self, e, fatal=False):
        with self.lock:
            st_ = self.state[e.name]
            st_["fails"] += 1
            self.stats[e.name]["fail"] += 1
            if fatal or st_["fails"] >= self.cfg.failure_threshold:
                cool = 600.0 if fatal else st_["cool"]
                st_["until"] = time.time() + cool
                st_["cool"] = min(st_["cool"] * 2, 600.0)
                st_["fails"] = 0

    def _validate(self, src, out, tgt):
        if not out or not out.strip():
            raise ValueError("empty")
        if out.strip() == src.strip() and len(src) > 25:
            raise ValueError("echoed")
        rx = SCRIPT_RE.get(tgt)
        if rx and len(src) > 20 and not rx.search(out):
            raise ValueError(f"not {tgt}")
        if len(src) > 60 and len(out) < 0.15 * len(src):
            raise ValueError("too short")

    def _pick(self, exclude, preferred_engine=None):
        if preferred_engine:
            for e in self.engines:
                if e.name == preferred_engine and e.name not in exclude and self._available(e):
                    if self.rate[e.name].can_start_now(0.5):
                        return e
                    break
        with self._rr_lock:
            n = len(self.engines)
            for _ in range(n):
                e = self.engines[self._rr % n]
                self._rr += 1
                if e.name in exclude:
                    continue
                if not self._available(e):
                    continue
                if not self.rate[e.name].can_start_now(2.0):
                    continue
                return e
        return None

    def _wait(self, exclude, deadline):
        if time.time() >= deadline:
            return
        soonest = float("inf")
        for e in self.engines:
            if e.name in exclude:
                continue
            if not self._available(e):
                with self.lock:
                    soonest = min(soonest, self.state[e.name]["until"])
            else:
                soonest = min(soonest, self.rate[e.name].next_start_time())
        time.sleep(max(0.1, min(1.0, soonest - time.time()))
                   if soonest < float("inf") else 0.5)

    def _try(self, e, piece, src, tgt):
        last = None
        for a in range(self.cfg.max_retries + 1):
            acq = False
            try:
                self.rate[e.name].wait_turn()
                acq = True
                out = e.translate(piece, src, tgt)
                self._validate(piece, out, tgt)
                self._ok(e)
                return out
            except RateLimitError as ex:
                self._fail(e)
                raise TranslationError(f"{e.name}: {ex}")
            except FatalEngineError as ex:
                self._fail(e, fatal=True)
                raise TranslationError(f"{e.name}: {ex}")
            except Exception as ex:
                last = ex
                if a < self.cfg.max_retries:
                    time.sleep(0.3 * (a + 1) + random.random() * 0.2)
            finally:
                if acq:
                    self.rate[e.name].release()
        self._fail(e)
        raise TranslationError(f"{e.name}: {last}")

    def _translate(self, text, exclude, src, tgt, preferred_engine=None):
        lead, core, trail = _split_ws(text)
        if not core:
            return text
        if all(e.name in exclude for e in self.engines):
            raise TranslationError("all engines failed")

        deadline = time.time() + self.cfg.max_wait_all_down
        engine = None
        while engine is None:
            engine = self._pick(exclude, preferred_engine)
            if engine:
                break
            if time.time() > deadline:
                raise TranslationError("all engines busy / cooling down")
            self._wait(exclude, deadline)

        pieces = split_for_engine(core, engine.max_chars, engine.max_bytes)

        if len(pieces) == 1:
            try:
                t = self._try(engine, core, src, tgt)
            except TranslationError:
                return self._translate(core, exclude | {engine.name}, src, tgt, None)
            return lead + t.strip() + trail

        def do_one(piece):
            pl, pc, pt = _split_ws(piece)
            if not pc:
                return piece
            try:
                t = self._try(engine, pc, src, tgt)
            except TranslationError:
                try:
                    t = self._translate(pc, exclude | {engine.name}, src, tgt, None)
                except Exception:
                    return piece
            return pl + t.strip() + pt

        with ThreadPoolExecutor(max_workers=min(len(pieces), 8)) as pool:
            outs = list(pool.map(do_one, pieces))
        return lead + "".join(outs) + trail

    def translate(self, text, src, tgt, preferred_engine=None):
        if not text or not text.strip():
            return text
        if src == "auto":
            src = auto_detect(text)
        if src == tgt:
            return text
        lead, core, trail = _split_ws(text)
        if not core:
            return text
        k = self._key(core, src, tgt)
        with self.lock:
            hit = self.cache.get(k)
        if hit:
            return lead + hit + trail
        result = self._translate(core, frozenset(), src, tgt, preferred_engine).strip()
        with self.lock:
            self.cache[k] = result
            self._dirty += 1
            flush = self._dirty >= self.cfg.save_every
        if flush:
            self.save()
        return lead + result + trail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _looks_translatable(t):
    if not t or len(t.strip()) < 2:
        return False
    if not re.search(r"[A-Za-z\u0900-\u0DFF\u0600-\u06FF\u4E00-\u9FFF]", t):
        return False
    if re.fullmatch(r"https?://\S+|www\.\S+|\S+@\S+\.\S+", t.strip()):
        return False
    return True


def _translate_many(translator, texts, src, tgt, on_done=None, workers=None,
                    preferred_engine=None):
    out = [None] * len(texts)
    if not texts:
        return out
    if workers is None:
        workers = min(16, max(4, len(texts)))

    def _one(t):
        return translator.translate(t, src, tgt, preferred_engine=preferred_engine)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, t): i for i, t in enumerate(texts)}
        for k, fut in enumerate(as_completed(futs), 1):
            i = futs[fut]
            try:
                out[i] = fut.result()
            except Exception as e:
                log(f"translate: {e}")
            if on_done:
                on_done(k)
    return out


_PARA_SEP = re.compile(r"(\n[ \t]*\n+)")


def _translate_paragraphs(text, translator, src, tgt, progress=None,
                          label="Translating", preferred_engine=None):
    CHUNK = 4000
    parts = _PARA_SEP.split(text)

    chunks = []
    for i, p in enumerate(parts):
        if i % 2 != 0:
            continue
        if not p.strip():
            continue
        if len(p) <= CHUNK:
            chunks.append((i, None, p))
        else:
            for j, sp in enumerate(split_text(p, CHUNK)):
                chunks.append((i, j, sp))

    texts = [c[2] for c in chunks]
    total = len(texts)
    if progress:
        progress.set(0, max(1, total), f"{label} 0/{total}")

    def on_done(k):
        if progress:
            progress.set(k, total, f"{label} {k}/{total}")

    results = _translate_many(translator, texts, src, tgt, on_done,
                              preferred_engine=preferred_engine)

    failed = sum(1 for r in results if r is None)
    new_parts = list(parts)
    grouped = defaultdict(dict)
    for (part_idx, sub_idx, orig), res in zip(chunks, results):
        r = res if res is not None else orig
        if sub_idx is None:
            new_parts[part_idx] = r
        else:
            grouped[part_idx][sub_idx] = r
    for part_idx, subs in grouped.items():
        new_parts[part_idx] = "".join(subs[k] for k in sorted(subs.keys()))

    return "".join(new_parts), failed, total


def _sample_text(path, max_chars=2000):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".txt":
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(max_chars)
        if ext == ".docx":
            if not _have("docx"):
                _pip(["python-docx"])
            from docx import Document
            doc = Document(path)
            parts = []
            for p in doc.paragraphs:
                if p.text.strip():
                    parts.append(p.text)
                if sum(len(x) for x in parts) > max_chars:
                    break
            return " ".join(parts)[:max_chars]
        if ext == ".pdf":
            if not _have("pymupdf"):
                _pip(["pymupdf"])
            pymupdf = _import_pymupdf()
            d = pymupdf.open(path)
            txt = ""
            for i in range(min(3, d.page_count)):
                txt += d[i].get_text("text")
                if len(txt) > max_chars:
                    break
            d.close()
            return txt[:max_chars]
        if ext == ".epub":
            if not _have("bs4"):
                _pip(["beautifulsoup4"])
            import zipfile
            from bs4 import BeautifulSoup
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if n.lower().endswith((".xhtml", ".html", ".htm")):
                        try:
                            soup = BeautifulSoup(z.read(n), "html.parser")
                        except Exception:
                            continue
                        for tag in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li"]):
                            t = tag.get_text(" ", strip=True)
                            if len(t) > 20:
                                return t[:max_chars]
            return ""
    except Exception as e:
        log(f"sample extract: {e}")
    return ""


# ---- TXT ----
def _translate_txt(path, translator, src, tgt, progress=None, preferred_engine=None):
    if progress:
        progress.set(0, 1, "Reading TXT…")
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    out_text, failed, total = _translate_paragraphs(text, translator, src, tgt, progress,
                                                    "TXT paragraph", preferred_engine)
    if total and failed == total:
        raise RuntimeError("all paragraphs failed to translate")
    out = _out_path(path, tgt, ".txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(out_text)
    return out, failed


# ---- DOCX ----
def _translate_docx(path, translator, src, tgt, progress=None, preferred_engine=None):
    if not _have("docx"):
        _pip(["python-docx"])
    from docx import Document
    doc = Document(path)
    targets, seen = [], set()

    def add(p):
        key = id(p._p)
        if key not in seen and p.text.strip():
            seen.add(key)
            targets.append(p)

    for p in doc.paragraphs:
        add(p)
    for tb in doc.tables:
        for row in tb.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    add(p)
    for sec in doc.sections:
        for part in (sec.header, sec.footer):
            try:
                for p in part.paragraphs:
                    add(p)
            except Exception:
                pass
    total = len(targets)

    def on_done(k):
        if progress:
            progress.set(k, max(1, total), f"DOCX paragraph {k}/{total}")

    if progress:
        progress.set(0, max(1, total), f"DOCX paragraph 0/{total}")
    results = _translate_many(translator, [p.text for p in targets], src, tgt, on_done,
                              preferred_engine=preferred_engine)
    failed = 0
    for p, new in zip(targets, results):
        if new is None:
            failed += 1
            continue
        if p.runs:
            p.runs[0].text = new
            for r in p.runs[1:]:
                r.text = ""
        else:
            p.text = new
    if total and failed == total:
        raise RuntimeError("all paragraphs failed to translate")
    if progress:
        progress.set(total, max(1, total), "Saving DOCX…")
    out = _out_path(path, tgt, ".docx")
    doc.save(out)
    return out, failed


# ---- EPUB ----
EPUB_BLOCKS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre",
               "figcaption", "dt", "dd", "td", "th", "caption", "div"]
EPUB_SKIP = ["script", "style", "head", "title", "nav"]


def _clean_tag(tag):
    return re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()


def _set_tag_text(tag, new):
    from bs4 import NavigableString, Tag
    media = ("img", "svg", "image", "video", "audio", "math")
    keep = [c for c in tag.children if isinstance(c, Tag)
            and (c.name in media or c.find(media))]
    anchors = [a for a in tag.find_all("a")
               if a.get("id") and not a.get_text(strip=True)
               and not any(a in k.descendants for k in keep)]
    for el in anchors + keep:
        el.extract()
    tag.clear()
    for el in anchors + keep:
        tag.append(el)
    tag.append(NavigableString(new))


def _translate_epub(path, translator, src, tgt, progress=None, preferred_engine=None):
    if not _have("bs4"):
        _pip(["beautifulsoup4"])
    if not _have("lxml"):
        _pip(["lxml"])
    import zipfile
    from bs4 import BeautifulSoup

    def parse(raw):
        try:
            return BeautifulSoup(raw, "xml"), True
        except Exception:
            return BeautifulSoup(raw, "html.parser"), False

    with zipfile.ZipFile(path) as zin:
        names = zin.namelist()
        container = parse(zin.read("META-INF/container.xml"))[0]
        rf = container.find("rootfile")
        if not rf:
            raise ValueError("Invalid EPUB (no rootfile)")
        opf_path = rf["full-path"]
        opf_dir = posixpath.dirname(opf_path)
        opf_soup = parse(zin.read(opf_path))[0]
        items = {i.get("id"): i for i in opf_soup.find_all("item")}
        docs = []
        for ref in opf_soup.find_all("itemref"):
            it = items.get(ref.get("idref"))
            if not it:
                continue
            href = it.get("href", "").split("#")[0]
            if not href:
                continue
            full = posixpath.normpath(posixpath.join(opf_dir, urllib.parse.unquote(href)))
            if full in names and full not in docs:
                docs.append(full)
        if not docs:
            docs = [n for n in names
                    if n.lower().endswith((".xhtml", ".html", ".htm")) and "/nav" not in n]
        new_files = {}
        total = len(docs)
        failed_total = 0
        for i, p in enumerate(docs, 1):
            label = f"EPUB chapter {i}/{total}"
            if progress:
                progress.set(i - 1, max(1, total), label)
            soup, is_xml = parse(zin.read(p))
            nav_links = [a for a in soup.find_all("a")
                         if a.find_parent("nav") and _looks_translatable(_clean_tag(a))]
            blocks = [t for t in soup.find_all(EPUB_BLOCKS)
                      if not t.find(EPUB_BLOCKS) and not t.find_parent(EPUB_SKIP)
                      and _looks_translatable(_clean_tag(t))]
            texts = [_clean_tag(x) for x in nav_links] + [_clean_tag(x) for x in blocks]
            n = len(texts)

            def on_done(k, i=i, n=n, label=label):
                if progress:
                    progress.set(i - 1 + k / max(1, n), max(1, total),
                                 f"{label} · {k}/{n} blocks")

            results = _translate_many(translator, texts, src, tgt, on_done,
                                      preferred_engine=preferred_engine)
            for a, new in zip(nav_links, results[:len(nav_links)]):
                if new is None:
                    failed_total += 1
                else:
                    a.string = new
            for tag, new in zip(blocks, results[len(nav_links):]):
                if new is None:
                    failed_total += 1
                else:
                    _set_tag_text(tag, new)
            root = soup.find("html")
            if root is not None:
                root["lang"] = tgt
                root["xml:lang"] = tgt
            out_html = str(soup)
            if not is_xml:
                out_html = re.sub(r"\bviewbox=", "viewBox=", out_html)
                out_html = re.sub(r"\bpreserveaspectratio=", "preserveAspectRatio=", out_html)
            new_files[p] = out_html.encode("utf-8")
            if progress:
                progress.set(i, max(1, total), label)
        if progress:
            progress.set(total, max(1, total), "Writing EPUB…")
        new_opf = zin.read(opf_path).decode("utf-8", errors="ignore")
        new_opf = re.sub(r"(<dc:language[^>]*>)[^<]*(</dc:language>)",
                         f"\\g<1>{tgt}\\g<2>", new_opf)
        tm = re.search(r"<dc:title[^>]*>([^<]*)</dc:title>", new_opf)
        if tm and _looks_translatable(tm.group(1)):
            try:
                tr = translator.translate(_html.unescape(tm.group(1)), src, tgt,
                                          preferred_engine=preferred_engine)
                new_opf = re.sub(r"(<dc:title[^>]*>)[^<]*(</dc:title>)",
                                 lambda m: m.group(1) + _html.escape(tr) + m.group(2),
                                 new_opf, count=1)
            except Exception:
                pass
        new_files[opf_path] = new_opf.encode("utf-8")
        out = _out_path(path, tgt, ".epub")
        with zipfile.ZipFile(out, "w") as zout:
            infos = sorted(zin.infolist(),
                           key=lambda x: 0 if x.filename == "mimetype" else 1)
            for info in infos:
                data = new_files.get(info.filename)
                if data is None:
                    data = zin.read(info.filename)
                ct = (zipfile.ZIP_STORED if info.filename == "mimetype"
                      else zipfile.ZIP_DEFLATED)
                zout.writestr(info.filename, data, compress_type=ct)
    return out, failed_total


# ---- PDF ----
FONT_DIR = os.path.join(tempfile.gettempdir(), "multilang_fonts")
NOTO_SCRIPTS = {"hi": "Devanagari", "mr": "Devanagari", "ta": "Tamil", "te": "Telugu",
                "bn": "Bengali", "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam",
                "pa": "Gurmukhi", "or": "Oriya", "ur": "Arabic", "ar": "Arabic",
                "fa": "Arabic", "th": "Thai", "ru": "Cyrillic", "uk": "Cyrillic",
                "el": "Greek", "he": "Hebrew"}


def _pdf_font(tgt):
    script = NOTO_SCRIPTS.get(tgt)
    if not script:
        return None, "sans-serif"
    dest = os.path.join(FONT_DIR, script)
    os.makedirs(dest, exist_ok=True)
    base = (f"https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/"
            f"fonts/NotoSans{script}/hinted/ttf")
    for w in ("Regular", "Bold"):
        fname = f"NotoSans{script}-{w}.ttf"
        fpath = os.path.join(dest, fname)
        if os.path.isfile(fpath) and os.path.getsize(fpath) > 20000:
            continue
        try:
            r = requests.get(f"{base}/{fname}", timeout=60)
            if r.status_code == 200:
                with open(fpath, "wb") as f:
                    f.write(r.content)
        except Exception as e:
            log(f"font {fname}: {e}")
    reg = os.path.join(dest, f"NotoSans{script}-Regular.ttf")
    bold = os.path.join(dest, f"NotoSans{script}-Bold.ttf")
    if not os.path.isfile(reg) or os.path.getsize(reg) < 20000:
        return None, "sans-serif"
    if not os.path.isfile(bold) or os.path.getsize(bold) < 20000:
        import shutil
        shutil.copy(reg, bold)
    return dest, f"NotoSans{script}"


def _translate_pdf(path, translator, src, tgt, progress=None, preferred_engine=None):
    if not _have("pymupdf"):
        _pip(["pymupdf"])
    pymupdf = _import_pymupdf()
    if progress:
        progress.set(0, 1, "Preparing PDF fonts…")
    font_dir, family = _pdf_font(tgt)
    if font_dir:
        archive = pymupdf.Archive(font_dir)
        script = family.replace("NotoSans", "")
        css = (f'@font-face {{font-family:"{family}"; src:url("NotoSans{script}-Regular.ttf");}}'
               f'@font-face {{font-family:"{family}"; font-weight:bold; '
               f'src:url("NotoSans{script}-Bold.ttf");}}')
    else:
        archive = None
        css = ""
    doc = pymupdf.open(path)
    if doc.needs_pass:
        raise ValueError("PDF is password protected")
    total = doc.page_count
    failed_total = 0
    for pno in range(total):
        label = f"PDF page {pno + 1}/{total}"
        if progress:
            progress.set(pno, total, label)
        page = doc[pno]
        if page.rotation:
            try:
                page.remove_rotation()
            except Exception:
                continue
        blocks = page.get_text("dict", sort=True).get("blocks", [])
        jobs = []
        for b in blocks:
            if b.get("type") != 0:
                continue
            lines, weights = [], {}
            first_y = last_y = None
            bad = False
            for ln in b.get("lines", []):
                if abs(ln.get("dir", (1, 0))[0]) < 0.95:
                    bad = True
                    break
                txt = "".join(s["text"] for s in ln["spans"])
                if not txt.strip():
                    continue
                lines.append(txt)
                y = ln["spans"][0]["origin"][1]
                first_y = y if first_y is None else first_y
                last_y = y
                for s in ln["spans"]:
                    if not s["text"].strip():
                        continue
                    is_bold = bool(s.get("flags", 0) & 16) or "bold" in s["font"].lower()
                    k = (round(s["size"], 1), is_bold, s["color"])
                    weights[k] = weights.get(k, 0) + len(s["text"].strip())
            if bad or not lines or not weights:
                continue
            full = " ".join(lines).strip()
            if not _looks_translatable(full):
                continue
            key = max(weights.items(), key=lambda kv: kv[1])[0]
            size, is_bold, color = key
            pitch = ((last_y - first_y) / max(1, len(lines) - 1)
                     if last_y and first_y and last_y > first_y
                     else size * 1.3)
            lh = max(1.2, min(1.7, pitch / size if size else 1.3))
            jobs.append([pymupdf.Rect(b["bbox"]), full, size, is_bold, color, lh, None])
        if not jobs:
            continue

        def on_done(k, pno=pno, n=len(jobs), label=label):
            if progress:
                progress.set(pno + k / n, total, f"{label} · {k}/{n} blocks")

        results = _translate_many(translator, [j[1] for j in jobs], src, tgt, on_done,
                                  preferred_engine=preferred_engine)
        for j, r in zip(jobs, results):
            j[6] = r
            if r is None:
                failed_total += 1
        for j in jobs:
            page.add_redact_annot(j[0], fill=False)
        try:
            page.apply_redactions(images=getattr(pymupdf, "PDF_REDACT_IMAGE_NONE", 0))
        except TypeError:
            page.apply_redactions()
        for j in jobs:
            rect, orig, size, is_bold, color, lh, trans = j
            if not trans:
                continue
            esc = _html.escape(trans)
            h = (f'<div style="font-family:{family}; font-size:{size * 0.98:.1f}pt; '
                 f'line-height:{lh:.2f}; color:#{color & 0xFFFFFF:06x}; '
                 f'font-weight:{"bold" if is_bold else "normal"};">{esc}</div>')
            expand = pymupdf.Rect(rect.x0, rect.y0, rect.x1,
                                  rect.y1 + max(rect.height * 0.6, size * 1.5))
            try:
                page.insert_htmlbox(expand, h, css=css, archive=archive, scale_low=0.4)
            except Exception as e:
                log(f"insert p{pno + 1}: {e}")
    if progress:
        progress.set(total, max(1, total), "Saving PDF…")
    out = _out_path(path, tgt, ".pdf")
    doc.save(out, garbage=3, deflate=True)
    doc.close()
    return out, failed_total


# ---------------------------------------------------------------------------
# Core handlers (framework-agnostic)
# ---------------------------------------------------------------------------
def _text_handler(text, src_name, tgt_name, engine_name, translator, progress=None):
    if not text or not text.strip():
        return "", "⚠️ Please enter text."
    src = NAME_TO_CODE.get(src_name, "en")
    tgt = NAME_TO_CODE.get(tgt_name, "en")
    preferred = None if engine_name == "Auto (failover)" else engine_name
    note = ""
    if src == "auto":
        det = auto_detect(text)
        note = f"Detected **{LANGUAGE_NAMES.get(det, det)}** · "
        src = det
    if src == tgt:
        return text, f"{note}Source and target are the same."
    t0 = time.time()
    out, failed, total = _translate_paragraphs(text, translator, src, tgt, progress,
                                               "Paragraph", preferred_engine=preferred)
    if total and failed == total:
        return "", "❌ All engines failed or are rate-limited — try again in a minute."
    translator.save()
    warn = f" · ⚠️ {failed}/{total} paragraphs failed" if failed else ""
    engine_label = preferred if preferred else "Auto‑failover"
    return out, (f"{note}✅ {LANGUAGE_NAMES[src]} → {LANGUAGE_NAMES[tgt]} · "
                 f"{len(text)}→{len(out)} chars · {time.time() - t0:.2f}s · "
                 f"engine: {engine_label}{warn}")


def _file_handler(path, src_name, tgt_name, engine_name, translator, progress=None):
    if not path or not os.path.isfile(path):
        return None, "⚠️ Please upload a file."
    src = NAME_TO_CODE.get(src_name, "en")
    tgt = NAME_TO_CODE.get(tgt_name, "en")
    preferred = None if engine_name == "Auto (failover)" else engine_name
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".txt", ".docx", ".pdf", ".epub"):
        return None, f"❌ Unsupported file type: {ext}. Use TXT, DOCX, PDF, or EPUB."
    if progress:
        progress.set(0, 1, "Detecting source language…")
    note = ""
    if src == "auto":
        sample = _sample_text(path)
        src = auto_detect(sample) if sample else "en"
        note = f"Detected **{LANGUAGE_NAMES.get(src, src)}** · "
    if src == tgt:
        return path, f"{note}Source and target are the same — no change."
    t0 = time.time()
    try:
        fn = {".txt": _translate_txt, ".docx": _translate_docx,
              ".pdf": _translate_pdf, ".epub": _translate_epub}[ext]
        out, failed = fn(path, translator, src, tgt, progress, preferred_engine=preferred)
    except Exception as e:
        traceback.print_exc()
        return None, f"❌ Failed: {e}"
    translator.save()
    kb = os.path.getsize(out) / 1024
    warn = f" · ⚠️ {failed} block(s) left untranslated" if failed else ""
    engine_label = preferred if preferred else "Auto‑failover"
    return out, (f"{note}✅ {LANGUAGE_NAMES[src]} → {LANGUAGE_NAMES[tgt]} · "
                 f"{os.path.basename(out)} · {kb:.0f} KB · {time.time() - t0:.1f}s · "
                 f"engine: {engine_label}{warn}")


# ---------------------------------------------------------------------------
# Streamlit glue
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_translator():
    return Translator()


def _run_with_progress(job, label="Working…"):
    """Run `job(progress)` in a worker thread, polling a live Streamlit
    progress bar in the main thread. Returns whatever `job` returns."""
    prog = Progress()
    prog.set(0, 1, "Starting…")
    box = {}

    with st.status(label, expanded=True) as sb:
        bar = st.progress(0, text="Starting…")
        msg_holder = st.empty()

        def worker():
            try:
                box["result"] = job(prog)
            except Exception as e:
                traceback.print_exc()
                box["error"] = str(e)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        while t.is_alive():
            bar.progress(prog.pct(), text=prog.msg)
            time.sleep(0.15)
        t.join()

        if "error" in box:
            bar.progress(100, text="Error")
            sb.update(label="Failed", state="error", expanded=True)
            raise RuntimeError(box["error"])

        bar.progress(100, text="Complete")
        sb.update(label="Complete", state="complete", expanded=False)

    return box["result"]


def _engine_status_markdown(translator):
    lines = ["| Engine | Status |", "|---|---|"]
    now = time.time()
    for e in translator.engines:
        with translator.lock:
            st_ = translator.state[e.name]
            cool = st_["until"]
            fails = st_["fails"]
        if cool > now:
            state = f"⏳ cooling ({int(cool - now)}s)"
        elif fails:
            state = f"⚠️ {fails} fail(s)"
        else:
            state = "✅ ready"
        lines.append(f"| `{e.name}` | {state} |")
    lines.append("")
    lines.append(f"**Cache:** {len(translator.cache)} entries")
    return "\n".join(lines)


def _mime_for(path):
    ext = os.path.splitext(path)[1].lower()
    return {
        ".txt": "text/plain",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf",
        ".epub": "application/epub+zip",
    }.get(ext, "application/octet-stream")


def _swap_langs():
    s = st.session_state.src_lang
    t = st.session_state.tgt_lang
    if s != "Auto-detect":
        st.session_state.src_lang = t
        st.session_state.tgt_lang = s


# ---------------------------------------------------------------------------
# Streamlit app
# ---------------------------------------------------------------------------
def main():
    st.title("🌐 Multilingual Translator")
    st.caption("Text or file (TXT · DOCX · PDF · EPUB) — 35+ languages, auto-detect. "
               "Multi-engine failover · no API keys required.")

    translator = get_translator()

    # --- Sidebar: settings ---
    with st.sidebar:
        st.header("Settings")

        if "src_lang" not in st.session_state:
            st.session_state.src_lang = "Auto-detect"
        if "tgt_lang" not in st.session_state:
            st.session_state.tgt_lang = "Hindi"

        c1, c2 = st.columns([3, 1])
        with c1:
            src = st.selectbox("From", SOURCE_CHOICES, key="src_lang")
        with c2:
            st.write("")
            st.write("")
            st.button("⇄", on_click=_swap_langs, help="Swap languages")
        tgt = st.selectbox("To", TARGET_CHOICES, key="tgt_lang")

        engine_choices = ["Auto (failover)"] + [e.name for e in translator.engines]
        engine = st.selectbox(
            "Translation engine", engine_choices, index=0,
            help="Pick a specific engine or let the system auto‑failover.")

        with st.expander("🔧 Engine status"):
            st.markdown(_engine_status_markdown(translator))
            if st.button("🔄 Refresh"):
                st.rerun()

        st.caption(f"Active engines: {len(translator.engines)}")
        if _BOOT_FAILED:
            with st.expander("⚠️ Some optional packages could not be installed"):
                for p, err in _BOOT_FAILED.items():
                    st.caption(f"**{p}** — features needing it are disabled.")
                    st.code(err[-300:])

    # --- Tabs ---
    tab_text, tab_file = st.tabs(["📝 Text", "📎 File"])

    # --- Text tab ---
    with tab_text:
        text = st.text_area("Text to translate", height=220,
                            placeholder="Type or paste text…", key="text_input")

        if st.button("Translate", type="primary", key="btn_text"):
            if not text or not text.strip():
                st.warning("Please enter text.")
            else:
                try:
                    result, status = _run_with_progress(
                        lambda prog: _text_handler(text, src, tgt, engine,
                                                   translator, prog),
                        label="Translating text…",
                    )
                    if result:
                        st.session_state.text_result = result
                        st.session_state.text_status = status
                    else:
                        st.session_state.text_status = status
                except Exception as e:
                    st.session_state.text_status = f"❌ Failed: {e}"

        if st.session_state.get("text_status"):
            s = st.session_state.text_status
            if s.startswith(("❌", "⚠️")):
                st.error(s)
            else:
                st.success(s)

        if st.session_state.get("text_result"):
            st.text_area("Translation", value=st.session_state.text_result,
                         height=260, disabled=True, key="text_result_display")

    # --- File tab ---
    with tab_file:
        st.markdown("Upload a **.txt**, **.docx**, **.pdf**, or **.epub** file. "
                    "Output is in the same format.")
        uploaded = st.file_uploader("Upload file",
                                    type=["txt", "docx", "pdf", "epub"],
                                    accept_multiple_files=False)

        if uploaded is not None and st.button("Translate file", type="primary",
                                              key="btn_file"):
            tmpdir = tempfile.mkdtemp(prefix="mlt_upload_")
            safe_name = os.path.basename(uploaded.name)
            path = os.path.join(tmpdir, safe_name)
            with open(path, "wb") as f:
                f.write(uploaded.getvalue())
            try:
                out_path, status = _run_with_progress(
                    lambda prog: _file_handler(path, src, tgt, engine,
                                               translator, prog),
                    label="Translating file…",
                )
                st.session_state.file_out = out_path
                st.session_state.file_status = status
            except Exception as e:
                st.session_state.file_out = None
                st.session_state.file_status = f"❌ Failed: {e}"

        if st.session_state.get("file_status"):
            s = st.session_state.file_status
            if s.startswith(("❌", "⚠️")):
                st.error(s)
            else:
                st.success(s)

        out_path = st.session_state.get("file_out")
        if out_path and os.path.exists(out_path):
            with open(out_path, "rb") as f:
                data = f.read()
            st.download_button(
                f"⬇️ Download {os.path.basename(out_path)}",
                data=data,
                file_name=os.path.basename(out_path),
                mime=_mime_for(out_path),
            )


main()
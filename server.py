import time
import json
import logging
import os
import re
import asyncio
import threading
import zipfile
import io
import shutil
from datetime import datetime
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager

# FastAPI
from fastapi import FastAPI, HTTPException, Query, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# SQLAlchemy
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, desc
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
ANALYSIS_LOCKS = {}
ANALYSIS_STATUS = {}

EMAIL = os.environ.get("EMAIL", "alshhabi0000@gmail.com")
PASSWORD = os.environ.get("PASSWORD", "Asdfgh123@")
NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_MODEL = "minimaxai/minimax-m1"
NVIDIA_API_TOKEN = os.environ.get("NVIDIA_API_TOKEN", "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")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
ARABIC_TTS_VOICE = os.environ.get("ARABIC_TTS_VOICE", "ar-SA-ZariyahNeural")
IS_HEADLESS = os.environ.get("HEADLESS", "0") == "1"

# Telegram
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8652668769:AAGUMELS4sWpcKZ5WSTxqFW8BUhiz-VwrgE")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@osamaalshahape")

# Database
DATA_DIR = "/data" if os.path.exists("/data") else "."
DB_PATH = os.path.join(DATA_DIR, "videos.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    url = Column(String, unique=True, index=True)
    channel = Column(String, default="")
    video_id = Column(String, nullable=True)
    scraped_at = Column(DateTime, default=datetime.now)
    srt_transcript = Column(Text, nullable=True)
    full_transcript = Column(Text, nullable=True)
    highlights = Column(Text, nullable=True)
    first_principles = Column(Text, nullable=True)

Base.metadata.create_all(bind=engine)

# Auto-migrate: add missing columns to existing DB
def migrate_db():
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if 'videos' in insp.get_table_names():
        existing = [c['name'] for c in insp.get_columns('videos')]
        migrations = {
            'video_id': 'VARCHAR', 'srt_transcript': 'TEXT',
            'full_transcript': 'TEXT', 'first_principles': 'TEXT'
        }
        with engine.connect() as conn:
            for col, dtype in migrations.items():
                if col not in existing:
                    logger.info(f"Adding column {col} to videos table...")
                    conn.execute(text(f"ALTER TABLE videos ADD COLUMN {col} {dtype}"))
                    conn.commit()
migrate_db()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def extract_video_id(url):
    m = re.search(r'(?:v=|youtu\.be/)([^&#?]{11})', url or '')
    return m.group(1) if m else None


# ============================================
# SRT & Timing Utilities
# ============================================

def parse_srt_time_to_seconds(ts: str) -> Optional[float]:
    ts = (ts or "").strip().replace("\ufeff", "").replace(".", ",")
    if not ts: return None
    if "," not in ts: ts += ",000"
    try:
        time_part, ms_part = ts.rsplit(",", 1)
        h, m, s = time_part.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms_part) / 1000.0
    except: return None

def parse_srt_cues(srt_content: str) -> List[Dict[str, Any]]:
    cues = []
    blocks = re.split(r"\n\s*\n", (srt_content or "").strip())
    for block in blocks:
        lines = [ln.rstrip() for ln in block.splitlines()]
        if not any(l.strip() for l in lines): continue
        idx = 0
        if idx < len(lines) and re.match(r"^\d+$", lines[idx].strip()): idx += 1
        if idx >= len(lines): continue
        m = re.match(r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})", lines[idx].strip())
        if not m: continue
        start_str = m.group(1).replace(".", ",")
        end_str = m.group(2).replace(".", ",")
        text = "\n".join(lines[idx + 1:]).strip()
        ss = parse_srt_time_to_seconds(start_str)
        es = parse_srt_time_to_seconds(end_str)
        if ss is None or es is None: continue
        cues.append({"start_str": start_str, "end_str": end_str, "start_sec": ss, "end_sec": es, "text": text})
    return cues

def split_cues_into_time_windows(cues, window_sec=300, max_chars=12000):
    if not cues: return []
    chunks, chunk = [], [cues[0]]
    anchor = cues[0]["start_sec"]
    chunk_chars = len(cues[0].get("text", ""))
    for c in cues[1:]:
        cue_chars = len(c.get("text", "")) + 40
        if c["start_sec"] - anchor >= window_sec or chunk_chars + cue_chars >= max_chars:
            chunks.append(chunk)
            chunk = []
            anchor = c["start_sec"]
            chunk_chars = 0
        chunk.append(c)
        chunk_chars += cue_chars
    if chunk: chunks.append(chunk)
    return chunks

def cues_to_srt_string(cues):
    parts = []
    for i, c in enumerate(cues, 1):
        parts.extend([str(i), f"{c['start_str']} --> {c['end_str']}", c["text"], ""])
    return "\n".join(parts)

def resolve_highlight_seconds(h, part_cues, part_index, duration_cap):
    st = h.get("start_time") or h.get("timecode")
    if isinstance(st, str) and st.strip():
        sec_f = parse_srt_time_to_seconds(st.strip())
        if sec_f is not None:
            if part_cues:
                nearest = min(part_cues, key=lambda c: abs(c["start_sec"] - sec_f))
                s = int(nearest["start_sec"]) if abs(nearest["start_sec"] - sec_f) <= 3.0 else int(sec_f)
            else:
                s = int(sec_f)
            return max(0, min(s, duration_cap))
    try: sec = int(float(h.get("seconds", 0)))
    except: sec = 0
    return max(0, min(sec, duration_cap))

def dedupe_highlights_by_time(highlights, gap_sec=4.0):
    if not highlights: return []
    highlights.sort(key=lambda x: x.get("seconds", 0))
    out, last = [], -1e9
    for h in highlights:
        s = float(h.get("seconds", 0))
        if s - last >= gap_sec: out.append(h); last = s
    return out


# ============================================
# DownSub API
# ============================================

def fetch_youtube_subs_downsub(video_url, formats=['txt', 'srt']):
    api_url = 'https://api.downsub.com/download'
    headers = {'Authorization': 'Bearer AIzalTjrrsT1cKdr4HSWUryzgFRiqNYc8XBzztm', 'Content-Type': 'application/json'}
    payload = {'url': video_url}
    results = {"srt": None, "txt": None, "title": None, "error": None}
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"[DownSub] attempt {attempt}/{max_retries}: fetching transcript for {video_url}")
            resp = requests.post(api_url, headers=headers, json=payload, timeout=55)
            resp.raise_for_status()
            data = resp.json()
            if data.get('status') != 'success':
                return {**results, "error": data.get('message', 'Unknown error')}

            subs = data.get('data', {}).get('subtitles', [])
            if not subs:
                return {**results, "error": "No subtitles were found for this video"}

            results["title"] = data.get('data', {}).get('title')
            selected = subs[0]
            for sub in subs:
                if "auto-generated" not in sub.get('language', '').lower():
                    selected = sub; break

            for fmt in selected.get('formats', []):
                f_type = fmt.get('format')
                if f_type in formats:
                    try:
                        f_resp = requests.get(fmt.get('url'), timeout=90)
                        f_resp.raise_for_status()
                        results[f_type] = f_resp.text
                    except: pass

            if results.get('srt') or results.get('txt'):
                return results
        except Exception as e:
            if attempt < max_retries: time.sleep(2 ** (attempt - 1))

    return {**results, "error": "Failed to fetch transcript after several attempts"}


# ============================================
# AI Analysis (NVIDIA MiniMax-M1)
# ============================================

class NvidiaRateLimitError(Exception):
    pass


def nvidia_ai_chat(messages):
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "model": NVIDIA_MODEL,
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.2,
        "top_p": 0.95,
        "stream": False,
    }

    for attempt in range(3):
        response = requests.post(NVIDIA_API_URL, headers=headers, json=payload, timeout=(10, 300))

        if response.status_code == 429:
            wait_seconds = int(response.headers.get("Retry-After", 20))
            if attempt == 2:
                raise NvidiaRateLimitError("NVIDIA rate limit reached. Wait a few minutes and try again.")
            logger.warning(f"NVIDIA rate limit reached. Waiting {wait_seconds}s before retry {attempt + 2}/3.")
            time.sleep(wait_seconds)
            continue

        if response.status_code >= 400:
            error_body = response.text[:2000]
            user_chars = sum(len(str(m.get("content", ""))) for m in messages if m.get("role") == "user")
            logger.error(
                "NVIDIA chat error: status=%s; model=%s; user_chars=%s; response=%s",
                response.status_code,
                NVIDIA_MODEL,
                user_chars,
                error_body,
            )
            response.raise_for_status()

        data = response.json()
        choices = data.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content")
            if content:
                return content

        logger.error(f"NVIDIA returned no text: {response.text[:500]}")
        return ""

    return ""


async def analyze_video_highlights_ai(srt_content, duration=0, title="", mode="highlights", progress_callback=None):
    if not NVIDIA_API_TOKEN or not srt_content:
        return []

    cues = parse_srt_cues(srt_content)
    if not cues: return []

    max_end = max(c["end_sec"] for c in cues)
    duration_cap = int(max(duration, int(max_end) + 1)) if duration > 0 else int(max_end) + 1

    time_windows = split_cues_into_time_windows(cues, window_sec=300, max_chars=12000)
    num_parts = len(time_windows)
    all_highlights = []
    logger.info(f"[AI] mode={mode}; transcript split into {num_parts} part(s)")

    async def fetch_moments_for_part(part_index, part_cues):
        part_srt = cues_to_srt_string(part_cues)
        t0, t1 = part_cues[0]["start_str"], part_cues[-1]["end_str"]
        logger.info(
            f"[AI] part {part_index + 1}/{num_parts}: range={t0}-{t1}; cues={len(part_cues)}; chars={len(part_srt)}"
        )

        if mode == "first_principles":
            task_desc = """STRIP AWAY all journalism, emotions, and narrative. Identify the "First Principles" (Foundational Truths).
            A First Principle is an underlying reality or structural cause that remains true even without names and places.
            Titles must be "Core Realities". Reasons must explain the "Undeniable Logic" behind the moment."""
            system_msg = "You are a geopolitical and strategic analyst. Extract the foundational principles behind the events. Return Arabic content only."
        else:
            task_desc = """Identify the most powerful, analytical, and discussion-focused moments.
            Focus strictly on moments containing deep political, military, or economic analysis, expert interviews, or major media citations.
            Avoid superficial or simple news readings.

            CRITICAL CONSTRAINTS for 'reason_ar':
            1. DO NOT describe the video, the analysis, or the narrator from the outside. DO NOT use phrases like 'يطرح المقطع الافتتاحي', 'يتناول التحليل', 'يصف التحليل', 'تستند هذه اللحظة', 'المقطع يوضح'.
            2. State the core analytical argument, fact, or news directly as a statement.
            3. Follow this structure for 'reason_ar' in Arabic:
               - [صياغة الحجة أو الخبر أو التحليل مباشرة وبشكل موضوعي]
               - خلاصة توضح كيف يمكن استخدام هذا الفيديو/المقطع لصناعة نقاش طويل ومترابط.
               - أي تناقضات أو وجهات نظر متعارضة في التحليل إن وجدت.
               - أي تطور عاجل أو خبر أخير متعلق بالقضية ومصدره إن وجد."""
            system_msg = "You are a senior geopolitical analyst. When writing 'reason_ar', write the facts and arguments directly. Never describe the video or the narrator's actions (e.g. do not say 'yashrah al-maqta'). Follow the structured format precisely in Arabic."

        prompt = f"""Below is segment (Part {part_index + 1}/{num_parts}) of a video transcript in SRT format.
VIDEO TITLE: {title}
TIME RANGE: {t0} to {t1}
TASK: {task_desc}
CONSTRAINTS:
1. EVERYTHING in ARABIC.
2. Result strictly in JSON list.
3. For each moment: title (max 5 words), start_time (exact SRT timestamp), seconds (integer), reason_ar (explanation).
        SRT SEGMENT:
{part_srt}"""

        try:
            messages = [{"role": "system", "content": system_msg}, {"role": "user", "content": prompt}]
            content = ""
            for attempt in range(1, 4):
                try:
                    content = await asyncio.to_thread(nvidia_ai_chat, messages)
                    break
                except requests.exceptions.Timeout as e:
                    logger.warning(f"[AI] part {part_index + 1}/{num_parts}: NVIDIA timeout on attempt {attempt}/3: {e}")
                    if attempt == 3:
                        raise
                    await asyncio.sleep(3 * attempt)
            match = re.search(r'\[.*\]', content.strip(), re.DOTALL)
            if match: return json.loads(match.group(0))
            logger.error(f"AI returned no JSON list for part {part_index}: {content[:500]}")
            return []
        except NvidiaRateLimitError:
            raise
        except Exception as e:
            logger.error(f"AI error part {part_index}: {e}")
            return []

    for i, part_cues in enumerate(time_windows):
        logger.info(f"[AI] part {i + 1}/{num_parts}: sending transcript segment to NVIDIA")
        chunk = await fetch_moments_for_part(i, part_cues)
        if chunk:
            for h in chunk:
                if isinstance(h, dict):
                    h["seconds"] = resolve_highlight_seconds(h, part_cues, i, duration_cap)
            all_highlights.extend(chunk)
            all_highlights = dedupe_highlights_by_time(all_highlights, gap_sec=5.0)
            all_highlights.sort(key=lambda x: x.get("seconds", 0))
            logger.info(f"[AI] part {i + 1}/{num_parts}: received {len(chunk)} item(s); total so far={len(all_highlights)}")
            if progress_callback:
                await progress_callback(all_highlights, i + 1, num_parts, False)
        else:
            logger.info(f"[AI] part {i + 1}/{num_parts}: no valid items returned")
            if progress_callback:
                await progress_callback(all_highlights, i + 1, num_parts, False)

    all_highlights = dedupe_highlights_by_time(all_highlights, gap_sec=5.0)
    all_highlights.sort(key=lambda x: x.get("seconds", 0))
    logger.info(f"[AI] complete: extracted {len(all_highlights)} item(s)")
    return all_highlights


async def run_analysis_background(video_id, mode):
    lock_key = f"{video_id}:{mode}"
    lock = ANALYSIS_LOCKS.setdefault(lock_key, asyncio.Lock())
    ANALYSIS_STATUS[lock_key] = {"state": "running", "completed_parts": 0, "total_parts": None, "items": 0}

    async with lock:
        db = SessionLocal()
        try:
            video = db.query(Video).filter(Video.id == video_id).first()
            if not video or not video.srt_transcript:
                logger.error(f"[AI] background analysis aborted: missing video or transcript for video_id={video_id}")
                return

            cached = video.first_principles if mode == "first_principles" else video.highlights
            if cached:
                try:
                    cached_items = json.loads(cached)
                    if cached_items:
                        logger.info(f"[AI] background analysis skipped: cache already exists for video_id={video_id}, mode={mode}")
                        return
                except Exception:
                    logger.info(f"[AI] background analysis will replace invalid cache for video_id={video_id}, mode={mode}")

            async def save_partial(items, completed_parts, total_parts, complete=False):
                current = db.query(Video).filter(Video.id == video_id).first()
                if not current:
                    return
                status_total = total_parts or ANALYSIS_STATUS.get(lock_key, {}).get("total_parts") or completed_parts
                ANALYSIS_STATUS[lock_key] = {
                    "state": "complete" if complete else "running",
                    "completed_parts": status_total if complete else completed_parts,
                    "total_parts": status_total,
                    "items": len(items),
                }
                payload = json.dumps(items, ensure_ascii=False)
                if mode == "first_principles":
                    current.first_principles = payload
                else:
                    current.highlights = payload
                db.add(current)
                db.commit()
                state = "complete" if complete else "partial"
                logger.info(f"[Progress] video_id={video_id}; mode={mode}; state={state}; parts={ANALYSIS_STATUS[lock_key]['completed_parts']}/{ANALYSIS_STATUS[lock_key]['total_parts']}; saved_items={len(items)}")

            logger.info(f"[AI Analysis] started background job: video_id={video_id}; mode={mode}; title={video.title}")
            highlights = await analyze_video_highlights_ai(
                video.srt_transcript,
                title=video.title,
                mode=mode,
                progress_callback=save_partial,
            )
            await save_partial(highlights, 0, 0, True)
            logger.info(f"[AI Analysis] finished background job: video_id={video_id}; mode={mode}; total_items={len(highlights)}")
        except NvidiaRateLimitError as e:
            ANALYSIS_STATUS[lock_key] = {"state": "error", "error": str(e)}
            logger.error(f"[AI Analysis] NVIDIA rate limit: {e}")
        except Exception as e:
            ANALYSIS_STATUS[lock_key] = {"state": "error", "error": str(e)}
            logger.error(f"[AI Analysis] background job failed: {e}")
        finally:
            db.close()


# ============================================
# Scraper Logic
# ============================================

def get_chrome_version():
    try:
        import winreg
        for hkey in [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]:
            for path in [r'Software\Google\Chrome\BLBeacon', r'Software\Google\Chrome\Update\Clients\{8A69D345-D564-463c-AFF1-A69D9E530F96}']:
                try:
                    key = winreg.OpenKey(hkey, path)
                    v, _ = winreg.QueryValueEx(key, 'version')
                    if v: return v
                except: continue
    except ImportError:
        try:
            import subprocess
            res = subprocess.run(["google-chrome", "--version"], capture_output=True, text=True, timeout=5)
            m = re.search(r"Google Chrome (\d+\.\d+\.\d+\.\d+)", res.stdout)
            if m: return m.group(1)
        except: pass
    except: pass
    return None

def download_chromedriver():
    import shutil as _shutil
    path_chromedriver = _shutil.which("chromedriver")
    if path_chromedriver:
        logger.info(f"Found chromedriver in PATH: {path_chromedriver}")
        return path_chromedriver

    name = "chromedriver.exe" if os.name == "nt" else "chromedriver"
    local_path = os.path.abspath(name)
    if os.path.exists(local_path):
        return local_path

    logger.error("chromedriver not found. Please place chromedriver.exe in the project folder.")
    return None


def extract_videos_js(driver):
    js = """
    var videos = [];
    var selectors = ['ytd-rich-item-renderer', 'ytd-video-renderer', 'ytd-grid-video-renderer', 'ytd-compact-video-renderer'];

    selectors.forEach(sel => {
        var items = document.querySelectorAll(sel);
        for (var i = 0; i < items.length; i++) {
            try {
                var el = items[i];
                var tl = el.querySelector('a#video-title-link') || el.querySelector('a#video-title') || el.querySelector('#video-title');
                var ch = el.querySelector('ytd-channel-name a') || el.querySelector('#channel-name a') || el.querySelector('.ytd-channel-name');

                if (tl) {
                    var t = tl.textContent.trim() || tl.getAttribute('title') || '';
                    var u = tl.href || '';
                    var c = ch ? ch.textContent.trim() : '';
                    if (t && u && u.includes('/watch')) {
                        if (!videos.some(v => v.url === u)) {
                            videos.push({title:t, url:u, channel:c});
                        }
                    }
                }
            } catch(e) {}
        }
    });

    if (!videos.length) {
        var links = document.querySelectorAll('a[href*="/watch"]');
        for (var i = 0; i < links.length; i++) {
            var l = links[i];
            var t = l.textContent.trim() || l.getAttribute('title') || '';
            var u = l.href;
            if (t.length > 5 && !videos.some(v => v.url === u)) {
                videos.push({title:t, url:u, channel:''});
            }
        }
    }
    return videos;
    """
    try: return driver.execute_script(js) or []
    except: return []

def save_videos_to_db(videos_raw):
    db = SessionLocal()
    added = 0
    seen_urls = set()
    try:
        for v in videos_raw:
            url = v.get('url', '')
            if not url or url in seen_urls: continue
            exists = db.query(Video).filter(Video.url == url).first()
            if not exists:
                db.add(Video(title=v.get('title',''), url=url, channel=v.get('channel',''),
                             video_id=extract_video_id(url), scraped_at=datetime.now()))
                added += 1
                seen_urls.add(url)
        if added: db.commit()
    except Exception as e:
        db.rollback(); logger.error(f"DB error: {e}")
    finally: db.close()
    return added

def scraper_loop():
    """Background scraper with auto-restart resilience"""
    while True:
        logger.info("=" * 50)
        logger.info("SCRAPER STARTING/RESTARTING...")
        logger.info("=" * 50)

        import undetected_chromedriver as uc
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.keys import Keys

        driver = None
        try:
            driver_path = download_chromedriver()
            options = uc.ChromeOptions()
            options.add_argument("--disable-popup-blocking")
            options.add_argument("--lang=en-US")
            options.add_argument("--accept-lang=en-US,en")
            if IS_HEADLESS:
                options.add_argument("--headless=new")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--disable-gpu")

            cv = get_chrome_version()
            vm = int(cv.split(".")[0]) if cv else None

            if driver_path and vm:
                driver = uc.Chrome(options=options, driver_executable_path=driver_path, version_main=vm)
            else:
                driver = uc.Chrome(options=options)

            logger.info("Chrome launched successfully!")

            # --- Login with retries ---
            max_retries = 2
            login_success = False
            for attempt in range(max_retries):
                try:
                    logger.info(f"Login attempt {attempt + 1}/{max_retries}...")
                    driver.get("https://stackoverflow.com/users/login")
                    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'button[data-provider="google"]')))

                    google_btn = driver.find_element(By.CSS_SELECTOR, 'button[data-provider="google"]')
                    google_btn.click()
                    time.sleep(4)

                    logger.info("Please log in manually in the opened browser. You have 3 minutes...")
                    for _ in range(60):
                        current_url = driver.current_url
                        if "stackoverflow.com" in current_url and "login" not in current_url:
                            break
                        if "youtube.com" in current_url:
                            break
                        time.sleep(3)

                    logger.info("Login wait completed!")
                    login_success = True
                    break
                except Exception as e:
                    logger.warning(f"Login attempt {attempt + 1} failed, retrying...")

            # --- Navigate to YouTube ---
            driver.get("https://www.youtube.com/")
            time.sleep(3)

            # Handle consent
            try:
                consent = driver.find_elements(By.CSS_SELECTOR, 'button[aria-label*="Accept all"], button[aria-label*="Reject all"], button[jsname="b3VHJd"]')
                if consent: consent[0].click(); time.sleep(2)
            except: pass

            # --- Main scraping loop ---
            extraction_count = 0
            while True:
                extraction_count += 1
                logger.info(f"=== Extraction #{extraction_count} ===")

                try: WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "ytd-app")))
                except: pass

                new_videos = extract_videos_js(driver)
                if not new_videos:
                    for i in range(4): driver.execute_script(f"window.scrollTo(0, {(i + 1) * 800});"); time.sleep(1)
                    new_videos = extract_videos_js(driver)

                added = save_videos_to_db(new_videos)
                logger.info(f"Cycle results: Extracted={len(new_videos)}, Added={added}")

                time.sleep(180)  # 3 min
                driver.refresh()
                time.sleep(5)

        except Exception as e:
            logger.error(f"Scraper error in loop: {e}")
            logger.info("Restarting scraper in 10 seconds...")
            time.sleep(10)
        finally:
            if driver:
                try: driver.quit()
                except: pass


# ============================================
# Guardian Live & TTS helpers
# ============================================

def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def extract_guardian_live_updates(html: str, source_url: str):
    soup = BeautifulSoup(html, "html.parser")
    updates = []
    seen = set()

    selectors = [
        '[id^="block-"]',
        '[data-testid="live-blog-block"]',
        'div[class*="live-blog-block"]',
        'article',
    ]

    for selector in selectors:
        for block in soup.select(selector):
            block_id = block.get("id") or block.get("data-id") or ""
            time_el = block.select_one("time")
            heading_el = block.select_one("h2, h3, [data-testid='headline']")
            body_nodes = block.select("p")

            body = _clean_text(" ".join(p.get_text(" ", strip=True) for p in body_nodes))
            title = _clean_text(heading_el.get_text(" ", strip=True) if heading_el else "")
            timestamp = ""
            timestamp_label = ""
            if time_el:
                timestamp = time_el.get("datetime") or ""
                timestamp_label = _clean_text(time_el.get_text(" ", strip=True))

            if not body and not title:
                continue

            fingerprint = block_id or f"{timestamp}|{title}|{body[:160]}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)

            text = body if body else title
            if len(text) < 20:
                continue

            updates.append({
                "id": fingerprint,
                "title": title,
                "text": text,
                "time": timestamp,
                "time_label": timestamp_label,
                "url": f"{source_url}#{block_id}" if block_id else source_url,
            })

    updates.sort(key=lambda item: item.get("time") or "", reverse=True)
    return updates


def discover_guardian_live_page_urls(html: str, current_url: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    current = urlparse(current_url)
    urls = []

    for link in soup.select('a[href*="page=with%3Ablock-"], a[href*="page=with:block-"]'):
        href = link.get("href")
        if not href:
            continue
        label = _clean_text(link.get_text(" ", strip=True)).lower()
        if label not in {"next", "previous", "oldest"}:
            continue
        absolute = urljoin(current_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc != current.netloc or parsed.path != current.path:
            continue
        cleaned = absolute.split("#", 1)[0]
        if cleaned not in urls:
            urls.append(cleaned)

    return urls


def format_guardian_full_text(updates: List[Dict[str, Any]]) -> str:
    ordered = sorted(updates, key=lambda item: item.get("time") or "")
    parts = []
    for index, update in enumerate(ordered, 1):
        header_parts = [f"{index}."]
        if update.get("time_label"):
            header_parts.append(update["time_label"])
        if update.get("title"):
            header_parts.append(update["title"])
        header = " ".join(header_parts)
        parts.append(f"{header}\n{update.get('text', '').strip()}")
    return "\n\n".join(part for part in parts if part.strip())


def fetch_guardian_live_all_pages(start_url: str, headers: Dict[str, str]):
    queue = [start_url.split("#", 1)[0]]
    visited = []
    pages_with_new_updates = []
    all_updates = []
    seen_updates = set()

    while queue and len(visited) < 12:
        page_url = queue.pop(0)
        if page_url in visited:
            continue

        response = requests.get(page_url, headers=headers, timeout=20)
        response.raise_for_status()
        html = response.text
        page_added = 0
        for update in extract_guardian_live_updates(html, page_url):
            update_id = update.get("id") or f"{update.get('time')}|{update.get('title')}|{update.get('text', '')[:160]}"
            if update_id in seen_updates:
                continue
            seen_updates.add(update_id)
            all_updates.append(update)
            page_added += 1

        visited.append(page_url)
        if page_added > 0:
            pages_with_new_updates.append(page_url)

        for discovered_url in discover_guardian_live_page_urls(html, page_url):
            if discovered_url not in visited and discovered_url not in queue:
                queue.append(discovered_url)

    all_updates.sort(key=lambda item: item.get("time") or "", reverse=True)
    return all_updates, pages_with_new_updates


class ArabicSpeechRequest(BaseModel):
    title: str = ""
    text: str


async def edge_arabic_tts_bytes(text: str) -> bytes:
    try:
        import edge_tts
    except ImportError:
        raise HTTPException(
            500,
            "edge-tts is not installed. Run: pip install -r requirements.txt",
        )

    audio = bytearray()
    try:
        communicate = edge_tts.Communicate(text, ARABIC_TTS_VOICE, rate="+0%", volume="+0%")
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
    except Exception as e:
        logger.error(f"Edge Arabic TTS failed: voice={ARABIC_TTS_VOICE}; error={e}")
        raise HTTPException(502, f"Arabic TTS failed for voice {ARABIC_TTS_VOICE}: {e}")

    if not audio:
        raise HTTPException(502, "The Arabic TTS service returned no audio.")
    return bytes(audio)


def groq_arabic_speech_text(title: str, text: str) -> str:
    source = _clean_text(f"{title}. {text}" if title else text)
    if not source:
        return ""

    if GROQ_API_KEY:
        try:
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": GROQ_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You translate live football updates into clear Modern Standard Arabic "
                            "for immediate text-to-speech. Return Arabic only. Keep names, teams, "
                            "scores, and times accurate. Do not add commentary."
                        ),
                    },
                    {
                        "role": "user",
                        "content": source[:5000],
                    },
                ],
                "temperature": 0.1,
                "max_tokens": 700,
            }

            response = requests.post(f"{GROQ_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                return _clean_text(content)
        except Exception as e:
            logger.error(f"Groq translation failed: {e}")

    # Fallback to NVIDIA MiniMax if GROQ_API_KEY is not set or failed
    if NVIDIA_API_TOKEN:
        try:
            logger.info("Using NVIDIA MiniMax translation fallback...")
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You translate live football updates into clear Modern Standard Arabic "
                        "for immediate text-to-speech. Return Arabic only. Keep names, teams, "
                        "scores, and times accurate. Do not add commentary."
                    ),
                },
                {
                    "role": "user",
                    "content": source[:5000],
                },
            ]
            content = nvidia_ai_chat(messages)
            if content:
                return _clean_text(content)
        except Exception as e:
            logger.error(f"NVIDIA MiniMax translation fallback failed: {e}")

    return source


# ============================================
# FastAPI App
# ============================================

@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=scraper_loop, daemon=True).start()
    yield

app = FastAPI(title="YouTube Intelligence", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/api/videos")
async def get_videos(page: int = 1, limit: int = 12, db: Session = Depends(get_db)):
    total = db.query(Video).count()
    videos = db.query(Video).order_by(desc(Video.scraped_at)).offset((page-1)*limit).limit(limit).all()
    return {"videos": [{"id":v.id,"title":v.title,"url":v.url,"channel":v.channel,
                        "video_id":v.video_id or extract_video_id(v.url),
                        "scraped_at":v.scraped_at.isoformat() if v.scraped_at else ""} for v in videos],
            "total": total, "page": page, "limit": limit}

@app.get("/api/video-insight/{video_id}")
async def get_video_insight(video_id: int, mode: str = "highlights", refresh: bool = False, db: Session = Depends(get_db)):
    """Fetch transcript with DownSub and analyze it with NVIDIA MiniMax."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video: raise HTTPException(404, "Video not found")

    # Step 1: Get transcript from DownSub (or cache)
    if not video.srt_transcript:
        logger.info(f"[DownSub] transcript not cached; fetching for: {video.title}")
        result = await asyncio.to_thread(fetch_youtube_subs_downsub, video.url)
        srt = result.get("srt")
        if srt:
            video.srt_transcript = srt
            video.full_transcript = result.get("txt")
            if result.get("title"):
                video.title = result.get("title")
            db.add(video)
            db.commit()
            db.refresh(video)
            logger.info(f"[Cache] saved transcript and title for video_id={video_id}")
        else:
            return {"video_id": video_id, "highlights": [], "mode": mode, "error": result.get("error", "Failed to fetch transcript from DownSub")}

    srt = video.srt_transcript

    # Step 2: AI Analysis
    if not NVIDIA_API_TOKEN:
        return {"video_id": video_id, "highlights": [], "mode": mode, "error": "NVIDIA_API_TOKEN is not configured"}

    lock_key = f"{video_id}:{mode}"
    lock = ANALYSIS_LOCKS.setdefault(lock_key, asyncio.Lock())

    db.refresh(video)
    cached = video.first_principles if mode == "first_principles" else video.highlights
    if cached and not refresh:
        try:
            cached_items = json.loads(cached)
            status = ANALYSIS_STATUS.get(lock_key, {})
            analyzing = status.get("state") == "running"
            if cached_items or analyzing:
                logger.info(f"[API] returning {'partial' if analyzing else 'cached'} results: video_id={video_id}; mode={mode}; items={len(cached_items)}; analyzing={analyzing}; progress={status}")
                return {
                    "video_id": video_id,
                    "highlights": cached_items,
                    "mode": mode,
                    "cached": True,
                    "analyzing": analyzing,
                    "progress": status,
                }
            logger.info(f"[API] ignoring empty cached {mode} results for video_id={video_id}; scheduling a new analysis")
        except: pass

    if refresh and cached and not lock.locked():
        logger.info(f"[API] refresh requested: clearing cached {mode} results for video_id={video_id}")
        if mode == "first_principles":
            video.first_principles = None
        else:
            video.highlights = None
        db.add(video)
        db.commit()
        db.refresh(video)

    status = ANALYSIS_STATUS.get(lock_key, {})
    is_running = status.get("state") == "running"

    if not is_running and not lock.locked():
        logger.info(f"[AI Analysis] scheduling background job: video_id={video_id}; mode={mode}; title={video.title}")
        ANALYSIS_STATUS[lock_key] = {"state": "running", "completed_parts": 0, "total_parts": None, "items": 0}
        asyncio.create_task(run_analysis_background(video_id, mode))
    else:
        logger.info(f"[AI Analysis] already running: video_id={video_id}; mode={mode}")

    logger.info(f"[API] analysis pending: video_id={video_id}; mode={mode}; no items available yet")
    return {"video_id": video_id, "highlights": [], "mode": mode, "cached": False, "analyzing": True}

@app.get("/api/video-insight-by-ytid/{yt_id}")
async def get_video_insight_by_ytid(yt_id: str, mode: str = "highlights", refresh: bool = False, db: Session = Depends(get_db)):
    """Fetch insight by YouTube video id."""
    video = db.query(Video).filter(Video.video_id == yt_id).first()
    if not video:
        video = db.query(Video).filter(Video.url.like(f"%{yt_id}%")).first()

    if not video:
        video_url = f"https://www.youtube.com/watch?v={yt_id}"
        logger.info(f"[Video] {yt_id} not found in DB; creating record")
        video = Video(
            title=f"YouTube Video {yt_id}",
            url=video_url,
            video_id=yt_id,
            scraped_at=datetime.now()
        )
        db.add(video)
        db.commit()
        db.refresh(video)

    return await get_video_insight(video.id, mode, refresh, db)

@app.post("/api/telegram-publish/{video_id}")
async def publish_to_telegram(video_id: int, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video: raise HTTPException(404)

    msg = f"{video.title}\n\n{video.url}"
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": CHANNEL_ID, "text": msg, "parse_mode": "HTML"}, timeout=15)
        if r.status_code == 200:
            return {"status": "success", "message": "Published successfully."}
        return {"status": "error", "message": f"Error: {r.text}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/guardian-live")
async def guardian_live(url: str = Query(..., min_length=10)):
    if not url.startswith("https://www.theguardian.com/"):
        raise HTTPException(400, "Only theguardian.com live URLs are supported.")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }
    try:
        updates, page_urls = await asyncio.to_thread(fetch_guardian_live_all_pages, url, headers)
    except requests.RequestException as e:
        raise HTTPException(502, f"Could not fetch Guardian live page: {e}")

    return {
        "source": url,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "count": len(updates),
        "page_count": len(page_urls),
        "pages": page_urls,
        "full_text": format_guardian_full_text(updates),
        "updates": updates,
    }


@app.post("/api/arabic-speech-text")
async def arabic_speech_text(payload: ArabicSpeechRequest):
    try:
        arabic_text = await asyncio.to_thread(groq_arabic_speech_text, payload.title, payload.text)
        return {
            "text": arabic_text,
            "provider": "groq" if GROQ_API_KEY else "nvidia-minimax",
            "model": GROQ_MODEL if GROQ_API_KEY else NVIDIA_MODEL,
        }
    except requests.RequestException as e:
        logger.error(f"Arabic speech conversion failed: {e}")
        raise HTTPException(502, f"Request failed: {e}")


@app.post("/api/arabic-tts")
async def arabic_tts(payload: ArabicSpeechRequest):
    try:
        arabic_text = await asyncio.to_thread(groq_arabic_speech_text, payload.title, payload.text)
        audio = await edge_arabic_tts_bytes(arabic_text[:4000])
        return Response(
            content=audio,
            media_type="audio/mpeg",
            headers={
                "Cache-Control": "no-store",
                "X-TTS-Voice": ARABIC_TTS_VOICE,
            },
        )
    except requests.RequestException as e:
        logger.error(f"Arabic speech conversion failed before TTS: {e}")
        raise HTTPException(502, f"Request failed: {e}")


# Static files
public_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
if os.path.exists(public_dir):
    app.mount("/", StaticFiles(directory=public_dir, html=True), name="public")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

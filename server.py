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
from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn
import requests
from requests.exceptions import Timeout

# SQLAlchemy
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, desc
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")
NVIDIA_API_URL = os.environ.get("NVIDIA_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "minimaxai/minimax-m3")
NVIDIA_API_TOKEN = os.environ.get(
    "NVIDIA_API_TOKEN",
    "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",
)
IS_HEADLESS = os.environ.get("HEADLESS", "0") == "1"
# Telegram
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@osamaalshahape")
ANALYSIS_LOCKS = {}
ANALYSIS_STATUS = {}

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
# SRT & Timing Utilities (from world-news)
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

def split_cues_into_time_windows(cues, window_sec=240, max_chars=5000):
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
# AI Analysis (NVIDIA MiniMax-M3)
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
        "max_tokens": 700,
        "temperature": 0.2,
        "top_p": 0.95,
        "stream": False,
    }

    for attempt in range(3):
        response = requests.post(NVIDIA_API_URL, headers=headers, json=payload, timeout=(10, 45))

        if response.status_code == 429:
            wait_seconds = min(int(response.headers.get("Retry-After", 20)), 60)
            if attempt == 2:
                raise NvidiaRateLimitError("NVIDIA rate limit reached. Wait a few minutes and try again.")
            logger.warning(f"NVIDIA rate limit reached. Waiting {wait_seconds}s before retry {attempt + 2}/3.")
            time.sleep(wait_seconds)
            continue

        if response.status_code >= 400:
            logger.error("NVIDIA chat error: status=%s; response=%s", response.status_code, response.text[:2000])
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
    
    time_windows = split_cues_into_time_windows(cues, window_sec=240, max_chars=5000)
    num_parts = len(time_windows)
    all_highlights = []
    logger.info(f"[AI] mode={mode}; transcript split into {num_parts} part(s)")

    async def fetch_moments_for_part(part_index, part_cues):
        part_srt = cues_to_srt_string(part_cues)
        t0, t1 = part_cues[0]["start_str"], part_cues[-1]["end_str"]

        if mode == "first_principles":
            task_desc = "استخرج أهم مبدأين تأسيسيين فقط من هذا الجزء. اكتب بالعربية فقط."
            system_msg = "أنت محلل جيوسياسي وفيلسوف استراتيجي. استخرج المبادئ المؤسسة والحقائق الصلبة التي تحرك الأحداث. أعد المحتوى بالعربية فقط."
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
            content = await asyncio.to_thread(nvidia_ai_chat, messages)
            match = re.search(r'\[.*\]', content.strip(), re.DOTALL)
            if match:
                return json.loads(match.group(0))
            return []
        except (NvidiaRateLimitError, Timeout):
            raise
        except Exception as e:
            logger.error(f"AI error part {part_index}: {e}")
            return None

    semaphore = asyncio.Semaphore(2)

    async def fetch_limited(i, part_cues):
        async with semaphore:
            logger.info(f"[AI] part {i + 1}/{num_parts}: sending to MiniMax")
            try:
                return i, part_cues, await fetch_moments_for_part(i, part_cues)
            except (NvidiaRateLimitError, Timeout):
                logger.error(f"[AI] part {i + 1}/{num_parts}: MiniMax request timed out or rate limited")
                return i, part_cues, None

    tasks = [fetch_limited(i, part_cues) for i, part_cues in enumerate(time_windows)]
    completed_count = 0
    failed_count = 0
    for done in asyncio.as_completed(tasks):
        i, part_cues, chunk = await done
        completed_count += 1
        if chunk is None:
            failed_count += 1
            logger.error(f"[AI] part {i + 1}/{num_parts}: failed")
            if progress_callback:
                await progress_callback(all_highlights, completed_count, num_parts, False)
        elif chunk:
            for h in chunk:
                if isinstance(h, dict):
                    h["seconds"] = resolve_highlight_seconds(h, part_cues, i, duration_cap)
            all_highlights.extend(chunk)
            all_highlights = dedupe_highlights_by_time(all_highlights, gap_sec=5.0)
            all_highlights.sort(key=lambda x: x.get("seconds", 0))
            logger.info(f"[AI] part {i + 1}/{num_parts}: received {len(chunk)} item(s)")
            if progress_callback:
                await progress_callback(all_highlights, completed_count, num_parts, False)
        elif progress_callback:
            await progress_callback(all_highlights, completed_count, num_parts, False)

    all_highlights = dedupe_highlights_by_time(all_highlights, gap_sec=5.0)
    all_highlights.sort(key=lambda x: x.get("seconds", 0))
    if not all_highlights and failed_count == num_parts:
        raise RuntimeError("MiniMax analysis timed out for all transcript parts")
    logger.info(f"[AI] complete: extracted {len(all_highlights)} item(s)")
    return all_highlights


# ============================================
# Scraper Logic
# ============================================

def get_chrome_version():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Google\Chrome\BLBeacon')
        return winreg.QueryValueEx(key, 'version')[0]
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
    import shutil
    path_chromedriver = shutil.which("chromedriver")
    if path_chromedriver:
        logger.info(f"Found chromedriver in PATH: {path_chromedriver}")
        return path_chromedriver

    name = "chromedriver.exe" if os.name == "nt" else "chromedriver"
    local_path = os.path.abspath(name)
    if os.path.exists(local_path):
        return local_path

    version = get_chrome_version()
    if not version: return None
    try:
        # Download Windows ZIP if running locally on Windows
        if os.name == "nt":
            r = requests.get(f"https://registry.npmmirror.com/-/binary/chrome-for-testing/{version}/win64/chromedriver-win64.zip", timeout=30)
            z = zipfile.ZipFile(io.BytesIO(r.content)); z.extractall('.')
            shutil.copy(os.path.join("chromedriver-win64", "chromedriver.exe"), ".")
            return os.path.abspath("chromedriver.exe")
    except Exception as e:
        logger.error(f"Failed to auto-download chromedriver: {e}")
    return None


def extract_videos_js(driver):
    js = """
    var videos = [];
    var items = document.querySelectorAll('ytd-rich-item-renderer');
    for (var i = 0; i < items.length; i++) {
        try {
            var el = items[i];
            var tl = el.querySelector('a#video-title-link') || el.querySelector('a#video-title');
            var ch = el.querySelector('ytd-channel-name a') || el.querySelector('#channel-name a');
            if (tl) {
                var t = tl.textContent.trim() || tl.getAttribute('title') || '';
                var u = tl.href || '';
                var c = ch ? ch.textContent.trim() : '';
                if (t && u && u.includes('/watch')) videos.push({title:t, url:u, channel:c});
            }
        } catch(e) {}
    }
    if (!videos.length) {
        var links = document.querySelectorAll('a[href*="/watch"]');
        for (var i = 0; i < links.length; i++) {
            var l = links[i], t = l.textContent.trim(), u = l.href;
            if (t.length > 5) videos.push({title:t, url:u, channel:''});
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
            
            # Check if exists in DB
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

                    logger.info("Please log in manually in the opened browser. You have 3 minutes to complete the verification...")
                    
                    # Wait for manual login (up to 3 minutes)
                    # When login is successful, Google redirects back to StackOverflow.
                    for _ in range(60):
                        current_url = driver.current_url
                        if "stackoverflow.com" in current_url and "login" not in current_url:
                            break
                        if "youtube.com" in current_url:
                            break
                        time.sleep(3)

                    logger.info("Manual login wait completed!")
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

                # Re-check load
                try: WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "ytd-app")))
                except: pass

                new_videos = extract_videos_js(driver)
                if not new_videos:
                    # Aggressive scroll
                    for i in range(4): driver.execute_script(f"window.scrollTo(0, {(i + 1) * 800});"); time.sleep(1)
                    new_videos = extract_videos_js(driver)

                added = save_videos_to_db(new_videos)
                logger.info(f"Cycle results: Extracted={len(new_videos)}, Added={added}")

                time.sleep(180) # 3 min
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
# FastAPI App
# ============================================

@asynccontextmanager
async def lifespan(app):
    # The recommendations scraper is disabled; the extension analyzes the current video on demand.
    yield

app = FastAPI(title="YouTube Intelligence", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


async def run_analysis_background(video_id: int, mode: str):
    lock_key = f"{video_id}:{mode}"
    lock = ANALYSIS_LOCKS.setdefault(lock_key, asyncio.Lock())
    if lock.locked():
        return

    async with lock:
        db = SessionLocal()
        try:
            ANALYSIS_STATUS[lock_key] = {"state": "running", "items": 0}
            video = db.query(Video).filter(Video.id == video_id).first()
            if not video or not video.srt_transcript:
                ANALYSIS_STATUS[lock_key] = {"state": "error", "error": "Missing video or transcript"}
                return

            async def save_partial(items, completed_parts, total_parts, complete=False):
                current = db.query(Video).filter(Video.id == video_id).first()
                if not current:
                    return
                if not items and not complete:
                    ANALYSIS_STATUS[lock_key] = {
                        "state": "running",
                        "items": 0,
                        "completed_parts": completed_parts,
                        "total_parts": total_parts,
                    }
                    return
                payload = json.dumps(items, ensure_ascii=False)
                if mode == "first_principles":
                    current.first_principles = payload
                else:
                    current.highlights = payload
                db.add(current)
                db.commit()
                ANALYSIS_STATUS[lock_key] = {
                    "state": "complete" if complete else "running",
                    "items": len(items),
                    "completed_parts": total_parts if complete else completed_parts,
                    "total_parts": total_parts,
                }
                logger.info(
                    f"[AI Analysis] {'complete' if complete else 'partial'}: "
                    f"video_id={video_id}; mode={mode}; items={len(items)}; "
                    f"parts={ANALYSIS_STATUS[lock_key]['completed_parts']}/{total_parts}"
                )

            logger.info(f"[AI Analysis] started: video_id={video_id}; mode={mode}; title={video.title}")
            highlights = await analyze_video_highlights_ai(
                video.srt_transcript,
                title=video.title,
                mode=mode,
                progress_callback=save_partial,
            )
            payload = json.dumps(highlights, ensure_ascii=False)
            if mode == "first_principles":
                video.first_principles = payload
            else:
                video.highlights = payload
            db.add(video)
            db.commit()
            total_parts = ANALYSIS_STATUS.get(lock_key, {}).get("total_parts") or 0
            ANALYSIS_STATUS[lock_key] = {"state": "complete", "items": len(highlights), "completed_parts": total_parts, "total_parts": total_parts}
            logger.info(f"[AI Analysis] complete: video_id={video_id}; mode={mode}; items={len(highlights)}")
        except NvidiaRateLimitError as e:
            ANALYSIS_STATUS[lock_key] = {"state": "error", "error": str(e)}
            logger.error(f"[AI Analysis] rate limited: {e}")
        except Exception as e:
            ANALYSIS_STATUS[lock_key] = {"state": "error", "error": str(e)}
            logger.error(f"[AI Analysis] failed: {e}")
        finally:
            db.close()

@app.get("/api/videos")
async def get_videos(page: int = 1, limit: int = 12, db: Session = Depends(get_db)):
    total = db.query(Video).count()
    videos = db.query(Video).order_by(desc(Video.scraped_at)).offset((page-1)*limit).limit(limit).all()
    return {"videos": [{"id":v.id,"title":v.title,"url":v.url,"channel":v.channel,
                        "video_id":v.video_id or extract_video_id(v.url),
                        "scraped_at":v.scraped_at.isoformat() if v.scraped_at else ""} for v in videos],
            "total": total, "page": page, "limit": limit}

@app.get("/api/video-insight/{video_id}")
async def get_video_insight(
    video_id: int,
    mode: str = "highlights",
    refresh: bool = False,
    db: Session = Depends(get_db),
):
    """Fetch a transcript from DownSub and analyze it with MiniMax-M3."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(404, "Video not found")

    if mode not in {"highlights", "first_principles"}:
        raise HTTPException(400, "Unsupported mode")

    lock_key = f"{video_id}:{mode}"
    status = ANALYSIS_STATUS.get(lock_key, {})
    cached = video.first_principles if mode == "first_principles" else video.highlights
    if cached and not refresh:
        try:
            cached_items = json.loads(cached)
            analyzing = status.get("state") == "running"
            if status.get("state") == "error" and not cached_items:
                return {
                    "video_id": video_id,
                    "highlights": [],
                    "mode": mode,
                    "cached": False,
                    "analyzing": False,
                    "error": status.get("error", "Analysis failed"),
                    "progress": status,
                }
            return {
                "video_id": video_id,
                "highlights": cached_items,
                "mode": mode,
                "cached": True,
                "analyzing": analyzing,
                "progress": status,
            }
        except Exception:
            logger.warning(f"[Cache] invalid cached JSON ignored: video_id={video_id}; mode={mode}")

    if refresh:
        if mode == "first_principles":
            video.first_principles = None
        else:
            video.highlights = None
        db.add(video)
        db.commit()
        db.refresh(video)
        cached = None

    if not video.srt_transcript:
        logger.info(f"[DownSub] transcript not cached; fetching: video_id={video_id}; title={video.title}")
        result = await asyncio.to_thread(fetch_youtube_subs_downsub, video.url)
        srt = result.get("srt")
        if not srt:
            return {
                "video_id": video_id,
                "highlights": [],
                "mode": mode,
                "error": result.get("error", "Failed to fetch transcript from DownSub"),
            }

        video.srt_transcript = srt
        video.full_transcript = result.get("txt")
        if result.get("title"):
            video.title = result.get("title")
        db.add(video)
        db.commit()
        db.refresh(video)
        logger.info(f"[Cache] saved transcript: video_id={video_id}; title={video.title}")

    if not NVIDIA_API_TOKEN:
        return {"video_id": video_id, "highlights": [], "mode": mode, "error": "NVIDIA_API_TOKEN is not configured"}

    status = ANALYSIS_STATUS.get(lock_key, {})
    if status.get("state") != "running":
        logger.info(f"[AI Analysis] scheduling: video_id={video_id}; mode={mode}; title={video.title}")
        ANALYSIS_STATUS[lock_key] = {"state": "running", "items": 0}
        asyncio.create_task(run_analysis_background(video_id, mode))
    else:
        logger.info(f"[AI Analysis] already running: video_id={video_id}; mode={mode}")

    return {
        "video_id": video_id,
        "highlights": [],
        "mode": mode,
        "cached": False,
        "analyzing": True,
        "progress": ANALYSIS_STATUS.get(lock_key, {"state": "running", "items": 0}),
    }

@app.get("/api/video-insight-by-ytid/{yt_id}")
async def get_video_insight_by_ytid(
    yt_id: str,
    mode: str = "highlights",
    refresh: bool = False,
    db: Session = Depends(get_db),
):
    """Fetch insight by YouTube video id."""
    video = db.query(Video).filter(Video.video_id == yt_id).first()
    if not video:
        # Fallback if video_id is not exactly stored but url contains it
        video = db.query(Video).filter(Video.url.like(f"%{yt_id}%")).first()
    
    if not video:
        # Create the video record on demand for the browser extension.
        video = Video(
            title="YouTube Video",
            url=f"https://www.youtube.com/watch?v={yt_id}",
            video_id=yt_id,
            scraped_at=datetime.now()
        )
        db.add(video)
        db.commit()
        db.refresh(video)
        logger.info(f"[Video] created record for YouTube id {yt_id}")
    
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
        return {"status": "error", "message": f"ط®ط·ط£: {r.text}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# Static files
public_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
if os.path.exists(public_dir):
    app.mount("/", StaticFiles(directory=public_dir, html=True), name="public")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


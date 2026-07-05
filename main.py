import time
import json
import logging
from datetime import datetime
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
import os
import requests
import zipfile
import io
import shutil
import winreg

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

EMAIL = "alshhabi0000@gmail.com"
PASSWORD = "Asdfgh123@"

def get_chrome_version():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Google\Chrome\BLBeacon')
        version = winreg.QueryValueEx(key, 'version')[0]
        return version
    except Exception as e:
        logging.warning("Could not detect Chrome version from registry, using fallback.")
        return "148.0.7778.97"

def download_chromedriver():
    local_path = os.path.abspath("chromedriver.exe")
    if os.path.exists(local_path):
        logging.info("تم العثور على chromedriver.exe محلياً، سيتم استخدامه ولن يتم التنزيل من الإنترنت.")
        return local_path
    
    logging.error("لم يتم العثور على chromedriver.exe في المجلد! يرجى وضعه يدوياً في نفس مسار البرنامج.")
    return None


def extract_videos_js(driver):
    """Extract videos using JavaScript - most reliable method for YouTube's complex DOM."""
    js_code = """
    var videos = [];
    
    // Strategy 1: ytd-rich-item-renderer (YouTube home page - main grid)
    var richItems = document.querySelectorAll('ytd-rich-item-renderer');
    for (var i = 0; i < richItems.length; i++) {
        try {
            var el = richItems[i];
            var titleLink = el.querySelector('a#video-title-link') || el.querySelector('a#video-title');
            var channelEl = el.querySelector('ytd-channel-name a') || 
                           el.querySelector('#channel-name a') ||
                           el.querySelector('ytd-channel-name #text') ||
                           el.querySelector('#text-container yt-formatted-string a');
            
            if (titleLink) {
                var title = titleLink.textContent.trim() || titleLink.getAttribute('title') || '';
                var url = titleLink.href || titleLink.getAttribute('href') || '';
                var channel = '';
                if (channelEl) {
                    channel = channelEl.textContent.trim();
                }
                if (title && url && url.includes('/watch')) {
                    videos.push({title: title, url: url, channel: channel});
                }
            }
        } catch(e) {}
    }
    
    // Strategy 2: ytd-video-renderer (search results & some pages)
    if (videos.length === 0) {
        var videoRenderers = document.querySelectorAll('ytd-video-renderer');
        for (var i = 0; i < videoRenderers.length; i++) {
            try {
                var el = videoRenderers[i];
                var titleLink = el.querySelector('a#video-title');
                var channelEl = el.querySelector('ytd-channel-name a') || el.querySelector('#channel-name a');
                
                if (titleLink) {
                    var title = titleLink.textContent.trim() || titleLink.getAttribute('title') || '';
                    var url = titleLink.href || '';
                    var channel = channelEl ? channelEl.textContent.trim() : '';
                    if (title && url && url.includes('/watch')) {
                        videos.push({title: title, url: url, channel: channel});
                    }
                }
            } catch(e) {}
        }
    }
    
    // Strategy 3: Generic - find ALL video title links on page
    if (videos.length === 0) {
        var allLinks = document.querySelectorAll('a[href*="/watch"]');
        for (var i = 0; i < allLinks.length; i++) {
            try {
                var link = allLinks[i];
                var title = link.textContent.trim() || link.getAttribute('title') || link.getAttribute('aria-label') || '';
                var url = link.href || '';
                // Skip very short text (likely not a video title)
                if (title.length > 5 && url.includes('/watch') && !url.includes('&list=')) {
                    // Try to avoid duplicates
                    var isDuplicate = false;
                    for (var j = 0; j < videos.length; j++) {
                        if (videos[j].url === url) { isDuplicate = true; break; }
                    }
                    if (!isDuplicate) {
                        videos.push({title: title, url: url, channel: ''});
                    }
                }
            } catch(e) {}
        }
    }
    
    return videos;
    """
    try:
        result = driver.execute_script(js_code)
        return result if result else []
    except Exception as e:
        logging.error(f"JS extraction error: {e}")
        return []


def extract_videos(driver):
    """Extract videos with multiple strategies."""
    
    # First try JavaScript extraction (most reliable)
    videos_raw = extract_videos_js(driver)
    
    videos = []
    seen_urls = set()
    for v in videos_raw:
        url = v.get('url', '')
        if url and url not in seen_urls:
            seen_urls.add(url)
            videos.append({
                "title": v.get('title', '').strip(),
                "url": url,
                "channel": v.get('channel', '').strip(),
                "scraped_at": datetime.now().isoformat()
            })
    
    return videos


def debug_page_state(driver):
    """Log useful debugging info when no videos are found."""
    try:
        current_url = driver.current_url
        title = driver.title
        logging.info(f"Current URL: {current_url}")
        logging.info(f"Page title: {title}")
        
        # Check if we're on a consent/cookie page
        page_source = driver.page_source[:3000]
        if 'consent' in page_source.lower() or 'agree' in page_source.lower():
            logging.warning("Possible consent/cookie dialog detected!")
            # Try to click consent button
            try:
                consent_btns = driver.find_elements(By.CSS_SELECTOR, 
                    'button[aria-label*="Accept"], button[aria-label*="agree"], '
                    'button[jsname="b3VHJd"], form button')
                for btn in consent_btns:
                    if btn.is_displayed():
                        logging.info(f"Clicking consent button: {btn.text}")
                        btn.click()
                        time.sleep(3)
                        break
            except Exception:
                pass
        
        # Count elements on page to understand structure
        counts = driver.execute_script("""
            return {
                'ytd-rich-item-renderer': document.querySelectorAll('ytd-rich-item-renderer').length,
                'ytd-video-renderer': document.querySelectorAll('ytd-video-renderer').length,
                'ytd-rich-grid-media': document.querySelectorAll('ytd-rich-grid-media').length,
                'a[href*=watch]': document.querySelectorAll('a[href*="/watch"]').length,
                'ytd-app': document.querySelectorAll('ytd-app').length,
                'body_children': document.body ? document.body.children.length : 0
            };
        """)
        logging.info(f"Page element counts: {counts}")
        
    except Exception as e:
        logging.error(f"Debug error: {e}")


def wait_for_youtube_load(driver, timeout=30):
    """Wait for YouTube page to fully load its dynamic content."""
    logging.info("Waiting for YouTube content to load...")
    
    # Wait for the ytd-app element which is YouTube's main container
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.TAG_NAME, "ytd-app"))
        )
    except Exception:
        logging.warning("ytd-app not found, page may not have loaded properly")
    
    # Scroll down progressively to trigger lazy loading
    for i in range(5):
        driver.execute_script(f"window.scrollTo(0, {(i + 1) * 600});")
        time.sleep(1.5)
    
    # Scroll back to top
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(2)
    
    # Wait until at least some video links appear
    for attempt in range(10):
        count = driver.execute_script(
            "return document.querySelectorAll('a[href*=\"/watch\"]').length;"
        )
        if count > 0:
            logging.info(f"Found {count} video links on page after {attempt + 1} checks")
            return True
        time.sleep(2)
    
    logging.warning("No video links found after waiting")
    return False


def handle_google_login(driver):
    """Handle Google login through StackOverflow with retries."""
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            logging.info(f"Login attempt {attempt + 1}/{max_retries}...")
            logging.info("Going to StackOverflow to log in via Google...")
            driver.get("https://stackoverflow.com/users/login")
            
            # Wait for the page to load
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'button[data-provider="google"], .s-btn__google'))
            )
            time.sleep(2)
            
            # Click "Log in with Google"
            google_btn = None
            for selector in ['button[data-provider="google"]', '.s-btn__google', 'button.s-btn__icon']:
                try:
                    buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                    for btn in buttons:
                        if btn.is_displayed() and ('google' in btn.text.lower() or 'google' in btn.get_attribute('innerHTML').lower()):
                            google_btn = btn
                            break
                    if google_btn:
                        break
                except Exception:
                    continue
            
            if not google_btn:
                # Try finding by text content
                all_buttons = driver.find_elements(By.TAG_NAME, 'button')
                for btn in all_buttons:
                    if 'google' in btn.text.lower():
                        google_btn = btn
                        break
            
            if not google_btn:
                logging.error("Could not find Google login button")
                if attempt < max_retries - 1:
                    time.sleep(3)
                    continue
                return False
            
            google_btn.click()
            time.sleep(5)
            
            # Enter email
            logging.info("Entering email...")
            email_input = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[type="email"]'))
            )
            email_input.clear()
            time.sleep(0.5)
            email_input.send_keys(EMAIL)
            time.sleep(1)
            email_input.send_keys(Keys.ENTER)
            
            time.sleep(5)
            
            # Enter password
            logging.info("Entering password...")
            password_input = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[type="password"]'))
            )
            time.sleep(1)  # Wait for animation
            password_input.clear()
            time.sleep(0.5)
            password_input.send_keys(PASSWORD)
            time.sleep(1)
            password_input.send_keys(Keys.ENTER)
            
            time.sleep(10)  # Wait for login to complete and redirect
            logging.info("Login completed successfully!")
            return True
            
        except Exception as e:
            logging.error(f"Login attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(3)
            else:
                return False
    
    return False


def login_and_scrape():
    driver_path = download_chromedriver()
    
    options = uc.ChromeOptions()
    # No user-data-dir specified, so cookies are NOT saved across restarts.
    # This complies with the requirement to not keep user logged in using cookies permanently.
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--lang=en-US")  # Force English for consistent selectors
    options.add_argument("--accept-lang=en-US,en")
    
    if driver_path:
        # Prevent undetected_chromedriver from downloading and timing out
        driver = uc.Chrome(options=options, driver_executable_path=driver_path, version_main=int(get_chrome_version().split(".")[0]))
    else:
        driver = uc.Chrome(options=options)
    
    try:
        # Step 1: Login
        login_success = handle_google_login(driver)
        if not login_success:
            logging.error("Failed to login after all retries. Continuing without login...")
        
        # Step 2: Navigate to YouTube
        logging.info("Navigating to YouTube main page...")
        driver.get("https://www.youtube.com/")
        time.sleep(3)
        
        # Handle potential consent dialog
        try:
            consent_btns = driver.find_elements(By.CSS_SELECTOR,
                'button[aria-label*="Accept all"], button[aria-label*="Reject all"], '
                'button[jsname="b3VHJd"], tp-yt-paper-button[aria-label*="Accept"]')
            for btn in consent_btns:
                if btn.is_displayed():
                    logging.info(f"Clicking YouTube consent button: {btn.text}")
                    btn.click()
                    time.sleep(3)
                    break
        except Exception:
            pass
        
        # Step 3: Wait for YouTube to load
        wait_for_youtube_load(driver)
        
        # Step 4: Main scraping loop
        all_videos = []
        extraction_count = 0
        
        while True:
            extraction_count += 1
            logging.info(f"=== Extraction #{extraction_count} ===")
            logging.info("Extracting videos from recommended page...")
            
            new_videos = extract_videos(driver)
            
            if len(new_videos) == 0:
                logging.warning("No videos found! Debugging page state...")
                debug_page_state(driver)
                
                # Try scrolling more aggressively
                logging.info("Trying aggressive scroll to load content...")
                for i in range(8):
                    driver.execute_script(f"window.scrollTo(0, {(i + 1) * 800});")
                    time.sleep(2)
                driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(3)
                
                # Retry extraction
                new_videos = extract_videos(driver)
                if len(new_videos) == 0:
                    logging.warning("Still no videos found after scroll retry.")
                    # Save page source for debugging
                    try:
                        with open("debug_page.html", "w", encoding="utf-8") as f:
                            f.write(driver.page_source)
                        logging.info("Saved page source to debug_page.html for inspection")
                    except Exception:
                        pass
            
            logging.info(f"Extracted {len(new_videos)} videos.")
            
            # Add new videos to collection (avoid duplicates)
            existing_urls = {v['url'] for v in all_videos}
            truly_new = [v for v in new_videos if v['url'] not in existing_urls]
            all_videos.extend(truly_new)
            logging.info(f"New unique videos: {len(truly_new)} | Total collected: {len(all_videos)}")
            
            # Save all collected videos
            output_file = "latest_videos.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(all_videos, f, ensure_ascii=False, indent=4)
                
            for v in truly_new[:5]:  # Print top 5 new
                logging.info(f"  Title: {v['title']} | Channel: {v['channel']}")
                
            logging.info("Waiting 3 minutes before next extraction...")
            time.sleep(180)
            
            # Refresh page to get new recommendations
            driver.refresh()
            time.sleep(5)
            wait_for_youtube_load(driver)
            
    except KeyboardInterrupt:
        logging.info("Stopped by user.")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    login_and_scrape()

import json
import logging
import os
import random
import re
import time
import warnings
from datetime import datetime, timedelta, timezone
import requests
from pathlib import Path
from typing import List, Optional, Set
from dotenv import load_dotenv
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Suppress FutureWarning for deprecated google.generativeai package
warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")


# Configure logging with a cleaner format
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'  # Cleaner format without timestamps for main output
)
logger = logging.getLogger(__name__)

# --- Region filtering -------------------------------------------------------
# The pipeline targets a USA/English audience, so non-English (and specifically
# Indian-audience) content is rejected during discovery before anything is
# downloaded. Three independent signals are used, since any one of them alone
# leaks: language tags are often missing, scripts are often transliterated to
# Latin, and keywords alone catch nothing that is written in native script.

# BCP-47 prefixes for languages widely used by Indian-audience channels.
INDIC_LANGUAGE_PREFIXES = (
    "hi",   # Hindi
    "bn",   # Bengali
    "ta",   # Tamil
    "te",   # Telugu
    "mr",   # Marathi
    "gu",   # Gujarati
    "kn",   # Kannada
    "ml",   # Malayalam
    "pa",   # Punjabi
    "ur",   # Urdu
    "or",   # Odia
    "as",   # Assamese
    "ne",   # Nepali
    "si",   # Sinhala
    "sd",   # Sindhi
    "bho",  # Bhojpuri
    "mai",  # Maithili
)

# Unicode blocks for Indic scripts. A title or channel name containing any of
# these is rejected regardless of what the language tag claims.
INDIC_SCRIPT_PATTERN = re.compile(
    "["
    "ऀ-ॿ"  # Devanagari (Hindi, Marathi, Nepali)
    "ঀ-৿"  # Bengali / Assamese
    "਀-੿"  # Gurmukhi (Punjabi)
    "઀-૿"  # Gujarati
    "଀-୿"  # Odia
    "஀-௿"  # Tamil
    "ఀ-౿"  # Telugu
    "ಀ-೿"  # Kannada
    "ഀ-ൿ"  # Malayalam
    "඀-෿"  # Sinhala
    "؀-ۿ"  # Arabic (Urdu)
    "]"
)

# Latin-script markers common in Indian-audience gaming titles and channel
# names. Matched as whole words to avoid false positives ("this" vs "hi").
INDIC_KEYWORDS = (
    "hindi", "indian", "india", "desi", "bhai", "bhaiya", "yaar", "bro ka",
    "tamil", "telugu", "malayalam", "kannada", "bengali", "punjabi", "marathi",
    "gujarati", "urdu", "bhojpuri", "hindustani", "gaming in hindi",
    "hindi gameplay", "hindi gaming", "indian gamer", "indian gaming",
    "hindi commentary", "bharat", "pubg mobile india", "bgmi",
    "free fire india", "gaming india", "namaste", "kaise", "kya", "hai bhai",
)
INDIC_KEYWORD_PATTERN = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(k) for k in INDIC_KEYWORDS) + r")(?![a-z])"
)

# Channel names to always reject, matched case-insensitively as substrings.
# Grow this list as specific channels slip through.
CHANNEL_BLOCKLIST = ()


# --- Content-type filtering -------------------------------------------------
# The account posts pure gameplay clips, so two other categories are rejected:
# videos where the creator appears on camera (facecam / IRL / vlog framing) and
# real-world physical challenge formats (truth or dare, pranks, stunts). Both
# are detected from title/description text, which videos.list already returns.

# Creator-on-camera markers. "facecam" and "irl" are the strongest signals;
# reveal/vlog/podcast framing reliably means a person on screen.
FACECAM_KEYWORDS = (
    "facecam", "face cam", "face reveal", "facereveal", "face-cam",
    "showing my face", "my face", "on cam", "on camera", "webcam",
    "irl", "in real life", "vlog", "vlogging", "day in my life",
    "podcast", "sitting down with", "talking head", "storytime",
    "story time", "q&a", "qanda", "ask me anything", "meet the",
    "behind the scenes", "unboxing", "room tour", "setup tour",
    "reacting on camera", "caught on camera", "my reaction",
    "first time seeing", "watch me", "me and my friend",
)

# Real-world physical challenge / stunt / prank formats.
#
# Deliberately excluded, despite sounding like challenge formats: "survive",
# "escape room", "parkour", "backflip", "punishment", "bet", "winner gets",
# "trampoline", "stunt". Every one of those is standard Roblox/horror GAMEPLAY
# vocabulary ("Survive the Killer", "Escape Room Roblox", "parkour speedrun"),
# and including them rejected ~85% of legitimate gameplay titles in testing.
# Only phrases that imply a real-world, physical act belong here.
PHYSICAL_CHALLENGE_KEYWORDS = (
    "truth or dare", "truth and dare", "dare challenge", "dared me",
    "i dare you", "spin the wheel",
    "prank", "pranked", "pranking", "prank war",
    "24 hour", "24 hours", "24hr", "48 hour", "48 hours",
    "last to leave", "last one to", "last to stop",
    "eating challenge", "food challenge", "spicy challenge",
    "hot sauce", "ice bath", "cold plunge",
    "try not to laugh", "try not to flinch", "try not to move",
    "slap challenge", "push up challenge", "workout challenge",
    "fitness challenge", "extreme challenge", "physical challenge",
    "real life challenge", "in real life challenge", "irl challenge",
    "squid game in real life", "hide and seek in real life",
    "shock collar", "electric shock", "taser",
)

FACECAM_PATTERN = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(k) for k in FACECAM_KEYWORDS) + r")(?![a-z])"
)
PHYSICAL_CHALLENGE_PATTERN = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(k) for k in PHYSICAL_CHALLENGE_KEYWORDS) + r")(?![a-z])"
)


class ContentGenerator:
    """Main class for finding and downloading YouTube content."""

    def __init__(self, search_queries: List[str] = ["roblox gaming"], video_count: int = 5):
        """
        Initialize the Content Generator.
        """
        self.search_queries = search_queries
        self.video_count = video_count
        
        # Statistics for summary
        self.stats = {
            "queries_processed": 0,
            "videos_found": 0,
            "videos_new": 0,
            "downloads_success": 0,
            "downloads_failed": 0,
            "instagram_uploads": 0,
            "errors": []
        }
        
        # Load environment variables
        env_file = Path("cred/.env")
        if env_file.exists():
            load_dotenv(env_file)
            self._log_step("SYSTEM", "Loaded environment variables from cred/.env")
        else:
            logger.warning("⚠️ cred/.env file not found")

        # Google Sheet tracking configuration (single source of truth)
        self.sheet_id = os.getenv("SHEET_ID")
        if self.sheet_id:
            self.sheet_id = self.sheet_id.strip().strip('"').strip("'")
        self.sheet_tab = os.getenv("SHEET_TAB", "Sheet1").strip().strip('"').strip("'")
        self.sheet_header = ["url", "title", "description", "channel", "downloaded_at"]
        self.sheets_service = self._init_sheets_service()

        # API configurations...
        self.instagram_access_token = os.getenv("INSTAGRAM_FACEBOOK_ACCESS_TOKEN")
        self.instagram_business_account_id = os.getenv("INSTAGRAM_USERID")
        # Graph API version used for container creation, resumable upload and publish
        self.graph_api_version = "v23.0"
        self.youtube_data_api_key = os.getenv("YOUTUBE_DATA_V3_API")
        if self.youtube_data_api_key:
            self.youtube_data_api_key = self.youtube_data_api_key.strip().strip('"').strip("'")
        else:
            logger.warning("⚠️ YOUTUBE_DATA_V3_API not found")

        # Apify token for the LurkAPI YouTube downloader actor
        self.lurkapi_token = os.getenv("LURKAPI")
        if self.lurkapi_token:
            self.lurkapi_token = self.lurkapi_token.strip().strip('"').strip("'")
        else:
            logger.warning("⚠️ LURKAPI not found")
        self.lurkapi_endpoint = (
            "https://api.apify.com/v2/actors/lurkapi~youtube-video-downloader/"
            "run-sync-get-dataset-items"
        )
        # Actor is billed per minute of video by quality tier (best/1080p/720p/480p/360p)
        self.lurkapi_quality = "1080p"

        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel('gemini-2.5-flash')
        else:
            logger.warning("⚠️ GEMINI_API_KEY not found")
            self.gemini_model = None

    def _log_header(self, title: str):
        print(f"\n{'='*70}")
        print(f" {title.center(68)} ")
        print(f"{'='*70}\n")

    def _log_step(self, stage: str, message: str, icon: str = "🔹"):
        print(f"{icon} [{stage.upper():<10}] {message}")

    def _log_substep(self, message: str, icon: str = "  ↳"):
        print(f"    {icon} {message}")


    
    def _init_sheets_service(self):
        """Build the Google Sheets service using the service account credentials.

        The Google Sheet is the single source of truth for tracking, so this
        raises if it cannot be initialized rather than silently continuing.
        """
        raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not raw or not self.sheet_id:
            raise RuntimeError(
                "Google Sheet tracking not configured: set GOOGLE_SERVICE_ACCOUNT_JSON and SHEET_ID in cred/.env"
            )

        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._log_step("SYSTEM", "Connected to Google Sheet for tracking")
        return service.spreadsheets()

    def _load_downloaded_videos(self) -> Set[str]:
        """Load the set of already-uploaded video URLs from the Google Sheet.

        The sheet is the only source of truth. If it cannot be read, this raises
        so the run aborts instead of risking duplicate uploads.
        """
        result = self.sheets_service.values().get(
            spreadsheetId=self.sheet_id,
            range=f"{self.sheet_tab}!A:A",
        ).execute()
        rows = result.get("values", [])
        urls = set()
        for i, row in enumerate(rows):
            if not row:
                continue
            value = row[0].strip()
            if i == 0 and value.lower() == "url":
                continue  # skip header row
            if value:
                urls.add(value)
        return urls

    def _save_downloaded_video(self, url: str, title: str = "", description: str = "", channel: str = "") -> None:
        """Append an uploaded video as a new row in the Google Sheet."""
        downloaded_at = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.sheets_service.values().append(
                spreadsheetId=self.sheet_id,
                range=f"{self.sheet_tab}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [[url, title, description, channel, downloaded_at]]},
            ).execute()
            self._log_substep("Recorded video in tracking sheet", "🧾")
        except Exception as e:
            logger.error(f"❌ Error writing to tracking sheet: {e}")

    def _generate_caption_with_gemini(self, title: str, description: str, channel: str = "") -> str:
        """Generate Instagram caption using Gemini AI with provided title, description, and channel name."""
        try:
            # Debug logging (internal)
            # logger.debug(f"Caption generation for: {title[:30]}...")
            
            if not self.gemini_model:
                self._log_substep("Gemini model not available, using default caption", "⚠️")
                if title and description:
                    return f"{title}\n\n{description}\n\n#gaming #roblox #videogames #trending"
                else:
                    return "#gaming #roblox #videogames #trending #horror #gamingcommunity #gamers #gaminglife"
            
            # Handle empty title and description by using only hashtags
            if not title and not description:
                logger.info("🏷️ Title and description empty, generating dynamic hashtags-only caption")
                
                # Generate dynamic trending hashtags based on Instagram gaming trends
                base_hashtags = [
                    "#gaming", "#roblox", "#videogames", "#trending", 
                    "#gamingcommunity", "#gamers", "#gaminglife", "#gamingclips",
                    "#epicgaming", "#gamingmemes", "#gamingvideos"
                ]
                
                # Add dynamic hashtags based on Instagram gaming trends
                dynamic_hashtags = [
                    "#instagramgaming", "#gaming2024", "#gamergoals", "#gamingsetup",
                    "#gamingislife", "#gamingonpc", "#gamingcontent", "#gamingdaily",
                    "#instagramreels", "#gamingontrending", "#gamingposts", "#gamingviral"
                ]
                
                # Combine and shuffle for variety
                all_hashtags = base_hashtags + dynamic_hashtags
                random.shuffle(all_hashtags)
                
                # Return 12-15 random hashtags
                selected_hashtags = all_hashtags[:12]
                return " ".join(selected_hashtags)
            
            prompt = f"""
            Create a HIGHLY RELEVANT Instagram caption in ENGLISH ONLY for a gaming video targeting USA audience with the following details:
            
            Title: {title}
            Description: {description}
            
            CRITICAL REQUIREMENTS:
            - DEEPLY ANALYZE the title and description to understand the ACTUAL video content
            - Create caption that DIRECTLY relates to what happens in the video
            - Reference SPECIFIC game names, characters, or gameplay moments from the content
            - Make it feel like you actually watched and understood this specific video
            - NEVER use generic phrases like "Hit the link in bio", "Join Discord", "free prizes", "protect your assets"
            - AVOID any promotional language that doesn't relate to the actual video content
            - NO generic CTAs about external links, Discord, or promotional offers
            - Focus ONLY on the video content itself - what happens, what's funny/scary/cool about it
            - Use American English slang and expressions where appropriate
            - Make it catchy and engaging for American gaming audience
            - Generate 10-15 relevant trending hashtags popular in USA gaming community
            - Research and include current trending hashtags for gaming/roblox that are popular in USA
            - Create hashtags that are likely to trend with USA audience
            - Keep caption SHORT and under 1500 characters (Instagram prefers shorter captions)
            - Use emojis where appropriate that match video's mood and content
            - Make it feel authentic and engaging for USA gamers
            - Start with a DIRECT HOOK that grabs attention immediately
            - Use "I see this video" perspective instead of "I play this game"
            - Include elements that create curiosity but keep it concise
            - Use phrases like "You need to see this", "Check this out", "The ending is wild" but ONLY if relevant to video
            - Extract KEY MOMENTS from the video content and reference them specifically
            - Use American gaming terminology and references
            - Target USA gaming culture and trends
            - If the video is about horror games, focus on scary moments, jumpscares, or tension
            - If the video is about Roblox, focus on specific game modes, funny moments, or gameplay
            - Make the caption SPECIFIC to this video, not generic gaming content
            - Create urgency without being too long
            
            FORMAT: Return only the caption text, no extra explanations.
            """
            
            response = self.gemini_model.generate_content(prompt)
            caption = response.text.strip()
            
            self._log_substep(f"AI Caption generated successfully", "✨")
            return caption
            
        except Exception as e:
            logger.error(f"Error generating caption with Gemini: {e}")
            return f"{title}\n\n{description}\n\n#gaming #roblox #videogames #trending #gamingcontent"
    
    def _upload_to_instagram(self, title: str, description: str = "", video_bytes: bytes = b"", channel: str = "") -> bool:
        """Upload a Reel to Instagram by uploading the raw video bytes directly.

        Uses Meta's resumable upload flow, so no external hosting is needed:
          1. Create a REELS container with upload_type=resumable.
          2. POST the raw video bytes to the rupload.facebook.com host.
          3. Poll container status, then publish.
        """
        try:
            if not self.instagram_access_token or not self.instagram_business_account_id:
                logger.error("Instagram API credentials not found in cred/.env file. Please ensure INSTAGRAM_FACEBOOK_ACCESS_TOKEN and INSTAGRAM_USERID are set.")
                return False

            if not video_bytes:
                logger.error("No video bytes provided for Instagram upload")
                return False

            version = self.graph_api_version

            # Generate caption using Gemini with title, description, and channel
            caption = self._generate_caption_with_gemini(title, description, channel)

            # Add credit line and disclaimer
            if channel:
                credit_line = f"\n\nCredit: {channel}"
                disclaimer = """
⚠️ Disclaimer
This video is created for entertainment and educational purposes only.
All rights belong to the original content owners.
I do not claim ownership of any clips used in this video. The content has been edited and transformed with added cuts, effects, and short edits to provide a unique viewing experience.
This video follows the principles of fair use under applicable copyright laws.
If you are the rightful owner of any content used and have any concerns, please contact me. I will promptly remove or credit the content as requested."""
                caption = caption + credit_line + disclaimer

            logger.info(f"Attempting to upload to Instagram ({len(video_bytes)} bytes): {title[:50]}")

            # Step 1: Create media container with resumable upload (no hosted URL)
            container_url = f"https://graph.facebook.com/{version}/{self.instagram_business_account_id}/media"

            container_data = {
                'media_type': 'REELS',
                'upload_type': 'resumable',
                'caption': caption,
                'access_token': self.instagram_access_token
            }

            container_response = requests.post(container_url, data=container_data)
            container_result = container_response.json()

            if 'id' not in container_result:
                logger.error(f"Failed to create media container: {container_result}")
                return False

            container_id = container_result['id']
            logger.info(f"Media container created with ID: {container_id}")

            # Step 2: Upload the raw video bytes to the rupload host
            rupload_url = f"https://rupload.facebook.com/ig-api-upload/{version}/{container_id}"
            rupload_headers = {
                'Authorization': f'OAuth {self.instagram_access_token}',
                'offset': '0',
                'file_size': str(len(video_bytes)),
            }
            logger.info("Uploading video bytes to rupload.facebook.com...")
            rupload_response = requests.post(rupload_url, headers=rupload_headers, data=video_bytes)
            try:
                rupload_result = rupload_response.json()
            except ValueError:
                rupload_result = {"raw": rupload_response.text}

            if not rupload_result.get('success') and rupload_response.status_code != 200:
                logger.error(f"Resumable upload failed ({rupload_response.status_code}): {rupload_result}")
                return False
            logger.info(f"Video bytes uploaded: {rupload_result}")

            # Step 3: Check media status
            status_url = f"https://graph.facebook.com/{version}/{container_id}"
            status_params = {
                'fields': 'status_code,status',
                'access_token': self.instagram_access_token
            }

            # Instagram Reels processing can take a few minutes for larger clips.
            # Poll for up to ~5 minutes (30 attempts x 10s) before giving up.
            max_attempts = 30
            poll_interval = 10  # seconds between checks
            time.sleep(5)  # give Instagram a moment to start processing
            for attempt in range(max_attempts):
                status_response = requests.get(status_url, params=status_params)
                status_result = status_response.json()

                logger.info(f"Media status check {attempt + 1}/{max_attempts}: {status_result}")

                if status_result.get('status_code') == 'FINISHED':
                    break
                elif status_result.get('status_code') == 'ERROR':
                    logger.error(f"Media processing failed: {status_result}")
                    return False

                time.sleep(poll_interval)
            else:
                logger.error(
                    f"Media processing timed out after {max_attempts * poll_interval}s "
                    f"(container {container_id} still IN_PROGRESS)"
                )
                return False
            
            # Step 3: Publish media (retry a few times; IG occasionally reports
            # "media not ready" for a few seconds even after status is FINISHED)
            publish_url = f"https://graph.facebook.com/{version}/{self.instagram_business_account_id}/media_publish"
            publish_data = {
                'creation_id': container_id,
                'access_token': self.instagram_access_token
            }

            publish_attempts = 5
            for attempt in range(publish_attempts):
                publish_response = requests.post(publish_url, data=publish_data)
                publish_result = publish_response.json()

                if 'id' in publish_result:
                    logger.info(f"Successfully uploaded to Instagram! Media ID: {publish_result['id']}")
                    return True

                logger.warning(
                    f"Publish attempt {attempt + 1}/{publish_attempts} not ready: {publish_result}"
                )
                if attempt < publish_attempts - 1:
                    time.sleep(10)

            logger.error(f"Failed to publish to Instagram after {publish_attempts} attempts")
            return False
                
        except Exception as e:
            logger.error(f"Error uploading to Instagram: {e}")
            return False
    
    def find_video_urls(self) -> List[str]:
        """
        Find YouTube video URLs using the YouTube Data API v3 search.list endpoint.

        Keeps paging ("scrolling") through search results until video_count new
        qualifying videos are found for each query, instead of giving up after a
        single page. When one page yields nothing that passes the <=50s length
        gate plus the language/content/duplicate filters, it follows
        nextPageToken to the next page and tries again.

        If a query's result pages run out, it retries the same query under
        different orderings/recency windows (see _search_variants), so a query is
        only abandoned once YouTube genuinely has nothing left to offer.

        Quota note: each search.list page costs 100 units (10,000/day default),
        so paging is capped by MAX_PAGES_PER_VARIANT across the variants to keep
        a barren query from draining the whole day's quota.

        Returns:
            List of YouTube video URLs
        """
        MAX_DURATION_SECONDS = 50
        MAX_PAGES_PER_VARIANT = 20  # 20 pages x 100 units = 2000 quota units per variant
        all_video_urls = []

        if not self.youtube_data_api_key:
            logger.error("❌ YouTube Data API key (YOUTUBE_DATA_V3_API) not found in environment. Please add it to your cred/.env file.")
            self.stats["errors"].append("YouTube Data API key missing")
            return []

        search_api_url = "https://www.googleapis.com/youtube/v3/search"

        for query in self.search_queries:
            logger.info(f"🔍 Searching YouTube for: {query}")

            downloaded_videos = self._load_downloaded_videos()
            found_for_query: List[str] = []
            seen_ids: Set[str] = set()
            pages_fetched = 0

            for variant_idx, variant in enumerate(self._search_variants(), 1):
                if len(found_for_query) >= self.video_count:
                    break

                if variant_idx > 1:
                    self._log_substep(
                        f"Still short ({len(found_for_query)}/{self.video_count}) - "
                        f"retrying '{query}' ordered by {variant['label']}",
                        "🔄",
                    )

                page_token = None
                for page in range(1, MAX_PAGES_PER_VARIANT + 1):
                    if len(found_for_query) >= self.video_count:
                        break

                    params = {
                        "part": "snippet",
                        "q": query,
                        "type": "video",
                        "videoDuration": "short",  # <4 min, closest proxy for Shorts
                        # Always ask for the API maximum: the <=50s length gate
                        # plus the language/content filters reject most results,
                        # so a small page usually yields zero keepers.
                        "maxResults": 50,
                        "order": variant["order"],
                        # Bias results toward the USA/English audience this
                        # account targets. These are ranking hints, not hard
                        # filters, so _rejection_reason still enforces the gate.
                        "regionCode": "US",
                        "relevanceLanguage": "en",
                        "key": self.youtube_data_api_key,
                    }
                    if variant.get("publishedAfter"):
                        params["publishedAfter"] = variant["publishedAfter"]
                    if page_token:
                        params["pageToken"] = page_token

                    data = self._search_page(
                        search_api_url, params, query, page, variant["label"]
                    )
                    if data is None:
                        # Hard failure (quota/network) already logged - stop
                        # paging this variant rather than hammering the API.
                        break

                    pages_fetched += 1
                    items = data.get("items", [])
                    page_token = data.get("nextPageToken")

                    # Extract video IDs, deduplicating across every page seen so
                    # far for this query.
                    unique_ids = []
                    for item in items:
                        vid = item.get("id", {}).get("videoId")
                        if vid and vid not in seen_ids:
                            seen_ids.add(vid)
                            unique_ids.append(vid)

                    if not unique_ids:
                        self._log_substep(
                            f"Page {page} ({variant['label']}): no new video IDs", "⚠️"
                        )
                        if not page_token:
                            break
                        continue

                    # Fetch duration + snippet in one call, then apply both the
                    # length limit and the English/USA-only gate before anything
                    # reaches the download queue.
                    details = self._get_video_details(unique_ids)

                    short_ids = [
                        vid for vid in unique_ids
                        if 0 < details.get(vid, {}).get("duration", 0) <= MAX_DURATION_SECONDS
                    ]
                    dropped = len(unique_ids) - len(short_ids)
                    if dropped:
                        self._log_substep(f"Filtered out {dropped} videos longer than {MAX_DURATION_SECONDS}s", "✂️")

                    kept_ids = []
                    region_rejected = 0
                    content_rejected = 0
                    for vid in short_ids:
                        info = details[vid]

                        reason = self._rejection_reason(
                            info["language"], info["title"], info["channel"]
                        )
                        if reason:
                            region_rejected += 1
                            self._log_substep(
                                f"Rejected '{info['title'][:40]}' - {reason}", "🌏"
                            )
                            continue

                        reason = self._content_rejection_reason(
                            info["title"], info["description"], info["channel"]
                        )
                        if reason:
                            content_rejected += 1
                            self._log_substep(
                                f"Rejected '{info['title'][:40]}' - {reason}", "🙅"
                            )
                            continue

                        kept_ids.append(vid)

                    if region_rejected:
                        self._log_substep(
                            f"Filtered out {region_rejected} non-English/regional videos", "🌏"
                        )
                    if content_rejected:
                        self._log_substep(
                            f"Filtered out {content_rejected} facecam/challenge videos", "🙅"
                        )

                    # Build URLs, skipping anything already queued this run or
                    # already downloaded previously.
                    for vid in kept_ids:
                        url = f"https://www.youtube.com/shorts/{vid}"
                        if url in all_video_urls or url in found_for_query:
                            continue
                        if url in downloaded_videos:
                            continue
                        found_for_query.append(url)
                        if len(found_for_query) >= self.video_count:
                            break

                    self._log_substep(
                        f"Page {page} ({variant['label']}): "
                        f"{len(found_for_query)}/{self.video_count} qualifying videos so far",
                        "📄",
                    )

                    if len(found_for_query) >= self.video_count:
                        break

                    if not page_token:
                        self._log_substep(
                            f"No more pages for '{query}' ({variant['label']}) after page {page}",
                            "🔚",
                        )
                        break

                    # Small pause so rapid paging does not trip rate limiting.
                    time.sleep(1)

            if found_for_query:
                self._log_substep(
                    f"Query '{query}': Found {len(found_for_query)} new videos "
                    f"across {pages_fetched} search pages",
                    "✅",
                )
                if len(found_for_query) < self.video_count:
                    self._log_substep(
                        f"Exhausted all search pages/variants for '{query}' - "
                        f"got {len(found_for_query)} of {self.video_count} requested",
                        "⚠️",
                    )
                all_video_urls.extend(found_for_query)
            else:
                self._log_step(
                    "ERROR",
                    f"No qualifying videos for query '{query}' after {pages_fetched} search pages",
                    "❌",
                )
                self.stats["errors"].append(f"Search error ({query}): no qualifying videos found")

        return all_video_urls

    def _search_variants(self) -> List[dict]:
        """
        Ordered list of search.list parameter variants to try for a single query.

        Paging through 'relevance' eventually runs out of pages; re-running the
        same query ordered by views/date/rating (and within recency windows)
        surfaces a different slice of YouTube's index, so a query is only
        abandoned after every angle comes back dry.
        """
        now = datetime.now(timezone.utc)
        return [
            {"label": "relevance", "order": "relevance"},
            {"label": "most viewed", "order": "viewCount"},
            {"label": "newest first", "order": "date"},
            {"label": "top rated", "order": "rating"},
            {
                "label": "past 30 days",
                "order": "date",
                "publishedAfter": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            {
                "label": "past year, most viewed",
                "order": "viewCount",
                "publishedAfter": (now - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        ]

    def _search_page(
        self, url: str, params: dict, query: str, page: int, variant_label: str
    ) -> Optional[dict]:
        """
        Fetch a single search.list page, retrying transient failures.

        Returns the parsed JSON body, or None if the page could not be fetched
        after all attempts (the caller then stops paging that variant).
        """
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                self._log_substep(
                    f"Fetching page {page} ({variant_label}) via YouTube Data API "
                    f"(attempt {attempt}/{max_attempts})...",
                    "🌐",
                )
                response = requests.get(url, params=params, timeout=30)
                data = response.json()

                if response.status_code == 200:
                    return data

                self._log_substep(
                    f"YouTube Data API returned status code {response.status_code} "
                    f"on attempt {attempt}: {data}",
                    "⚠️",
                )
                # Quota exhaustion / permission errors will not fix themselves
                # on retry, so bail out immediately instead of waiting twice.
                if response.status_code == 403:
                    self._log_step(
                        "ERROR",
                        f"YouTube Data API refused the request (403) for '{query}' - quota or key issue",
                        "❌",
                    )
                    self.stats["errors"].append(
                        f"YouTube Data API error ({query}): status code 403 (quota/permission)"
                    )
                    return None
                if attempt == max_attempts:
                    self.stats["errors"].append(
                        f"YouTube Data API error ({query}): status code {response.status_code}"
                    )
                    return None
            except Exception as e:
                self._log_substep(f"Attempt {attempt} failed: {e}", "⚠️")
                if attempt == max_attempts:
                    self.stats["errors"].append(f"Search error ({query}): {e}")
                    return None

            sleep_time = 15 if attempt == 1 else 30
            self._log_substep(f"Waiting {sleep_time} seconds before retry...", "⏳")
            time.sleep(sleep_time)

        return None

    def _get_video_details(self, video_ids: List[str]) -> dict:
        """
        Fetch duration and snippet details for the given video IDs via videos.list.

        One videos.list call costs 1 quota unit regardless of how many parts are
        requested, and returns up to 50 videos, so a single query's results are
        covered by one request. Requesting `snippet` alongside `contentDetails`
        is therefore free and supplies the language/title/channel fields the
        region filter needs.

        Returns a dict of {video_id: {"duration": int, "language": str,
        "title": str, "channel": str, "description": str}}; IDs that could not
        be resolved are omitted.
        """
        details = {}
        if not video_ids:
            return details

        videos_api_url = "https://www.googleapis.com/youtube/v3/videos"
        # videos.list accepts up to 50 IDs per call
        for start in range(0, len(video_ids), 50):
            batch = video_ids[start:start + 50]
            try:
                params = {
                    "part": "contentDetails,snippet",
                    "id": ",".join(batch),
                    "key": self.youtube_data_api_key,
                }
                response = requests.get(videos_api_url, params=params, timeout=30)
                data = response.json()
                if response.status_code != 200:
                    logger.warning(f"videos.list error while fetching details: {data}")
                    continue
                for item in data.get("items", []):
                    vid = item.get("id")
                    if not vid:
                        continue
                    iso = item.get("contentDetails", {}).get("duration", "")
                    seconds = self._parse_iso8601_duration(iso)
                    if seconds is None:
                        continue
                    snippet = item.get("snippet", {})
                    # defaultAudioLanguage is the spoken language; defaultLanguage
                    # describes the title/description metadata. Prefer the former.
                    language = (
                        snippet.get("defaultAudioLanguage")
                        or snippet.get("defaultLanguage")
                        or ""
                    )
                    details[vid] = {
                        "duration": seconds,
                        "language": language,
                        "title": snippet.get("title", ""),
                        "channel": snippet.get("channelTitle", ""),
                        "description": snippet.get("description", ""),
                    }
            except Exception as e:
                logger.warning(f"Error fetching video details: {e}")
        return details

    @staticmethod
    def _parse_iso8601_duration(iso: str) -> Optional[int]:
        """Convert an ISO 8601 duration (e.g. 'PT1M5S', 'PT45S') to seconds."""
        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
        if not match:
            return None
        hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
        return hours * 3600 + minutes * 60 + seconds

    @staticmethod
    def _rejection_reason(language: str, title: str, channel: str) -> Optional[str]:
        """Return why this video fails the English/USA-only gate, or None if it passes.

        Strict mode: a video is kept only when its language tag is English. An
        empty language tag is tolerated (a large share of Shorts omit it), but
        only if neither the title nor the channel name shows an Indic script or
        keyword. Any non-English tag is rejected outright.
        """
        text = f"{title} {channel}"

        for blocked in CHANNEL_BLOCKLIST:
            if blocked.lower() in channel.lower():
                return f"blocklisted channel ({channel})"

        if INDIC_SCRIPT_PATTERN.search(text):
            return "Indic script in title/channel"

        if INDIC_KEYWORD_PATTERN.search(text.lower()):
            return "Indic keyword in title/channel"

        tag = language.lower()
        if tag:
            primary = tag.split("-")[0]
            if primary in INDIC_LANGUAGE_PREFIXES:
                return f"Indic language tag ({language})"
            if primary != "en":
                return f"non-English language tag ({language})"

        return None

    @staticmethod
    def _content_rejection_reason(title: str, description: str, channel: str) -> Optional[str]:
        """Return why this video fails the gameplay-only gate, or None if it passes.

        Rejects two formats that don't fit a pure gameplay feed: creator-on-camera
        videos (facecam, IRL, vlog) and real-world physical challenges (truth or
        dare, pranks, stunts).

        The description is searched as well as the title, because these formats
        are frequently only disclosed in the description ("facecam at 10k likes").
        Only the first 500 characters are used — beyond that, descriptions are
        mostly boilerplate links, sponsor blurbs and channel promos that trigger
        false positives.
        """
        text = f"{title} {channel} {description[:500]}".lower()

        match = FACECAM_PATTERN.search(text)
        if match:
            return f"creator-on-camera content ('{match.group(0)}')"

        match = PHYSICAL_CHALLENGE_PATTERN.search(text)
        if match:
            return f"physical challenge content ('{match.group(0)}')"

        return None

    def _extract_video_id(self, url: str) -> Optional[str]:
        """Extract the 11-character YouTube video ID from a watch or shorts URL."""
        match = re.search(r"(?:/shorts/|/watch\?v=|youtu\.be/|[?&]v=)([a-zA-Z0-9_-]{11})", url)
        return match.group(1) if match else None

    def _get_video_metadata(self, url: str) -> tuple[str, str, str]:
        """
        Fetch video title, description, and channel name using the YouTube Data API v3.

        Args:
            url: YouTube video URL

        Returns:
            Tuple of (title, description, channel)
        """
        try:
            if not self.youtube_data_api_key:
                logger.warning("YouTube Data API key not available; cannot fetch metadata")
                return "", "", ""

            video_id = self._extract_video_id(url)
            if not video_id:
                logger.warning(f"Could not extract video ID from {url}")
                return "", "", ""

            api_url = "https://www.googleapis.com/youtube/v3/videos"
            params = {
                "part": "snippet",
                "id": video_id,
                "key": self.youtube_data_api_key
            }

            response = requests.get(api_url, params=params, timeout=30)
            data = response.json()

            if response.status_code != 200:
                logger.warning(f"YouTube Data API error for {url}: {data}")
                return "", "", ""

            items = data.get("items", [])
            if not items:
                logger.warning(f"No metadata found for {url} (video may be private/deleted)")
                return "", "", ""

            snippet = items[0].get("snippet", {})
            title = snippet.get("title", "")
            description = snippet.get("description", "")
            channel = snippet.get("channelTitle", "")
            return title, description, channel

        except Exception as e:
            logger.warning(f"Error fetching metadata for {url}: {e}")
            return "", "", ""
    
    def _download_video_bytes(self, url: str) -> Optional[bytes]:
        """Download a YouTube video via the LurkAPI Apify actor and return its bytes.

        The actor (lurkapi~youtube-video-downloader) is run synchronously: the
        request blocks until the run finishes and returns its dataset items.
        Each item carries a videoFileUrl pointing at the file in Apify's
        key-value store, which is then fetched into memory. The download itself
        happens on Apify's side, so YouTube's bot-blocking of datacenter IPs
        (e.g. GitHub Actions runners) no longer applies here.
        """
        if not self.lurkapi_token:
            self._log_substep("LURKAPI token not found in cred/.env", "❌")
            return None

        # The token goes in the Authorization header rather than the ?token=
        # query string so it can't leak into logged URLs or error messages.
        headers = {"Authorization": f"Bearer {self.lurkapi_token}"}
        payload = {
            "videoUrls": [url],
            "quality": self.lurkapi_quality,
            "format": "mp4",
            "includeSubtitles": False,
            "maxVideosPerUrl": 1,
            "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": []},
        }

        try:
            # Apify caps synchronous runs at 300s, so allow a little longer.
            response = requests.post(
                self.lurkapi_endpoint, headers=headers, json=payload, timeout=330
            )
            if response.status_code not in (200, 201):
                self._log_substep(
                    f"LurkAPI run failed ({response.status_code}): {response.text[:300]}", "❌"
                )
                return None

            items = response.json()
            if not items:
                self._log_substep("LurkAPI returned no items", "❌")
                return None

            item = items[0]
            file_url = item.get("videoFileUrl")
            if item.get("error") or not file_url:
                self._log_substep(
                    f"LurkAPI could not download the video: "
                    f"{item.get('error') or item.get('status') or 'no videoFileUrl'}",
                    "❌",
                )
                return None

            # Only send the Apify token to Apify's own hosts.
            file_headers = headers if file_url.startswith("https://api.apify.com/") else {}
            file_response = requests.get(file_url, headers=file_headers, timeout=300)
            if file_response.status_code != 200:
                self._log_substep(
                    f"Fetching video file failed ({file_response.status_code})", "❌"
                )
                return None

            video_bytes = file_response.content
            if not video_bytes:
                self._log_substep("Downloaded file was empty (0 bytes)", "❌")
                return None

            return video_bytes

        except Exception as e:
            self._log_substep(f"LurkAPI download failed: {e}", "❌")
            return None

    def download_video(self, url: str) -> bool:
        """
        Download a single video into memory via the LurkAPI Apify actor and publish it to Instagram Reels.
        
        Args:
            url: YouTube video URL
            
        Returns:
            True if download and upload successful, False otherwise
        """
        try:
            self._log_step("PROCESS", f"Starting: {url}", "🎬")
            
            # Fetch metadata first
            title, description, channel = self._get_video_metadata(url)
            if not title:
                self._log_substep("Could not fetch video metadata. Skipping.", "⚠️")
                return False
            
            # Skip reaction videos
            if title:
                title_lower = title.lower()
                reaction_keywords = ['reaction', 'reacts to', 'reacting to', 'reaction video']
                
                if any(keyword in title_lower for keyword in reaction_keywords):
                    self._log_substep(f"Skipping reaction video: {title[:40]}...", "⏭️")
                    return False
            
            self._log_substep(f"Video identified: {title[:50]}...", "📝")
            self._log_substep(f"Channel: {channel}", "📺")

            self._log_substep("Downloading video from YouTube...", "⬇️")
            video_bytes = self._download_video_bytes(url)

            if video_bytes:
                self.stats["downloads_success"] += 1
                self._log_substep("Download completed successfully", "✅")

                # Upload the raw bytes directly to Instagram Reels (resumable upload)
                self._log_substep("Publishing to Instagram Reels...", "📸")
                if self._upload_to_instagram(title, description, video_bytes, channel):
                    self.stats["instagram_uploads"] += 1
                    self._log_substep("PUBLISHED TO INSTAGRAM!", "🚀")
                    self._save_downloaded_video(url, title, description, channel)
                else:
                    self._log_substep("Failed to publish to Instagram", "❌")

                return True
            else:
                self.stats["downloads_failed"] += 1
                self._log_substep("Download failed", "❌")
                return False
                
        except Exception as e:
            self.stats["errors"].append(f"Processing error ({url}): {e}")
            self._log_step("ERROR", f"Processing {url}: {e}", "❌")
            return False

    
    def _print_summary(self):
        """Print a clean summary of the entire run."""
        self._log_header("WORKFLOW SUMMARY")
        
        print(f"{'Metric':<25} | {'Value':<10}")
        print(f"{'-'*25}-|-{'-'*10}")
        print(f"{'Queries Processed':<25} | {self.stats['queries_processed']:<10}")
        print(f"{'Total Videos Found':<25} | {self.stats['videos_found']:<10}")
        print(f"{'New Videos to Process':<25} | {self.stats['videos_new']:<10}")
        print(f"{'Successful Downloads':<25} | {self.stats['downloads_success']:<10}")
        print(f"{'Failed Downloads':<25} | {self.stats['downloads_failed']:<10}")
        print(f"{'Instagram Uploads':<25} | {self.stats['instagram_uploads']:<10}")
        
        if self.stats["errors"]:
            print(f"\n⚠️ ERRORS ENCOUNTERED ({len(self.stats['errors'])}):")
            for error in self.stats["errors"][:5]:
                print(f"  • {error}")
            if len(self.stats["errors"]) > 5:
                print(f"  ... and {len(self.stats['errors']) - 5} more.")
        
        print(f"\n{'='*70}\n")

    def process_videos(self) -> None:
        """Main method to find and download videos."""
        self._log_header("STARTING CONTENT GENERATOR")
        
        try:
            all_new_videos = []
            all_found_videos = []
            
            # Discovery Phase
            self._log_header("DISCOVERY PHASE")
            for query in self.search_queries:
                self.stats["queries_processed"] += 1
                self._log_step("SEARCH", f"Query: {query}", "🔍")
                
                original_queries = self.search_queries
                self.search_queries = [query]
                video_urls = self.find_video_urls()
                self.search_queries = original_queries
                
                if not video_urls:
                    self._log_substep(f"No results for: {query}", "⚠️")
                    continue
                
                self.stats["videos_found"] += len(video_urls)
                all_found_videos.extend(video_urls)
                
                downloaded_videos = self._load_downloaded_videos()
                new_videos_for_query = [url for url in video_urls if url not in downloaded_videos][:self.video_count]
                
                self.stats["videos_new"] += len(new_videos_for_query)
                all_new_videos.extend(new_videos_for_query)
                
                self._log_substep(f"Found {len(video_urls)} videos ({len(new_videos_for_query)} to process)")
                for i, url in enumerate(video_urls[:10], 1):
                    status = "[QUEUE]" if url in new_videos_for_query else "[SKIP]"
                    self._log_substep(f"{i}. {url} {status}")
                if len(video_urls) > 10:
                    self._log_substep(f"... and {len(video_urls)-10} more")
            
            if not all_new_videos:
                self._log_header("NO NEW CONTENT TO PROCESS")
                return

            # Processing Phase
            self._log_header(f"PROCESSING PHASE ({len(all_new_videos)} VIDEOS)")
            
            for idx, url in enumerate(all_new_videos, 1):
                print(f"\n[Video {idx}/{len(all_new_videos)}]")
                self.download_video(url)
                time.sleep(2)

            # Final Report
            self._print_summary()
            
        except Exception as e:
            self._log_step("FATAL", f"Critical Error: {e}", "💥")
            self._print_summary()
            raise


def main():
    """Main entry point."""
    try:
        # Configuration - can be moved to config file
        config = {
            # "search_queries": ["Horror Game Gameplay", "Roblox Adventure",]
            "search_queries": [
                "poppy playtime animation"],
            "video_count": 1
        }
        
        generator = ContentGenerator(**config)
        generator.process_videos()
        
    except KeyboardInterrupt:
        logger.info("⏹️ Process interrupted by user")
    except Exception as e:
        logger.error(f"💥 Application Error: {e}")
        raise


if __name__ == "__main__":
    main()
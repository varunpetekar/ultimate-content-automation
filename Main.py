import io
import json
import logging
import os
import random
import re
import subprocess
import time
import warnings
import requests
from pathlib import Path
from typing import List, Optional, Set
from dotenv import load_dotenv
import google.generativeai as genai
import cloudinary
import cloudinary.uploader
import cloudinary.api

# Suppress FutureWarning for deprecated google.generativeai package
warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")


# Configure logging with a cleaner format
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'  # Cleaner format without timestamps for main output
)
logger = logging.getLogger(__name__)

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
        
        # API configurations...
        self.instagram_access_token = os.getenv("INSTAGRAM_FACEBOOK_ACCESS_TOKEN")
        self.instagram_business_account_id = os.getenv("INSTAGRAM_USERID")
        self.cloudinary_base_url = "https://res.cloudinary.com/ddszy4br6/video/upload/v1776316134/Reels/"
        
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel('gemini-2.5-flash')
        else:
            logger.warning("⚠️ GEMINI_API_KEY not found")
            self.gemini_model = None
        
        self.cloudinary_api_key = os.getenv("CLOUDINARY_API_KEY")
        self.cloudinary_api_secret = os.getenv("CLOUDINARY_API_SECRET")
        self.cloudinary_cloud_name = "ddszy4br6"
        
        cloudinary.config(
            cloud_name=self.cloudinary_cloud_name,
            api_key=self.cloudinary_api_key,
            api_secret=self.cloudinary_api_secret
        )

    def _log_header(self, title: str):
        print(f"\n{'='*70}")
        print(f" {title.center(68)} ")
        print(f"{'='*70}\n")

    def _log_step(self, stage: str, message: str, icon: str = "*"):
        # Force ASCII icon to avoid UnicodeEncodeError on Windows consoles
        ascii_icon = ''.join(c for c in icon if ord(c) < 128) or '*'
        print(f"{ascii_icon} [{stage.upper():<10}] {message}")

    def _log_substep(self, message: str, icon: str = "->"):
        ascii_icon = ''.join(c for c in icon if ord(c) < 128) or '->'
        print(f"    {ascii_icon} {message}")


    
    def _upload_to_cloudinary(self, video_source, title: str) -> Optional[str]:
        """Upload video to Cloudinary (accepts file path or bytes stream) and return the URL."""
        try:
            if not self.cloudinary_api_key or not self.cloudinary_api_secret:
                logger.error("Cloudinary API credentials not found. Please set CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET in .env file")
                return None
            
            # Sanitize title for Cloudinary
            safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()
            safe_title = safe_title.replace(' ', '_').upper()
            
            # Upload to Cloudinary using SDK (supports bytes stream or file path)
            upload_result = cloudinary.uploader.upload(
                video_source,
                resource_type='video',
                folder='Reels',
                public_id=safe_title
            )
            
            if 'secure_url' in upload_result:
                cloudinary_url = upload_result['secure_url']
                self._log_substep(f"Uploaded to Cloudinary: {cloudinary_url}", "✅")
                return cloudinary_url
            else:
                self._log_substep(f"Cloudinary upload failed: {upload_result}", "❌")
                return None
                
        except Exception as e:
            self._log_substep(f"Error uploading to Cloudinary: {e}", "❌")
            return None
    
    def _delete_from_cloudinary(self, public_id: str) -> bool:
        """Delete video from Cloudinary to free up storage."""
        try:
            if not self.cloudinary_api_key or not self.cloudinary_api_secret:
                logger.error("Cloudinary API credentials not found")
                return False
            
            # Delete from Cloudinary using SDK
            delete_result = cloudinary.uploader.destroy(
                public_id,
                resource_type='video'
            )
            
            if delete_result.get('result') == 'ok':
                logger.info(f"🗑️ Successfully deleted from Cloudinary: {public_id}")
                return True
            else:
                logger.error(f"❌ Failed to delete from Cloudinary: {delete_result}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error deleting from Cloudinary: {e}")
            return False
    
    def _clear_all_cloudinary_videos(self) -> bool:
        """Delete all videos and the Reels folder from Cloudinary to ensure complete cleanup."""
        try:
            if not self.cloudinary_api_key or not self.cloudinary_api_secret:
                logger.error("Cloudinary API credentials not found")
                return False
            
            logger.info(" invalidate: true to remove CDN cached copies")
            folder_path = "Reels"
            
            # Try to delete everything inside the folder first
            try:
                result = cloudinary.api.delete_resources_by_prefix(folder_path + "/", invalidate=True)
                logger.info(f" Deleted assets: {result}")
            except Exception as assets_err:
                logger.error(f" Bulk delete of assets failed: {assets_err}")
            
            # Try to delete the folder itself anyway
            try:
                cloudinary.api.delete_folder(folder_path)
                logger.info(f"🗑️ Successfully deleted Cloudinary folder: {folder_path}")
            except Exception as folder_err:
                logger.warning(f"⚠️ Could not delete Cloudinary folder '{folder_path}': {folder_err}")
            
            return True
            
        except Exception as e:
            logger.error(f" Error during Cloudinary cleanup: {e}")
            return False
            return False
    
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
    
    def _upload_to_instagram(self, title: str, description: str = "", video_url: str = "", channel: str = "") -> bool:
        """Upload video to Instagram using Facebook Graph API."""
        try:
            if not self.instagram_access_token or not self.instagram_business_account_id:
                logger.error("Instagram API credentials not found in cred/.env file. Please ensure INSTAGRAM_FACEBOOK_ACCESS_TOKEN and INSTAGRAM_USERID are set.")
                return False
            
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
            
            logger.info(f"Attempting to upload to Instagram: {video_url}")
            
            # Step 1: Create media container
            container_url = f"https://graph.facebook.com/v18.0/{self.instagram_business_account_id}/media"
            
            container_data = {
                'media_type': 'REELS',
                'video_url': video_url,
                'caption': caption,
                'access_token': self.instagram_access_token
            }
            
            container_response = requests.post(container_url, json=container_data)
            container_result = container_response.json()
            
            if 'id' not in container_result:
                logger.error(f"Failed to create media container: {container_result}")
                return False
            
            container_id = container_result['id']
            logger.info(f"Media container created with ID: {container_id}")
            
            # Step 2: Check media status
            status_url = f"https://graph.facebook.com/v18.0/{container_id}"
            status_params = {
                'fields': 'status_code,status',
                'access_token': self.instagram_access_token
            }
            
            max_attempts = 10
            for attempt in range(max_attempts):
                status_response = requests.get(status_url, params=status_params)
                status_result = status_response.json()
                
                logger.info(f"Media status check {attempt + 1}: {status_result}")
                
                if status_result.get('status_code') == 'FINISHED':
                    break
                elif status_result.get('status_code') == 'ERROR':
                    logger.error(f"Media processing failed: {status_result}")
                    return False
                
                time.sleep(5)  # Wait 5 seconds between checks
            else:
                logger.error("Media processing timed out")
                return False
            
            # Step 3: Publish media
            publish_url = f"https://graph.facebook.com/v18.0/{self.instagram_business_account_id}/media_publish"
            publish_data = {
                'creation_id': container_id,
                'access_token': self.instagram_access_token
            }
            
            publish_response = requests.post(publish_url, json=publish_data)
            publish_result = publish_response.json()
            
            if 'id' in publish_result:
                logger.info(f"Successfully uploaded to Instagram! Media ID: {publish_result['id']}")
                return True
            else:
                logger.error(f"Failed to publish to Instagram: {publish_result}")
                return False
                
        except Exception as e:
            logger.error(f"Error uploading to Instagram: {e}")
            return False
    def find_video_urls(self) -> List[str]:
        """Find video URLs from YouTube search results."""
        all_video_urls = []
        scrapninja_key = os.getenv("SCRAPNINJA_KEY") or os.getenv("SCRAPNINJA_API_KEY") or os.getenv("RAPIDAPI_KEY")
        if scrapninja_key:
            scrapninja_key = scrapninja_key.strip('"').strip("'")
        else:
            logger.error("❌ ScrapNinja API Key (SCRAPNINJA_KEY) not found in environment. Please add it to your cred/.env file.")
            self.stats["errors"].append("ScrapNinja API key missing")
            return []
        
        scrapninja_url = os.getenv("SCRAPNINJA_URL") or "https://scrapeninja.p.rapidapi.com/v2/scrape-js"
        scrapninja_url = scrapninja_url.strip('"').strip("'")
        
        # Dynamically extract netloc host header from the URL
        from urllib.parse import urlparse
        parsed_url = urlparse(scrapninja_url)
        scrapninja_host = parsed_url.netloc or "scrapeninja.p.rapidapi.com"
        
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        ]
        
        for query in self.search_queries:
            logger.info(f"🔍 Searching YouTube for: {query}")
            formatted_query = query.replace(' ', '+')
            search_url = f'https://www.youtube.com/results?search_query={formatted_query}&sp=CAMSBAgCEAk%253D'
            
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    self._log_substep(f"Fetching search results via ScrapNinja (attempt {attempt}/{max_attempts})...", "🌐")
                    
                    selected_ua = random.choice(user_agents)
                    payload = {
                        "url": search_url,
                        "wait": 3000,  # wait for JS to load
                        "retryNum": 2,  # trigger rotating proxy retries inside ScrapNinja (max allowed is 2)
                        "statusNotExpected": [403, 429, 503],  # status codes that trigger proxy rotation retries
                        "headers": [
                            f"User-Agent: {selected_ua}"
                        ]
                    }
                    
                    headers = {
                        "x-rapidapi-key": scrapninja_key,
                        "x-rapidapi-host": scrapninja_host,
                        "Content-Type": "application/json"
                    }
                    
                    # Larger timeout to allow ScrapNinja's internal retries to complete
                    response = requests.post(scrapninja_url, json=payload, headers=headers, timeout=90)
                    
                    if response.status_code == 200:
                        response_json = response.json()
                        html = response_json.get("body", "")
                        
                        # Extract video IDs from both shorts and watch formats to maximize compatibility
                        shorts_ids = re.findall(r"\/shorts\/([a-zA-Z0-9_-]{11})", html)
                        watch_ids = re.findall(r"\/watch\?v=([a-zA-Z0-9_-]{11})", html)
                        
                        # Deduplicate while preserving order of appearance
                        seen_ids = set()
                        unique_ids = []
                        for video_id in shorts_ids + watch_ids:
                            if video_id not in seen_ids:
                                seen_ids.add(video_id)
                                unique_ids.append(video_id)
                        
                        if not unique_ids:
                            self._log_substep(f"ScrapNinja returned 200 but found 0 video IDs on attempt {attempt}", "⚠️")
                            if attempt == max_attempts:
                                self._log_step("ERROR", f"No videos found for query '{query}' after all attempts", "❌")
                                self.stats["errors"].append(f"Search error ({query}): no videos found")
                            else:
                                sleep_time = 15 if attempt == 1 else 30
                                self._log_substep(f"Waiting {sleep_time} seconds before reset and retry...", "⏳")
                                time.sleep(sleep_time)
                            continue
                        
                        # Construct full URLs
                        video_urls = [f"https://www.youtube.com/shorts/{vid}" for vid in unique_ids]
                        
                        # Filter duplicates against other found videos in this run
                        filtered_urls = []
                        for url in video_urls:
                            if url not in all_video_urls:
                                filtered_urls.append(url)
                                if len(filtered_urls) >= self.video_count:
                                    break
                        
                        self._log_substep(f"Query '{query}': Found {len(filtered_urls)} videos", "✅")
                        all_video_urls.extend(filtered_urls)
                        break  # Success - exit the retry loop
                    else:
                        self._log_substep(f"ScrapNinja returned status code {response.status_code} on attempt {attempt}", "⚠️")
                        if attempt == max_attempts:
                            self._log_step("ERROR", f"ScrapNinja returned status code {response.status_code} after all attempts", "❌")
                            self.stats["errors"].append(f"ScrapNinja error ({query}): status code {response.status_code}")
                        else:
                            sleep_time = 15 if attempt == 1 else 30
                            self._log_substep(f"Waiting {sleep_time} seconds before reset and retry...", "⏳")
                            time.sleep(sleep_time)
                            
                except Exception as e:
                    self._log_substep(f"Attempt {attempt} failed: {e}", "⚠️")
                    if attempt == max_attempts:
                        self._log_step("ERROR", f"Failed finding videos for '{query}' after all attempts: {e}", "❌")
                        self.stats["errors"].append(f"Search error ({query}): {e}")
                    else:
                        sleep_time = 15 if attempt == 1 else 30
                        self._log_substep(f"Waiting {sleep_time} seconds before reset and retry...", "⏳")
                        time.sleep(sleep_time)
        
        return all_video_urls
    
    def _get_video_metadata(self, url: str) -> tuple[str, str, str]:
        """
        Fetch video title, description, and channel name using yt-dlp.
        
        Args:
            url: YouTube video URL
            
        Returns:
            Tuple of (title, description, channel)
        """
        try:
            cmd = [
                "yt-dlp",
                "--dump-json",
                url
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60
            )

            # If yt-dlp returned JSON in stdout even on error, parse it.
            if result.stdout:
                try:
                    data = json.loads(result.stdout)
                    title = data.get('title', '')
                    description = data.get('description', '')
                    channel = data.get('channel', data.get('uploader', ''))
                    return title, description, channel
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse yt-dlp JSON for {url}: {result.stdout[:200]}")

            # If no valid JSON, fall back to a simpler command without extra flags.
            if result.returncode != 0:
                logger.warning(f"yt-dlp returned error code {result.returncode} for {url}, attempting fallback.")
                fallback_cmd = [
                    "yt-dlp",
                    "--dump-json",
                    "--extractor-args", "youtube:player_client=android",
                    url
                ]
                fallback_res = subprocess.run(
                    fallback_cmd,
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                if fallback_res.stdout:
                    try:
                        data = json.loads(fallback_res.stdout)
                        title = data.get('title', '')
                        description = data.get('description', '')
                        channel = data.get('channel', data.get('uploader', ''))
                        return title, description, channel
                    except json.JSONDecodeError:
                        logger.warning(f"Fallback JSON parse failed for {url}: {fallback_res.stdout[:200]}")

            logger.warning(f"Could not fetch metadata for {url}")
            return "", "", ""
        except Exception as e:
            logger.warning(f"Error fetching metadata for {url}: {e}")
            return "", "", ""
    
    def download_video(self, url: str) -> bool:
        """
        Download a single video directly to memory using yt-dlp and upload to Cloudinary.
        
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
            
            # Sanitize title for filename / Cloudinary public_id
            safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()
            
            self._log_substep("Streaming video from YouTube into memory...", "⬇️")
            cmd = [
                "yt-dlp",
                "-o", "-",
                "-f", "mp4",
                "--no-warnings",
                "--extractor-args", "youtube:player_client=android",
                url
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            
            if result.returncode == 0:
                self.stats["downloads_success"] += 1
                self._log_substep("Download completed successfully", "✅")
                
                video_bytes = result.stdout
                if not video_bytes:
                    self._log_substep("Captured 0 bytes from video stream", "❌")
                    return False
                
                video_stream = io.BytesIO(video_bytes)
                
                # Upload to Cloudinary
                self._log_substep("Uploading stream to Cloudinary for temporary hosting...", "☁️")
                cloudinary_url = self._upload_to_cloudinary(video_stream, title)
                
                if cloudinary_url:
                    # Upload to Instagram
                    self._log_substep("Publishing to Instagram Reels...", "📸")
                    if self._upload_to_instagram(title, description, cloudinary_url, channel):
                        self.stats["instagram_uploads"] += 1
                        self._log_substep("PUBLISHED TO INSTAGRAM!", "🚀")
                        
                        # Cleanup Cloudinary
                        self._delete_from_cloudinary(f"Reels/{safe_title.replace(' ', '_').upper()}")
                    else:
                        self._log_substep("Failed to publish to Instagram", "❌")
                else:
                    self._log_substep("Cloudinary upload failed", "❌")
                
                return True
            else:
                self.stats["downloads_failed"] += 1
                self._log_substep(f"Download failed: {result.stderr.decode('utf-8', errors='ignore')[:100]}...", "❌")
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
        try:
            # Setup
            self._log_step("INIT", "Preparing Cloudinary environment...")

            # Determine if Cloudinary credentials are present
            self._skip_cloudinary = not (self.cloudinary_api_key and self.cloudinary_api_secret)
            # Setup Cloudinary only if credentials exist
            if not self._skip_cloudinary:
                try:
                    cloudinary.api.create_folder("Reels")
                    self._log_substep("Cloudinary 'Reels' folder ready", "✅")
                except Exception:
                    self._log_substep("Reels folder already exists or verified", "ℹ️")
            else:
                self._log_substep("Cloudinary credentials missing – skipping folder setup", "⚠️")

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
                
                new_videos_for_query = video_urls[:self.video_count]
                
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
            
            # Cleanup Phase
            self._log_header("CLEANUP PHASE")
            self._log_step("CLEANUP", "Clearing temporary Cloudinary storage...", "🧹")
            if self._clear_all_cloudinary_videos():
                self._log_substep("Storage cleared successfully", "✅")
            
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
    "search_queries": ["game", "animation"],
    "video_count": 2
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
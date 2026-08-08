#!/usr/bin/env python3
"""
Kids Video Generator - Daily Automated Pipeline Orchestrator

This script runs the complete daily video generation pipeline:
1. Picks a topic based on day of week
2. Generates script using Claude
3. Creates video via HeyGen (or 3D animation pipeline)
4. Uploads to YouTube Shorts
5. Posts to Instagram Reels
6. Sends Telegram notification

Usage:
    python run_daily_pipeline.py           # Run once manually
    python run_daily_pipeline.py --daemon  # Run as daemon with scheduler
    python run_daily_pipeline.py --test    # Test mode (no uploads)
"""

import os
import sys
import json
import time
import random
import logging
import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import yaml

# Third-party imports (install via: pip install pyyaml requests apscheduler)
try:
    import requests
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError:
    print("Installing dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml", "requests", "apscheduler"])
    import requests
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger


# ═══════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class VideoScript:
    title: str
    description: str
    voice_script: str
    heygen_prompt: str
    tags: List[str]
    instagram_caption: str
    moral: str
    topic: str
    content_type: str


@dataclass
class PipelineResult:
    success: bool
    video_url: Optional[str] = None
    youtube_video_id: Optional[str] = None
    instagram_post_id: Optional[str] = None
    error: Optional[str] = None
    script: Optional[VideoScript] = None


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG LOADER
# ═══════════════════════════════════════════════════════════════════════════

class Config:
    def __init__(self, config_path: str = "config.yaml"):
        self.path = Path(config_path)
        self.data = {}
        
        # 1. Load from config.yaml if exists
        if self.path.exists():
            with open(self.path, 'r', encoding='utf-8') as f:
                self.data = yaml.safe_load(f) or {}
        
        # 2. Override with environment variables (for GitHub Actions)
        self._load_from_env()
        
        self._setup_logging()
    
    def _load_from_env(self):
        """Load API keys from environment variables"""
        env_mapping = {
            'anthropic': 'ANTHROPIC_API_KEY',
            'heygen': 'HEYGEN_API_KEY',
            'youtube_client_id': 'YOUTUBE_CLIENT_ID',
            'youtube_client_secret': 'YOUTUBE_CLIENT_SECRET',
            'youtube_refresh_token': 'YOUTUBE_REFRESH_TOKEN',
            'instagram_access_token': 'INSTAGRAM_ACCESS_TOKEN',
            'instagram_account_id': 'INSTAGRAM_ACCOUNT_ID',
            'telegram_bot_token': 'TELEGRAM_BOT_TOKEN',
            'telegram_chat_id': 'TELEGRAM_CHAT_ID',
            'kie_api_token': 'KIE_API_TOKEN',
            'imgbb_api_key': 'IMGBB_API_KEY',
            'youtube_data_api_key': 'YOUTUBE_DATA_API_KEY',
        }
        
        if 'api_keys' not in self.data:
            self.data['api_keys'] = {}
        
        for key, env_var in env_mapping.items():
            env_val = os.environ.get(env_var)
            if env_val:
                self.data['api_keys'][key] = env_val
        
        # Also load pipeline settings from env if present
        if 'pipeline' not in self.data:
            self.data['pipeline'] = {}
        
        # Default pipeline settings (can be overridden by config.yaml)
        defaults = {
            'mode': 'heygen',
            'schedule_cron': '0 2 * * *',
            'max_duration_seconds': 60,
            'target_age_group': '4-8',
            'language': 'hi',
            'content_schedule': {
                0: "nursery_rhyme",
                1: "educational",
                2: "fun_facts",
                3: "story",
                4: "poem",
                5: "lullaby",
                6: "cartoon_story"
            },
            'topics': {
                'nursery_rhyme': ["चंदा मामा दूर के", "मछली जल की रानी है", "आ री आ निंदिया", "लकड़ी की काठी"],
                'educational': ["पानी का चक्र", "गिनती 1 से 10", "रंगों की दुनिया", "फलों के नाम"],
                'fun_facts': ["जानवरों के रोचक तथ्य", "अंतरिक्ष की अनोखी बातें", "पेड़ पौधों का जादू"],
                'story': ["प्यासा कौआ", "खरगोश और कछुआ", "शेर और चूहा", "चालाक लोमड़ी"],
                'poem': ["बारिश आई रिमझिम", "तितली रानी", "सूरज निकला", "फूलों की बगिया"],
                'lullaby': ["सो जा राजकुमार", "चंदा मामा आओ", "निंदिया आ जा", "आलू कचालू"],
                'cartoon_story': ["जंगल के दोस्त", "परियों की कहानी", "जादुई पेंसिल", "उड़न खटोला"]
            }
        }
        
        for key, val in defaults.items():
            if key not in self.data['pipeline']:
                self.data['pipeline'][key] = val
        
        # Heygen defaults
        if 'heygen' not in self.data:
            self.data['heygen'] = {}
        self.data['heygen'].setdefault('avatar_id', 'auto')
        self.data['heygen'].setdefault('voice_id', 'hi-IN-Standard-A')
        self.data['heygen'].setdefault('video_width', 1080)
        self.data['heygen'].setdefault('video_height', 1920)
        
        # Upload defaults
        if 'upload' not in self.data:
            self.data['upload'] = {}
        self.data['upload'].setdefault('youtube', {})
        self.data['upload']['youtube'].setdefault('category_id', '27')
        self.data['upload']['youtube'].setdefault('privacy_status', 'public')
        self.data['upload']['youtube'].setdefault('made_for_kids', True)
        self.data['upload']['youtube'].setdefault('default_language', 'hi')
        self.data['upload']['youtube'].setdefault('tags', ['Shorts', 'hindi', 'kids', 'education', 'cartoon', 'बच्चे', 'HindiKids'])
        self.data['upload'].setdefault('instagram', {})
        self.data['upload']['instagram'].setdefault('share_to_feed', True)
        self.data['upload']['instagram'].setdefault('tags', ['Reels', 'InstagramReels', 'HindiKids', 'KidsContent', 'बच्चे'])
        
        # Logging defaults
        if 'logging' not in self.data:
            self.data['logging'] = {}
        self.data['logging'].setdefault('level', 'INFO')
        self.data['logging'].setdefault('file', 'logs/pipeline.log')
        self.data['logging'].setdefault('max_size_mb', 10)
        self.data['logging'].setdefault('backup_count', 5)
    
    def _setup_logging(self):
        # Check for LOG_LEVEL env var (GitHub Actions sets this)
        log_level = os.environ.get('LOG_LEVEL', self.data.get('logging', {}).get('level', 'INFO'))
        log_file = self.data.get('logging', {}).get('file', 'logs/pipeline.log')
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        
        logging.basicConfig(
            level=getattr(logging, log_level),
            format='%(asctime)s | %(levelname)-8s | %(message)s',
            handlers=[
                logging.FileHandler(log_file, encoding='utf-8'),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)
    
    def get(self, key: str, default=None):
        keys = key.split('.')
        val = self.data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val
    
    @property
    def api_keys(self) -> Dict[str, str]:
        return self.data.get('api_keys', {})
    
    @property
    def pipeline_mode(self) -> str:
        return self.data.get('pipeline', {}).get('mode', 'heygen')
    
    @property
    def schedule_cron(self) -> str:
        return self.data.get('pipeline', {}).get('schedule_cron', '0 2 * * *')
    
    @property
    def content_schedule(self) -> Dict[int, str]:
        return self.data.get('pipeline', {}).get('content_schedule', {})
    
    @property
    def topics(self) -> Dict[str, List[str]]:
        return self.data.get('pipeline', {}).get('topics', {})


# ═══════════════════════════════════════════════════════════════════════════
# TOPIC SELECTOR
# ═══════════════════════════════════════════════════════════════════════════

class TopicSelector:
    def __init__(self, config: Config):
        self.config = config
        self.logger = config.logger
        self.youtube_api_key = config.api_keys.get('youtube_data_api_key')
    
    def pick_today_topic(self) -> Dict[str, Any]:
        """Pick topic - tries trending from YouTube, falls back to static"""
        now = datetime.now()
        day_of_week = now.weekday()
        day_map = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}
        n8n_day = day_map[day_of_week]
        content_type = self.config.content_schedule.get(n8n_day, "story")
        
        # Try to get trending topic from YouTube
        trending_topic = self._fetch_trending_topic(content_type)
        
        if trending_topic:
            topic = trending_topic
            self.logger.info(f"Using trending topic: {topic}")
        else:
            topics_list = self.config.topics.get(content_type, ["बच्चों की कहानी"])
            topic = random.choice(topics_list)
            self.logger.info(f"Using static topic: {topic}")
        
        return {
            "topic": topic,
            "content_type": content_type,
            "date": now.strftime("%Y-%m-%d"),
            "day_of_week": n8n_day
        }
    
    def _fetch_trending_topic(self, content_type: str) -> Optional[str]:
        """Fetch trending Hindi kids topics from YouTube"""
        if not self.youtube_api_key:
            return None
        
        try:
            # Search queries based on content type
            search_queries = {
                "story": "hindi kids story कहानी cartoon",
                "nursery_rhyme": "hindi nursery rhyme बाल गीत",
                "educational": "hindi kids educational शिक्षा",
                "fun_facts": "hindi fun facts रोचक तथ्य",
                "poem": "hindi poem कविता बच्चों",
                "lullaby": "hindi lullaby लोरी",
                "cartoon_story": "hindi cartoon story कार्टून कहानी"
            }
            
            query = search_queries.get(content_type, "hindi kids video")
            
            url = "https://www.googleapis.com/youtube/v3/search"
            params = {
                "part": "snippet",
                "q": query,
                "regionCode": "IN",
                "relevanceLanguage": "hi",
                "type": "video",
                "order": "viewCount",
                "maxResults": 10,
                "key": self.youtube_api_key
            }
            
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            
            items = data.get("items", [])
            if not items:
                return None
            
            # Extract clean titles, filter for kid-appropriate
            titles = []
            for item in items:
                title = item["snippet"]["title"]
                title = title.replace("&", "&").replace("@\\w+", "").strip()
                # Filter: length, kid keywords
                if len(title) > 5 and any(kw in title.lower() for kw in 
                    ["कहानी", "story", "बच्चे", "kids", "cartoon", "राइम", "rhymes", "गीत", "song"]):
                    titles.append(title)
            
            if titles:
                return random.choice(titles[:5])  # Pick from top 5
            
        except Exception as e:
            self.logger.warning(f"Trending fetch failed: {e}")
        
        return None


# ═══════════════════════════════════════════════════════════════════════════
# CLAUDE SCRIPT GENERATOR
# ═══════════════════════════════════════════════════════════════════════════

class ScriptGenerator:
    def __init__(self, config: Config):
        self.config = config
        self.logger = config.logger
        self.api_key = config.api_keys.get('anthropic')
        self.api_url = "https://api.anthropic.com/v1/messages"
    
    def generate(self, topic: str, content_type: str) -> VideoScript:
        """Generate video script using Claude"""
        self.logger.info(f"Generating script for: {topic} ({content_type})")
        
        prompt = self._build_prompt(topic, content_type)
        
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        
        payload = {
            "model": "claude-3-5-sonnet-20240620",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}]
        }
        
        try:
            self.logger.debug(f"Anthropic API key (first 10): {self.api_key[:10] if self.api_key else 'None'}...")
            self.logger.debug(f"Request payload: {json.dumps(payload)[:200]}")
            resp = requests.post(self.api_url, headers=headers, json=payload, timeout=60)
            self.logger.debug(f"Response status: {resp.status_code}")
            self.logger.debug(f"Response headers: {dict(resp.headers)}")
            self.logger.debug(f"Response text: {resp.text[:500]}")
            resp.raise_for_status()
            result = resp.json()
            
            raw_text = result['content'][0]['text']
            script_data = self._parse_response(raw_text)
            
            # Validate and enrich
            script = VideoScript(
                title=script_data.get('title', f'{topic} #Shorts'),
                description=script_data.get('description', ''),
                voice_script=script_data.get('voiceScript', ''),
                heygen_prompt=script_data.get('heygenPrompt', f'Vertical 9:16 kids cartoon about {topic}. Hindi voiceover.'),
                tags=script_data.get('tags', ['Shorts', 'hindi', 'kids', 'education']),
                instagram_caption=script_data.get('instagramCaption', ''),
                moral=script_data.get('moral', ''),
                topic=topic,
                content_type=content_type
            )
            
            self.logger.info(f"Script generated: {script.title[:50]}...")
            return script
            
        except Exception as e:
            self.logger.error(f"Script generation failed: {e}")
            raise
    
    def _build_prompt(self, topic: str, content_type: str) -> str:
        return f"""Create a 60-second Hindi kids video script about: {topic}. Content type: {content_type}. Target age: 4-8 years.

IMPORTANT: This is for YouTube Shorts and Instagram Reels - vertical 9:16 format, max 60 seconds.

Rules:
- Return ONLY valid JSON, no markdown, no code blocks, no newlines inside string values
- Use simple double quotes only
- In voiceScript and heygenPrompt do NOT use double quotes inside the text, use single quotes instead

Format:
{{
  "compliance": {{"status": "safe", "notes": "kid friendly"}},
  "title": "Hindi title under 60 chars with #Shorts",
  "description": "2 sentence Hindi description with hashtags",
  "voiceScript": "complete Hindi script under 120 words, fun and engaging",
  "heygenPrompt": "Vertical 9:16 format kids cartoon video. Cute cartoon characters, bright colorful background, cheerful mood for children under 10. Hindi voiceover. Max 60 seconds.",
  "tags": ["Shorts", "hindi", "kids", "education", "cartoon", "बच्चे", "HindiKids"],
  "instagramCaption": "Hindi caption with emojis and 5 hashtags including #Reels",
  "moral": "one line Hindi moral"
}}"""
    
    def _parse_response(self, raw: str) -> Dict:
        """Parse and clean JSON response from Claude"""
        cleaned = raw.replace('```json', '').replace('```', '').strip()
        
        # Find JSON boundaries
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start != -1 and end != -1:
            cleaned = cleaned[start:end+1]
        
        # Clean control characters
        cleaned = ''.join(c for c in cleaned if ord(c) >= 32 or c in '\n\r\t')
        
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            self.logger.warning(f"JSON parse failed, trying fallback: {e}")
            return self._fallback_parse(cleaned)
    
    def _fallback_parse(self, text: str) -> Dict:
        """Fallback regex extraction"""
        import re
        def extract(field: str) -> str:
            pattern = rf'"{field}"\s*:\s*"([^"]*)"'
            match = re.search(pattern, text)
            return match.group(1) if match else ''
        
        return {
            'title': extract('title'),
            'description': extract('description'),
            'voiceScript': extract('voiceScript'),
            'heygenPrompt': extract('heygenPrompt'),
            'moral': extract('moral'),
            'instagramCaption': extract('instagramCaption'),
            'tags': ['Shorts', 'hindi', 'kids', 'education']
        }


# ═══════════════════════════════════════════════════════════════════════════
# HEYGEN VIDEO GENERATOR
# ═══════════════════════════════════════════════════════════════════════════

class HeyGenGenerator:
    def __init__(self, config: Config):
        self.config = config
        self.logger = config.logger
        self.api_key = config.api_keys.get('heygen')
        self.base_url = "https://api.heygen.com/v1"
    
    def generate(self, script: VideoScript) -> str:
        """Generate video via HeyGen, return video URL"""
        self.logger.info("Submitting video generation to HeyGen...")
        
        # Step 1: Create video
        create_url = f"{self.base_url}/video_agent/generate"
        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json"
        }
        
        prompt = f"{script.heygen_prompt} Speak in Hindi. Hindi script: {script.voice_script}"
        
        payload = {"prompt": prompt}
        
        resp = requests.post(create_url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        
        video_id = result.get('data', {}).get('video_id')
        if not video_id:
            raise Exception(f"HeyGen create failed: {result}")
        
        self.logger.info(f"Video submitted: {video_id}")
        
        # Step 2: Poll for completion
        return self._wait_for_completion(video_id)
    
    def _wait_for_completion(self, video_id: str, max_wait: int = 300) -> str:
        """Poll HeyGen until video is ready"""
        status_url = f"{self.base_url}/video_status.get"
        headers = {"X-Api-Key": self.api_key}
        
        start = time.time()
        while time.time() - start < max_wait:
            resp = requests.get(
                status_url,
                headers=headers,
                params={"video_id": video_id},
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json().get('data', {})
            status = data.get('status', '')
            
            self.logger.info(f"HeyGen status: {status}")
            
            if status == 'completed':
                video_url = data.get('video_url')
                if video_url:
                    self.logger.info(f"Video ready: {video_url}")
                    return video_url
                raise Exception("Video completed but no URL returned")
            
            elif status == 'failed':
                raise Exception(f"HeyGen generation failed: {data}")
            
            time.sleep(10)  # Poll every 10 seconds
        
        raise Exception(f"Timeout waiting for HeyGen video ({max_wait}s)")


# ═══════════════════════════════════════════════════════════════════════════
# YOUTUBE UPLOADER
# ═══════════════════════════════════════════════════════════════════════════

class YouTubeUploader:
    def __init__(self, config: Config):
        self.config = config
        self.logger = config.logger
        self.client_id = config.api_keys.get('youtube_client_id')
        self.client_secret = config.api_keys.get('youtube_client_secret')
        self.refresh_token = config.api_keys.get('youtube_refresh_token')
        self.access_token = None
        self.token_expiry = 0
    
    def _get_access_token(self) -> str:
        """Get valid access token, refresh if needed"""
        if self.access_token and time.time() < self.token_expiry - 60:
            return self.access_token
        
        self.logger.info("Refreshing YouTube access token...")
        
        token_url = "https://oauth2.googleapis.com/token"
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token"
        }
        
        resp = requests.post(token_url, data=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        self.access_token = data['access_token']
        self.token_expiry = time.time() + data.get('expires_in', 3600)
        
        return self.access_token
    
    def upload(self, video_url: str, script: VideoScript) -> str:
        """Upload video to YouTube Shorts, return video ID"""
        self.logger.info("Uploading to YouTube Shorts...")
        
        access_token = self._get_access_token()
        
        # Download video first
        self.logger.info("Downloading video from HeyGen...")
        video_resp = requests.get(video_url, timeout=120)
        video_resp.raise_for_status()
        
        # Prepare metadata
        title = script.title
        if '#Shorts' not in title:
            title += ' #Shorts'
        if len(title) > 100:
            title = title[:97] + '...'
        
        description = (script.description or '') + '\n\n#Shorts #HindiKids #बच्चोंकीकहानी #KidsLearning #HindiCartoon #बच्चे #cartoon'
        
        tags = script.tags + ['Shorts', 'hindi', 'kids', 'cartoon', 'बच्चे']
        
        # Upload via YouTube Data API v3 (resumable upload)
        # For simplicity, we'll use the direct upload approach
        # Note: For production, use resumable upload for large files
        
        upload_url = "https://www.googleapis.com/upload/youtube/v3/videos"
        params = {
            "part": "snippet,status",
            "uploadType": "multipart"
        }
        headers = {
            "Authorization": f"Bearer {access_token}"
        }
        
        # Create multipart form data
        metadata = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": self.config.get('upload.youtube.category_id', '27'),
                "defaultLanguage": "hi",
                "defaultAudioLanguage": "hi"
            },
            "status": {
                "privacyStatus": self.config.get('upload.youtube.privacy_status', 'public'),
                "selfDeclaredMadeForKids": self.config.get('upload.youtube.made_for_kids', True),
                "notifySubscribers": True
            }
        }
        
        # Save video to temp file
        temp_path = Path(f"/tmp/video_{int(time.time())}.mp4")
        temp_path.write_bytes(video_resp.content)
        
        try:
            with open(temp_path, 'rb') as f:
                files = {
                    'metadata': ('metadata', json.dumps(metadata), 'application/json'),
                    'media': ('video.mp4', f, 'video/mp4')
                }
                resp = requests.post(upload_url, params=params, headers=headers, files=files, timeout=300)
            
            resp.raise_for_status()
            result = resp.json()
            
            video_id = result.get('id')
            self.logger.info(f"YouTube upload successful: {video_id}")
            return video_id
            
        finally:
            temp_path.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
# INSTAGRAM UPLOADER
# ═══════════════════════════════════════════════════════════════════════════

class InstagramUploader:
    def __init__(self, config: Config):
        self.config = config
        self.logger = config.logger
        self.access_token = config.api_keys.get('instagram_access_token')
        self.account_id = config.api_keys.get('instagram_account_id')
        self.base_url = "https://graph.facebook.com/v18.0"
    
    def upload_reel(self, video_url: str, script: VideoScript) -> str:
        """Post video as Instagram Reel, return post ID"""
        self.logger.info("Posting to Instagram Reels...")
        
        caption = script.instagram_caption
        if '#Reels' not in caption:
            caption += ' #Reels #InstagramReels'
        
        url = f"{self.base_url}/{self.account_id}/reels"
        params = {
            "access_token": self.access_token,
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": self.config.get('upload.instagram.share_to_feed', True)
        }
        
        resp = requests.post(url, params=params, timeout=120)
        
        if resp.status_code == 400:
            # Try alternative endpoint
            url = f"{self.base_url}/{self.account_id}/media"
            params["media_type"] = "REELS"
            resp = requests.post(url, params=params, timeout=120)
        
        resp.raise_for_status()
        result = resp.json()
        
        # For reels, need to publish
        if 'id' in result:
            container_id = result['id']
            publish_url = f"{self.base_url}/{self.account_id}/media_publish"
            pub_params = {
                "access_token": self.access_token,
                "creation_id": container_id
            }
            pub_resp = requests.post(publish_url, params=pub_params, timeout=60)
            pub_resp.raise_for_status()
            pub_result = pub_resp.json()
            post_id = pub_result.get('id', container_id)
        else:
            post_id = result.get('id', 'unknown')
        
        self.logger.info(f"Instagram Reel posted: {post_id}")
        return post_id


# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM NOTIFIER
# ═══════════════════════════════════════════════════════════════════════════

class TelegramNotifier:
    def __init__(self, config: Config):
        self.config = config
        self.logger = config.logger
        self.bot_token = config.api_keys.get('telegram_bot_token')
        self.chat_id = config.api_keys.get('telegram_chat_id')
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
    
    def send_success(self, script: VideoScript, youtube_id: str, instagram_id: str):
        """Send success notification"""
        text = (
            f"✅ *वीडियो auto-publish हो गई!*\n\n"
            f"📌 *Topic:* {script.topic}\n"
            f"📝 *Title:* {script.title}\n"
            f"💡 *Moral:* {script.moral}\n\n"
            f"📺 YouTube Shorts: https://youtu.be/{youtube_id}\n"
            f"📱 Instagram Reels: {instagram_id}\n\n"
            f"🎉 आज का काम पूरा!\n"
            f"कल सुबह 8 बजे नई वीडियो बनेगी 🌅"
        )
        self._send(text)
    
    def send_failure(self, error: str, topic: str):
        """Send failure notification"""
        text = (
            f"❌ *वीडियो generation failed!*\n\n"
            f"📌 *Topic:* {topic}\n"
            f"💥 *Error:* {error}\n\n"
            f"Please check logs and retry manually."
        )
        self._send(text)
    
    def _send(self, text: str):
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }
            requests.post(self.api_url, json=payload, timeout=30)
        except Exception as e:
            self.logger.error(f"Telegram notification failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════

class DailyPipeline:
    def __init__(self, config: Config, test_mode: bool = False):
        self.config = config
        self.logger = config.logger
        self.test_mode = test_mode
        
        # Initialize components
        self.topic_selector = TopicSelector(config)
        self.script_generator = ScriptGenerator(config)
        self.heygen = HeyGenGenerator(config)
        self.youtube = YouTubeUploader(config)
        self.instagram = InstagramUploader(config)
        self.telegram = TelegramNotifier(config)
    
    def run(self) -> PipelineResult:
        """Run the complete daily pipeline"""
        self.logger.info("=" * 50)
        self.logger.info("STARTING DAILY PIPELINE")
        self.logger.info("=" * 50)
        
        try:
            # Step 1: Pick topic
            topic_info = self.topic_selector.pick_today_topic()
            topic = topic_info['topic']
            content_type = topic_info['content_type']
            
            # Step 2: Generate script
            script = self.script_generator.generate(topic, content_type)
            
            # Step 3: Generate video
            if self.test_mode:
                self.logger.info("TEST MODE: Skipping video generation")
                video_url = "https://example.com/test_video.mp4"
            else:
                video_url = self.heygen.generate(script)
            
            # Step 4: Upload to YouTube
            if self.test_mode:
                youtube_id = "TEST_VIDEO_ID"
            else:
                youtube_id = self.youtube.upload(video_url, script)
            
            # Step 5: Post to Instagram
            if self.test_mode:
                instagram_id = "TEST_INSTAGRAM_ID"
            else:
                instagram_id = self.instagram.upload_reel(video_url, script)
            
            # Step 6: Notify success
            if not self.test_mode:
                self.telegram.send_success(script, youtube_id, instagram_id)
            
            self.logger.info("=" * 50)
            self.logger.info("PIPELINE COMPLETED SUCCESSFULLY")
            self.logger.info("=" * 50)
            
            return PipelineResult(
                success=True,
                video_url=video_url,
                youtube_video_id=youtube_id,
                instagram_post_id=instagram_id,
                script=script
            )
            
        except Exception as e:
            self.logger.error(f"Pipeline failed: {e}", exc_info=True)
            if not self.test_mode:
                self.telegram.send_failure(str(e), topic_info.get('topic', 'unknown'))
            return PipelineResult(success=False, error=str(e))


# ═══════════════════════════════════════════════════════════════════════════
# SCHEDULER / DAEMON MODE
# ═══════════════════════════════════════════════════════════════════════════

def run_scheduler(config: Config):
    """Run pipeline on schedule"""
    scheduler = BlockingScheduler(timezone='UTC')
    
    cron = config.schedule_cron
    # Parse cron: "0 2 * * *" -> hour=2, minute=0
    parts = cron.split()
    if len(parts) == 5:
        minute, hour = parts[0], parts[1]
        trigger = CronTrigger(hour=hour, minute=minute, timezone='UTC')
    else:
        trigger = CronTrigger.from_crontab(cron, timezone='UTC')
    
    def job():
        pipeline = DailyPipeline(config)
        pipeline.run()
    
    scheduler.add_job(job, trigger, id='daily_pipeline', replace_existing=True)
    
    config.logger.info(f"Scheduler started. Next run at: {scheduler.get_job('daily_pipeline').next_run_time}")
    config.logger.info("Press Ctrl+C to stop")
    
    try:
        scheduler.start()
    except KeyboardInterrupt:
        config.logger.info("Scheduler stopped")
        scheduler.shutdown()


# ═══════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Kids Video Generator - Daily Pipeline")
    parser.add_argument('--config', default='config.yaml', help='Config file path')
    parser.add_argument('--test', action='store_true', help='Test mode (no uploads)')
    parser.add_argument('--daemon', action='store_true', help='Run as scheduled daemon')
    args = parser.parse_args()
    
    # Load config
    try:
        config = Config(args.config)
    except FileNotFoundError:
        print(f"Config file not found: {args.config}")
        print("Copy config.yaml.example to config.yaml and fill in your API keys")
        sys.exit(1)
    
    if args.daemon:
        run_scheduler(config)
    else:
        pipeline = DailyPipeline(config, test_mode=args.test)
        result = pipeline.run()
        
        if result.success:
            print("\n✅ Pipeline completed successfully!")
            print(f"   YouTube: https://youtu.be/{result.youtube_video_id}")
            print(f"   Instagram: {result.instagram_post_id}")
            sys.exit(0)
        else:
            print(f"\n❌ Pipeline failed: {result.error}")
            sys.exit(1)


if __name__ == '__main__':
    main()
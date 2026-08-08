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
        if not self.path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(self.path, 'r', encoding='utf-8') as f:
            self.data = yaml.safe_load(f)
        
        self._setup_logging()
    
    def _setup_logging(self):
        log_cfg = self.data.get('logging', {})
        log_file = log_cfg.get('file', 'logs/pipeline.log')
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        
        logging.basicConfig(
            level=getattr(logging, log_cfg.get('level', 'INFO')),
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
    
    def pick_today_topic(self) -> Dict[str, Any]:
        """Pick topic based on day of week"""
        now = datetime.now()
        day_of_week = now.weekday()  # 0=Monday, 6=Sunday
        
        # Adjust: n8n workflow uses 0=Sunday
        day_map = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}
        n8n_day = day_map[day_of_week]
        
        content_type = self.config.content_schedule.get(n8n_day, "story")
        topics_list = self.config.topics.get(content_type, ["बच्चों की कहानी"])
        topic = random.choice(topics_list)
        
        self.logger.info(f"Day: {now.strftime('%A')} | Type: {content_type} | Topic: {topic}")
        
        return {
            "topic": topic,
            "content_type": content_type,
            "date": now.strftime("%Y-%m-%d"),
            "day_of_week": n8n_day
        }


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
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}]
        }
        
        try:
            resp = requests.post(self.api_url, headers=headers, json=payload, timeout=60)
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
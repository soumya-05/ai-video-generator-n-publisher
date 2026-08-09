"""Original story generation via Claude.

There is no topic list anywhere in this project. Every run invents a brand new
story, seeded by the day's content type, live trend signals, the season, and
the list of everything already published (so nothing ever repeats).

The output schema deliberately mirrors the story-to-animation skills
(characters.json / backgrounds.json / shots.json) so artifacts produced here
can be inspected or hand-edited with those same skills.
"""

import json
import re
from typing import List

import requests

API_URL = "https://api.anthropic.com/v1/messages"

# Hindi narration at a kid-friendly pace is roughly 2.2 words/second.
WORDS_PER_SHOT = 18

SYSTEM_PROMPT = """You are a master storyteller for Indian children aged 4-8, \
writing for a Hindi YouTube channel, and you also direct the 3D animation.

HOW YOU WRITE

Story craft:
- One hero, one want, one obstacle. The child must be able to say what the hero
  wants in five words. Everything in the story pushes toward or against that want.
- The hero must try and FAIL at least once before succeeding. Easy wins are boring.
- The solution comes from something small and specific planted earlier in the
  story - a habit, an object, a friend's quirk. Never from luck or a grown-up
  arriving to fix everything.
- Raise a question in the child's head early and answer it late.
- The moral is what the story already proves. Never bolt on a lesson the events
  did not earn.

Originality:
- Never retell Panchatantra, Aesop or Jataka classics: no thirsty crow, tortoise
  and hare, lion and mouse, greedy dog, boy who cried wolf, ant and grasshopper.
- Avoid the tired frames too: no "magic genie grants wishes", no "sharing is
  caring" party, no "the small one saves everyone because they are small", no
  dream-it-was-all-a-dream ending.
- Take one concrete, slightly odd Indian detail (a lost tiffin lid, a kite stuck
  on a transformer, a cow blocking the school gate, the last jalebi) and build
  outward from it. Specific beats generic every time.

Setting:
- Everyday India, seen at a child's eye level: mohallas, terrace lines of
  drying clothes, mango trees, monsoon puddles, dadi's kitchen, thela vendors,
  railway crossings, festival evenings, Indian animals and birds.

Safety:
- Gentle throughout. No violence, death, real fear, injury, or scary imagery.
  Tension comes from worry and hope, never from threat. Problems are solved by
  cleverness, courage or kindness.
- The video model refuses any shot that reads as a child in physical danger,
  even a happy one, and a refused shot wastes the whole episode. So never put a
  child character near a well, a rooftop edge, deep or moving water, fire, a
  stove, heights, machinery, tools with blades, or a rope tied to anyone. Keep
  children on the ground doing safe things; give risky-looking actions to an
  animal friend or a magical object instead.

Hindi narration:
- Simple spoken Hindi (Devanagari) a 5-year-old understands. Short sentences.
- Written for the ear, not the page: read it aloud in your head and keep the
  rhythm bouncy. Sound words (धड़ाम, छपाक, सरसर, टप-टप) and repetition are good.
- Use character dialogue inside the narration - it is far more alive than
  reporting what happened.
- Talk to the child sometimes ("देखो!", "अब क्या होगा?") but do not overdo it.
- No English words except ones every Indian child already uses.

You always reply with a single valid JSON object and nothing else."""


class StoryGenerationError(RuntimeError):
    pass


class StoryGenerator:
    def __init__(self, config):
        self.config = config
        self.logger = config.logger.getChild("story")
        self.model = config.get("story.model", "claude-opus-4-6")

    def generate(
        self,
        content_type: str,
        video_format: str,
        shot_count: int,
        signals: dict,
        avoid: List[str],
        host,
        friends: List,
    ) -> dict:
        self.logger.info(
            "Generating %s story (%s, %d shots, cast: %s)",
            video_format,
            content_type,
            shot_count,
            ", ".join([host.name] + [f.name for f in friends]),
        )
        prompt = self._build_prompt(
            content_type, video_format, shot_count, signals, avoid, host, friends
        )
        # A 38-shot story is a large JSON object and Claude occasionally drops a
        # field from one shot. Another call costs cents, while letting it through
        # wastes an entire render, so just ask again.
        attempts = self.config.get("story.max_attempts", 3)
        for attempt in range(1, attempts + 1):
            try:
                raw = self._call_claude(prompt)
                story = _parse_json(raw)
                story["content_type"] = content_type
                story["format"] = video_format
                _validate(story, shot_count, host.key)
                break
            except StoryGenerationError as exc:
                if attempt == attempts:
                    raise
                self.logger.warning(
                    "Story attempt %d/%d rejected (%s); regenerating",
                    attempt, attempts, exc,
                )
        self.logger.info(
            "Story: %s (%d shots, %d characters)",
            story.get("title", "?"),
            len(story["shots"]),
            len(story["characters"]),
        )
        # Narration longer than the shot has to be sped up to fit, and past
        # ~1.3x it stops sounding like a storyteller to a small child.
        overlong = [
            (shot["shot_id"], words)
            for shot in story["shots"]
            if (words := len(shot["narration_hi"].split())) > WORDS_PER_SHOT + 4
        ]
        if overlong:
            self.logger.warning(
                "%d shot(s) over the %d-word budget and will be spoken fast: %s",
                len(overlong),
                WORDS_PER_SHOT + 4,
                ", ".join(f"{sid}={n}w" for sid, n in overlong),
            )
        return story

    def _call_claude(self, prompt: str) -> str:
        api_key = self.config.require_key("anthropic", "ANTHROPIC_API_KEY")
        resp = requests.post(
            API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                # A 38-shot story with a full veo_prompt per shot runs long.
                "max_tokens": 32000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=300,
        )
        if resp.status_code >= 400:
            raise StoryGenerationError(
                f"Claude API {resp.status_code}: {resp.text[:400]}"
            )
        return resp.json()["content"][0]["text"]

    def _build_prompt(
        self,
        content_type: str,
        video_format: str,
        shot_count: int,
        signals: dict,
        avoid: List[str],
        host,
        friends: List,
    ) -> str:
        duration = shot_count * 8
        keywords = ", ".join(signals.get("trending_keywords") or []) or "(none available)"
        avoid_block = "\n".join(f"- {line}" for line in avoid) or "- (nothing yet)"
        aspect = self.config.get(f"{video_format}.aspect_ratio", "16:9")

        cast_block = "\n\n".join(
            f"  cast_key: {member.key}\n"
            f"  Name: {member.name} ({'HOST' if member.is_host else 'recurring friend'})\n"
            f"  Catchphrase: {member.catchphrase}\n"
            f"  Locked appearance: {member.description}"
            for member in [host, *friends]
        )

        if video_format == "short":
            pacing = f"""SHAPE - 60-second YouTube Short ({shot_count} shots)
A Short lives or dies in its first 2 seconds. Structure it like this:
- Shot 1: {host.name} opens mid-action with a hook - a startling image or a
  direct question to the child ("इस डिब्बे में क्या है?"). No slow greeting.
- Shots 2-3: the problem lands, fast and visual.
- Middle shots: ONE attempt that fails, then the clever turn.
- Second-to-last shot: the payoff, the biggest visual moment of the video.
- Final shot: {host.name} says the moral in one line and waves goodbye.
Cut every word that is not doing work. One problem only. No sub-plots."""
        else:
            pacing = f"""SHAPE - full 5-minute story ({shot_count} shots)
Three acts, roughly:
- ACT 1 (first ~1/5): {host.name} greets the children, then we meet the world
  and learn exactly what the hero wants and what stands in the way. Plant the
  small detail that will solve everything later - casually, so nobody notices.
- ACT 2 (middle ~3/5): two or three attempts that fail, each failure worse and
  more specific than the last. Stakes rise. Around the two-thirds mark the hero
  hits a low point and nearly gives up.
- ACT 3 (last ~1/5): the planted detail pays off, the hero solves it themselves,
  and we get a warm resolution. {host.name} closes with the moral and a wave.
Also required in a long story:
- Give the hero one small flaw (impatient, scared of the dark, always hungry)
  that they visibly overcome.
- One running gag repeated three times, funnier each time.
- One quiet beat where nobody speaks and the picture carries the feeling."""

        return f"""Create a brand new Hindi story for Indian children aged 4-8.

CONTEXT
- Content type for today: {content_type}
- Date: {signals.get('date')} ({signals.get('weekday')}), season: {signals.get('season')}
- Themes currently trending in Hindi kids content: {keywords}
  Use these only as loose inspiration for mood or setting. Do NOT copy any title.
- Video format: {video_format}, {shot_count} shots x 8 seconds = ~{duration} seconds
- Aspect ratio: {aspect}

{pacing}

THE PERMANENT CAST - THESE NEVER CHANGE
Our channel has recurring characters children already recognise. Their
appearance is locked: you must reuse these exact descriptions, never restyle
their clothes, colours, species or age.

{cast_block}

Rules for the cast:
- {host.name} is the hero and appears in the FIRST and LAST shot, opening the
  episode and closing it with the moral and a wave. Follow the SHAPE section
  above for how the opening should feel.
- {host.name} lives through the adventure in between and must solve it himself.
- The recurring friends listed above must genuinely take part in this episode,
  each with at least one moment that uses their personality, not just cameos.
  A friend may help, but never hands the hero the answer.
- Keep at most three characters in any single shot so the frame stays readable.
- In every veo_prompt, copy each character's locked appearance almost word for
  word so the render stays consistent.
- Give every cast character in the JSON a "cast_key" exactly as shown above.

ALREADY PUBLISHED - your story must be clearly different in premise, guest
characters and setting from every one of these:
{avoid_block}

BEFORE YOU WRITE
Silently think up three different premises: one funny, one gently emotional, one
built on a strange everyday object. Discard any that resemble a story you have
told before or that a hundred other channels have already made. Write the boldest
survivor. Do not show me this thinking - only the final JSON.

REQUIREMENTS
1. You may invent 0-2 NEW one-off guest characters for this story only (a
   grumpy shopkeeper, a lost kitten, a talking kite...). Give them
   "cast_key": null. They appear once and are never seen again.
2. Invent 3-6 distinct locations. Never use the same background for more than
   three shots in a row - the picture must keep changing.
3. Write exactly {shot_count} shots. Each shot is exactly 8 seconds.
4. narration_hi for each shot must be {WORDS_PER_SHOT - 3}-{WORDS_PER_SHOT + 4} Hindi
   words. COUNT THE WORDS of every line before you finish and shorten any that
   run over - {WORDS_PER_SHOT + 4} words is a hard limit, because longer lines get
   sped up to fit the 8 seconds and stop sounding like a storyteller to a small
   child. Devanagari script only. This is
   narration a storyteller reads, not subtitles. Prefer dialogue over description
   wherever a character can say it instead. Never simply narrate what the picture
   already shows - the words should add what the picture cannot.
5. Every shot must move the story. If a shot could be deleted without the child
   noticing, replace it. Give each shot its own emotional beat in "mood", and let
   consecutive shots contrast (loud then quiet, wide then close).
6. veo_prompt for each shot must be in ENGLISH and fully self-contained: never
   reference character_id/bg_id, and re-describe every character's appearance in
   full each time so the look stays consistent. Always start with
   "Pixar-style 3D animation," and end with ", 8 seconds". Each one must state:
   camera (slow dolly-in, low-angle tracking shot, wide establishing, close-up
   on the face...), what the characters physically DO, their facial expression,
   the setting, and the light. Vary the camera between shots - never three
   identical framings in a row. Never ask for on-screen text, letters, words,
   subtitles, logos or UI. Never describe speech or lip movement; the Hindi
   narration is added separately.
7. YouTube metadata in Hindi. The title must promise a story, not describe one -
   curiosity or a question beats a summary. The description opens with a one-line
   hook.

Return ONLY this JSON object:
{{
  "title": "Hindi story title, catchy, under 60 characters",
  "premise": "one line English premise, used to avoid repeats later",
  "moral": "one line Hindi moral",
  "characters": [
    {{"character_id": "char_001",
      "cast_key": "{host.key}",
      "name": "{host.name}",
      "description": "the locked appearance from above, copied"}},
    {{"character_id": "char_002",
      "cast_key": null,
      "name": "guest character Hindi name",
      "description": "detailed English visual description: species or age, build, colours, exact clothing, distinguishing features"}}
  ],
  "backgrounds": [
    {{"bg_id": "bg_001",
      "name": "location name",
      "description": "detailed English visual description of the location, time of day, lighting"}}
  ],
  "shots": [
    {{"shot_id": "shot_001",
      "characters": ["char_001"],
      "background": "bg_001",
      "mood": "emotional beat",
      "narration_hi": "Hindi narration for this 8 second shot",
      "veo_prompt": "Pixar-style 3D animation, <camera>, <full character description doing action>, <setting>, <lighting and mood>, 8 seconds"}}
  ],
  "youtube": {{
    "title": "Hindi YouTube title under 90 characters",
    "description": "Hindi description, 3-4 lines, with a hook first",
    "tags": ["10-14 mixed Hindi and English tags"]
  }}
}}"""


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise StoryGenerationError(f"No JSON object in model reply: {raw[:300]}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise StoryGenerationError(f"Malformed JSON from model: {exc}") from exc


def _validate(story: dict, expected_shots: int, host_key: str) -> None:
    for field in ("title", "shots", "characters", "backgrounds"):
        if not story.get(field):
            raise StoryGenerationError(f"Story is missing required field '{field}'")

    char_ids = {c["character_id"] for c in story["characters"]}
    bg_ids = {b["bg_id"] for b in story["backgrounds"]}
    # character_id -> cast_key, for characters that are permanent cast members.
    cast_by_char = {
        c["character_id"]: c["cast_key"]
        for c in story["characters"]
        if c.get("cast_key")
    }
    host_char_ids = [
        char_id for char_id, key in cast_by_char.items() if key == host_key
    ]

    for index, shot in enumerate(story["shots"], start=1):
        shot.setdefault("shot_id", f"shot_{index:03d}")
        for field in ("narration_hi", "veo_prompt"):
            if not shot.get(field):
                raise StoryGenerationError(f"{shot['shot_id']} is missing '{field}'")
        # Unknown ids would break reference lookup later.
        shot["characters"] = [c for c in shot.get("characters", []) if c in char_ids]
        if shot.get("background") not in bg_ids:
            shot["background"] = story["backgrounds"][0]["bg_id"]

    # The host must bookend every episode so the channel feels consistent.
    if host_char_ids:
        for position in (0, -1):
            present = story["shots"][position]["characters"]
            if host_char_ids[0] not in present:
                present.insert(0, host_char_ids[0])

    # Resolve which locked reference sheets each shot needs.
    for shot in story["shots"]:
        shot["cast_keys"] = [
            cast_by_char[char_id]
            for char_id in shot["characters"]
            if char_id in cast_by_char
        ]

    actual = len(story["shots"])
    if abs(actual - expected_shots) > max(2, expected_shots * 0.2):
        raise StoryGenerationError(
            f"Expected ~{expected_shots} shots, model returned {actual}"
        )

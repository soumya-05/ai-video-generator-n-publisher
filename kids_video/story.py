"""Original explainer script generation via Claude.

There is no topic list anywhere in this project. Every run invents a brand new
subject, seeded by the day's content type, live trend signals, the season, and
the list of everything already published (so nothing ever repeats).

Each shot carries one line of spoken English narration plus a self-contained
prompt for the video model.
"""

import json
import re
from typing import List, Optional

import requests

API_URL = "https://api.anthropic.com/v1/messages"

# Narration for this style sits around 2.6 words/second.
WORDS_PER_SHOT = 21

# One of these is chosen per video to suit the subject, then prefixed verbatim
# to every veo_prompt in that video. The shots are rendered as separate 8-second
# clips with no shared context, so an identical opening phrase is the only thing
# making them look like one continuous video - but the right phrase depends on
# what is being explained. Chip fabrication needs a cleanroom macro look;
# lightning needs storm cinematography. A single fixed style made every video
# look like the same abstract shapes regardless of topic.
STYLE_MENU = {
    "manufacturing": (
        "Photorealistic macro documentary cinematography inside a clean industrial "
        "facility, precise machined detail, accurate materials, crisp focus, "
        "soft diffused overhead lighting"
    ),
    "nature": (
        "Photorealistic high-speed nature cinematography, natural light, real "
        "weather and atmosphere, deep aerial perspective, ultra sharp detail"
    ),
    "mechanism": (
        "Photorealistic engineering cutaway, accurate mechanical geometry and real "
        "materials, one half of the housing removed to reveal the working parts, "
        "clean studio lighting, shallow depth of field"
    ),
    "microscopic": (
        "Photorealistic scientific microscopy visualisation, translucent organic "
        "structures with accurate surface detail, soft volumetric lighting, "
        "dark field background"
    ),
    "space": (
        "Photorealistic astronomical visualisation, astronomically accurate "
        "planetary surfaces and stellar detail, hard directional sunlight, deep "
        "black space, subtle volumetric dust"
    ),
}

SYSTEM_PROMPT = """You are a science and engineering explainer writer for a \
YouTube channel with a worldwide audience, and you also direct the visuals. \
Think Veritasium or Kurzgesagt.

WHAT THE CHANNEL IS
How things actually work. Washing machines, jet engines, particle accelerators,
MRI scanners, galaxies, CPUs, vaccines, the human kidney, lithium batteries,
suspension bridges, CRISPR, black holes, refrigerators, semiconductors. Anything
a curious adult has used or heard of but could not actually explain.

HOW YOU WRITE

Structure:
- Open on a question the viewer cannot answer but feels they should. Never open
  with a greeting, a channel name, or "today we will learn about".
- Then answer it by building one mechanism, step by step, in the order the
  physical thing works. Each shot is one link in that chain.
- Every explanation bottoms out in something the viewer can already picture:
  water, air, magnets, springs, marbles, traffic, heat. Analogy first, then the
  real term.
- End on the consequence - why this mattered, what it made possible, or the
  strangest fact about it. Never end on a summary of what was just said.

Accuracy:
- Everything you state must be factually correct. Numbers, dates, names and
  magnitudes must be real. If you are not certain of a figure, describe the
  scale in words instead of inventing a number.
- Simplify freely, but never say something that is actually false. If a
  simplification is a lie, flag the nuance in one clause instead.
- No pseudoscience, no "scientists don't know why", no conspiracy framing.

Tone:
- Confident, curious, fast. You are explaining to a smart adult who simply has
  not studied this, never to a child.
- No filler, no "as we all know", no rhetorical padding. Every sentence carries
  new information.
- Awe is earned by the facts themselves, not by adjectives.

Language:
- Plain international English, written for the ear and read aloud by a narrator.
- The audience is worldwide and most of them are not native speakers. Use common
  words, short sentences, active voice. No idioms, no slang, no wordplay, no
  culture-specific references, no imperial-only units.
- Define a technical term the moment you use it, in the same sentence.

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
        requested: Optional[dict] = None,
    ) -> dict:
        self.logger.info(
            "Generating %s script (%s, %d shots)%s",
            video_format,
            content_type,
            shot_count,
            f" on request: {requested['topic']}" if requested else "",
        )
        prompt = self._build_prompt(
            content_type, video_format, shot_count, signals, avoid, requested
        )
        # A 38-shot script is a large JSON object and Claude occasionally drops
        # a field from one shot. Another call costs cents, while letting it
        # through wastes an entire render, so just ask again.
        attempts = self.config.get("story.max_attempts", 3)
        for attempt in range(1, attempts + 1):
            try:
                raw = self._call_claude(prompt)
                story = _parse_json(raw)
                story["content_type"] = content_type
                story["format"] = video_format
                _validate(story, shot_count)
                break
            except StoryGenerationError as exc:
                if attempt == attempts:
                    raise
                self.logger.warning(
                    "Script attempt %d/%d rejected (%s); regenerating",
                    attempt, attempts, exc,
                )
        self.logger.info(
            "Script: %s (%d shots)", story.get("title", "?"), len(story["shots"])
        )
        # Narration longer than the shot has to be sped up to fit, and past
        # ~1.3x it stops sounding like a person talking.
        overlong = [
            (shot["shot_id"], words)
            for shot in story["shots"]
            if (words := len(shot["narration"].split())) > WORDS_PER_SHOT + 4
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
        try:
            resp = requests.post(
                API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    # A 38-shot script with a full veo_prompt per shot runs long.
                    "max_tokens": 32000,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
                # A 38-shot script on a reasoning model takes several minutes.
                timeout=self.config.get("story.timeout_seconds", 900),
            )
        except requests.RequestException as exc:
            # Raised as a StoryGenerationError so the retry loop covers a blip.
            raise StoryGenerationError(f"Claude request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise StoryGenerationError(
                f"Claude API {resp.status_code}: {resp.text[:400]}"
            )
        # Newer models may lead with thinking blocks, so take the text ones.
        blocks = resp.json().get("content", [])
        text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
        if not text:
            raise StoryGenerationError(
                f"No text in model reply: {json.dumps(blocks)[:300]}"
            )
        return text

    def _build_prompt(
        self,
        content_type: str,
        video_format: str,
        shot_count: int,
        signals: dict,
        avoid: List[str],
        requested: Optional[dict] = None,
    ) -> str:
        duration = shot_count * 8
        keywords = ", ".join(signals.get("trending_keywords") or []) or "(none available)"
        avoid_block = "\n".join(f"- {line}" for line in avoid) or "- (nothing yet)"
        aspect = self.config.get(f"{video_format}.aspect_ratio", "16:9")
        style_menu = "\n".join(
            f'   - {name}: "{text}"' for name, text in STYLE_MENU.items()
        )

        if video_format == "short":
            pacing = f"""SHAPE - 60-second YouTube Short ({shot_count} shots)
A Short is won or lost in the first 2 seconds.
- Shot 1: the hook. One startling fact or one question, over the single most
  striking image in the video. No introduction of any kind.
- Shots 2-3: the setup - what the thing is and why the obvious answer is wrong.
- Middle shots: the mechanism, one clean step per shot, no digressions.
- Second-to-last shot: the payoff - the moment it clicks.
- Final shot: the consequence or the strangest number, and stop.
One idea only. Cut every word not carrying information. No sub-topics."""
        else:
            pacing = f"""SHAPE - full 5-minute explainer ({shot_count} shots)
- OPEN (first ~1/6): the hook question, then why the intuitive answer fails.
  State plainly what the viewer will understand by the end.
- BUILD (middle ~4/6): the mechanism assembled step by step, in physical order.
  Introduce exactly one new idea per shot and use the previous shot's idea to
  do it. Every ~6 shots, land a concrete number, a date, or a real-world
  consequence so the viewer gets a reward for staying.
- CLOSE (last ~1/6): zoom out - what this made possible, what it costs, what
  breaks when it fails, or the open question at the frontier.
Also required in a long video:
- One counter-intuitive fact the viewer will want to repeat to someone.
- One moment where you name what people commonly get wrong about this.
- One quiet shot with a wide, slow visual where the narration says very little."""

        if requested:
            extra = (
                f"\nThey also said: {requested['description']}"
                if requested.get("description")
                else ""
            )
            subject_block = f"""THE SUBJECT IS FIXED - DO NOT CHOOSE YOUR OWN
The channel owner has asked for exactly this:

  {requested['topic']}{extra}

Cover that and nothing else. If it is too broad for {shot_count} shots, narrow
it to the single most interesting mechanism inside it and explain that properly
rather than skimming the whole field. Ignore the published list below except as
a reminder of what you have already said, and the trending themes entirely."""
        else:
            subject_block = f"""CHOOSING THE SUBJECT
Pick ONE specific mechanism, object or phenomenon - not a broad field. "How a
washing machine gets clothes clean" is a subject; "washing machines" is not.
"Why MRI needs liquid helium" is a subject; "medical imaging" is not. Narrow
enough that {shot_count} shots can actually explain it end to end."""

        return f"""Write a brand new explainer video script.

CONTEXT
- Content type for today: {content_type}
- Date: {signals.get('date')} ({signals.get('weekday')}), season: {signals.get('season')}
- Themes currently trending in science and tech content: {keywords}
  Use these only as loose inspiration for the subject area. Do NOT copy a title.
- Video format: {video_format}, {shot_count} shots x 8 seconds = ~{duration} seconds
- Aspect ratio: {aspect}

{pacing}

{subject_block}

ALREADY PUBLISHED - your subject must be clearly different from every one of
these. Not a different angle on the same object: a different object.
{avoid_block}

BEFORE YOU WRITE
Silently plan the chain of explanation end to end and check every link actually
follows from the one before it. Discard any framing every science channel has
already used. Do not show me this thinking - only the final JSON.

REQUIREMENTS
1. Choose the "visual_style" that genuinely suits this subject, by copying ONE
   value verbatim from this menu:
{style_menu}
   Pick on the physics of what you are showing, not on habit. Chip fabrication
   or an engine assembly line is "manufacturing". Lightning, ocean waves or
   volcanoes are "nature". A pump, gearbox or hard drive is "mechanism". Cells,
   viruses or blood is "microscopic". Planets, stars or spacecraft is "space".
2. Invent 4-8 distinct visual settings ("backgrounds") that actually exist in
   this subject: the specific machine, the specific chamber, the specific
   landscape, the specific scale. Never use the same background for more than
   three shots in a row.
3. Write exactly {shot_count} shots. Each shot is exactly 8 seconds.
4. narration for each shot must be {WORDS_PER_SHOT - 4}-{WORDS_PER_SHOT + 4}
   English words. COUNT THE WORDS of every line before you finish and shorten
   any that run over - {WORDS_PER_SHOT + 4} words is a hard limit, because
   longer lines get sped up to fit the 8 seconds.
5. Every shot must advance the explanation. If a shot could be deleted without
   the viewer losing a step, replace it. Give each shot its own beat in "mood".
6. veo_prompt is the most important field. THE VIEWER MUST SEE WHAT THEY HEAR.
   Each prompt must show the exact thing its own narration is describing, at the
   moment it is described. If the line says molten steel is poured, the shot is
   molten steel being poured - not a generic factory, not an abstract shape.
   Reuse the concrete nouns from that shot's narration inside its veo_prompt.
   Every prompt begins with the visual_style string you chose in requirement 1,
   copied character for character, and ends with ", 8 seconds". THE STYLE IS
   IDENTICAL IN EVERY SHOT - the shots are rendered separately and only look
   like one video if that opening phrase never varies.
   Between the style and the ending, state:
     a) the real subject in specific physical detail - name the actual parts,
        materials, colours, textures and scale a viewer would see. "A 300mm
        silicon wafer, mirror-polished, held on a vacuum chuck under amber
        cleanroom light" is right. "A smooth disc shape" is wrong and is the
        single most common failure. Specific beats simple.
     b) exactly one thing that moves, slowly, and it must be the thing the
        narration is about. One motion only - busy shots read as messy.
     c) the camera as a simple move: "slow push-in", "slow orbit", "static wide
        shot", "gentle pan left", "cutaway slowly opening". Vary it between
        shots; never three identical moves in a row.
   Keep every prompt self-contained: never reference bg_id or an earlier shot.
   NO human characters, no faces, no hands, no presenter.
   Never ask for on-screen text, numbers, labels, arrows, letters, subtitles,
   logos, watermarks or UI - the video model renders text as garbage.
7. YouTube metadata in English, written for a worldwide audience. The title must
   promise the answer to a question - curiosity beats description. The
   description opens with a one-line hook.

Return ONLY this JSON object:
{{
  "title": "catchy title, under 60 characters",
  "subject": "one line statement of exactly what is explained, used to avoid repeats later",
  "takeaway": "one line statement of the single thing the viewer now understands",
  "visual_style": "the ONE style string copied verbatim from the menu in requirement 1",
  "backgrounds": [
    {{"bg_id": "bg_001",
      "name": "setting name",
      "description": "what is physically there: the real object, its materials, its scale"}}
  ],
  "shots": [
    {{"shot_id": "shot_001",
      "background": "bg_001",
      "mood": "beat of this shot",
      "narration": "spoken English narration for this 8 second shot",
      "veo_prompt": "<visual_style verbatim>, <the exact thing this shot's narration describes, in specific physical detail>, <the one thing that moves>, <camera move>, 8 seconds"}}
  ],
  "youtube": {{
    "title": "YouTube title under 90 characters",
    "description": "description, 3-4 lines, with a hook first",
    "tags": ["10-14 tags"]
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


def _resolve_style(story: dict) -> str:
    """The one style string every veo_prompt in this video must open with.

    The model is asked to copy a menu value verbatim but sometimes returns the
    menu key instead, or paraphrases it.
    """
    chosen = (story.get("visual_style") or "").strip().rstrip(",")
    if chosen in STYLE_MENU:  # returned the key, not the value
        chosen = STYLE_MENU[chosen]
    if chosen not in STYLE_MENU.values():
        # A cutaway of the real object is the safest look for "how it works".
        chosen = STYLE_MENU["mechanism"]
    story["visual_style"] = chosen
    return chosen


def _strip_style(prompt: str) -> str:
    """Drop a leading style clause the model wrote for itself, if any."""
    for text in STYLE_MENU.values():
        if prompt.startswith(text):
            return prompt[len(text) :].lstrip(", ")
    return prompt


def _validate(story: dict, expected_shots: int) -> None:
    for field in ("title", "shots", "backgrounds"):
        if not story.get(field):
            raise StoryGenerationError(f"Script is missing required field '{field}'")

    bg_ids = {b["bg_id"] for b in story["backgrounds"]}
    style = _resolve_style(story)

    for index, shot in enumerate(story["shots"], start=1):
        shot.setdefault("shot_id", f"shot_{index:03d}")
        for field in ("narration", "veo_prompt"):
            if not shot.get(field):
                raise StoryGenerationError(f"{shot['shot_id']} is missing '{field}'")
        # Continuity depends entirely on this prefix being byte-identical across
        # shots, and the model does paraphrase it. Repairing is free; another
        # full script generation is not.
        if not shot["veo_prompt"].startswith(style):
            shot["veo_prompt"] = f"{style}, {_strip_style(shot['veo_prompt'])}"
        # An unknown id would break the artifact cross-reference later.
        if shot.get("background") not in bg_ids:
            shot["background"] = story["backgrounds"][0]["bg_id"]

    actual = len(story["shots"])
    if abs(actual - expected_shots) > max(2, expected_shots * 0.2):
        raise StoryGenerationError(
            f"Expected ~{expected_shots} shots, model returned {actual}"
        )

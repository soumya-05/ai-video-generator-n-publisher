"""The channel's permanent cast of 3D characters.

Kids come back for characters they recognise, so appearance must be locked
forever. Each cast member gets a multi-view reference sheet rendered exactly
once, hosted permanently on imgbb, and pinned in data/cast.json (committed).
Those URLs are then passed to Veo as REFERENCE_2_VIDEO inputs so the character
looks identical in every clip of every episode.

Adding cast members is close to free: a Veo clip costs the same $0.325 whether
one character or four are on screen. The only cost is the one-off reference
sheet render (a few cents per character).

Veo accepts at most 3 reference images per clip, so reference_urls_for()
budgets those slots across whoever appears in a given shot.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import requests

IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"
MAX_REFERENCES = 3  # hard Veo limit

VIEWS = {
    "front": "front view, facing the camera directly, neutral friendly smile",
    "three_quarter": "three-quarter view turned 45 degrees, mid-gesture, warm expression",
    "side": "full side profile view, standing naturally",
}
VIEW_ORDER = ["front", "three_quarter", "side"]


class CastError(RuntimeError):
    pass


class Character:
    def __init__(self, key: str, data: dict):
        self.key = key
        self.name: str = data["name"]
        self.role: str = data.get("role", "friend")
        self.description: str = data["description"]
        self.catchphrase: str = data.get("catchphrase", "")
        self.reference_urls: Dict[str, str] = data.get("reference_urls", {})

    @property
    def is_host(self) -> bool:
        return self.role == "host"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "catchphrase": self.catchphrase,
            "reference_urls": self.reference_urls,
        }


class Cast:
    def __init__(self, members: Dict[str, Character]):
        self.members = members
        hosts = [m for m in members.values() if m.is_host]
        if not hosts:
            raise CastError("Cast has no character with role 'host'")
        self.host = hosts[0]

    @property
    def friends(self) -> List[Character]:
        return [m for m in self.members.values() if not m.is_host]

    def get(self, key: str) -> Optional[Character]:
        return self.members.get(key)

    def rotate_friends(self, count: int, offset: int) -> List[Character]:
        """Pick recurring friends for an episode, rotating so all get screen time."""
        friends = self.friends
        if not friends or count <= 0:
            return []
        count = min(count, len(friends))
        start = offset % len(friends)
        return [friends[(start + i) % len(friends)] for i in range(count)]

    def reference_urls_for(self, keys: Sequence[str]) -> List[str]:
        """Reference images for a shot, within Veo's 3-image budget.

        A single character gets all three views (strongest identity lock).
        Multiple characters share the slots, host first, one view each.
        """
        present = [self.members[key] for key in keys if key in self.members]
        present = [c for c in present if c.reference_urls]
        if not present:
            return []

        present.sort(key=lambda c: not c.is_host)  # host first

        if len(present) == 1:
            character = present[0]
            return [
                character.reference_urls[view]
                for view in VIEW_ORDER
                if view in character.reference_urls
            ][:MAX_REFERENCES]

        urls: List[str] = []
        for index, character in enumerate(present[:MAX_REFERENCES]):
            # Give the first character a second angle if there is a spare slot.
            wanted = VIEW_ORDER[: 2 if index == 0 and len(present) == 2 else 1]
            for view in wanted:
                if view in character.reference_urls and len(urls) < MAX_REFERENCES:
                    urls.append(character.reference_urls[view])
        return urls


class CastManager:
    def __init__(self, config, kie_client):
        self.config = config
        self.kie = kie_client
        self.logger = config.logger.getChild("cast")
        self.path = Path(config.get("paths.cast_file", "data/cast.json"))

    def load(self) -> Optional[Cast]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CastError(f"{self.path} is unreadable: {exc}") from exc
        members = {key: Character(key, value) for key, value in data.items()}
        if not members:
            return None
        return Cast(members)

    def from_config(self) -> Cast:
        """The cast as described in config, without rendered reference sheets.

        Enough to write and preview a story; not enough to render it.
        """
        defined = self.config.get("cast", {})
        if not defined:
            raise CastError("No 'cast' section in config.yaml")
        return Cast({key: Character(key, spec) for key, spec in defined.items()})

    def require(self) -> Cast:
        cast = self.load()
        if cast is None:
            raise CastError(
                "No cast has been created yet. Run this once:\n"
                "    python run_daily_pipeline.py setup-cast\n"
                "It renders each character's reference sheet, hosts them on imgbb, "
                f"and writes {self.path}. Commit that file so every future episode "
                "reuses the exact same characters."
            )
        return cast

    def create(self, force: bool = False, only: Optional[str] = None) -> Cast:
        """Render reference sheets for cast members that don't have them yet."""
        existing = self.load()
        defined = self.config.get("cast", {})
        if not defined:
            raise CastError("No 'cast' section in config.yaml")

        current: Dict[str, dict] = {}
        if existing:
            current = {key: member.as_dict() for key, member in existing.members.items()}

        for key, spec in defined.items():
            if only and key != only:
                continue
            already = current.get(key, {}).get("reference_urls")
            if already and not force:
                self.logger.info("%s already has a reference sheet; skipping", key)
                # Keep the pinned URLs but let config edits to text fields through.
                current[key] = {
                    **spec,
                    "reference_urls": already,
                    "name": current[key]["name"],
                    "description": current[key]["description"],
                }
                continue

            self.logger.info("Rendering reference sheet for '%s'", spec.get("name", key))
            current[key] = {**spec, "reference_urls": self._render_sheet(key, spec)}

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.logger.info("Cast saved to %s", self.path)
        return Cast({key: Character(key, value) for key, value in current.items()})

    def _render_sheet(self, key: str, spec: dict) -> Dict[str, str]:
        model = self.config.get("kie.character_image_model", "nano-banana-pro")
        urls: Dict[str, str] = {}
        for view, view_prompt in VIEWS.items():
            prompt = (
                f"Pixar-style 3D animated character, {spec['description']}. "
                f"{view_prompt}. Full body, centred, plain neutral light grey "
                f"studio background, soft even studio lighting, consistent "
                f"proportions, high detail character model sheet render. "
                f"No text, no letters, no watermark."
            )
            kie_url = self.kie.generate_image(
                model,
                {
                    "prompt": prompt,
                    "aspect_ratio": "1:1",
                    "resolution": "2K",
                    "output_format": "png",
                },
                label=f"{key}/{view}",
            )
            # kie.ai deletes generated media after 14 days, so the sheet must be
            # re-hosted somewhere permanent before it is pinned.
            urls[view] = self._host_permanently(kie_url, f"{key}_{view}")
        return urls

    def _host_permanently(self, source_url: str, name: str) -> str:
        api_key = self.config.key("imgbb")
        if not api_key:
            raise CastError(
                "IMGBB_API_KEY is required to host character sheets permanently. "
                "kie.ai deletes generated images after 14 days, which would break "
                "character consistency. Free key: https://api.imgbb.com/"
            )
        resp = requests.post(
            IMGBB_UPLOAD_URL,
            data={"key": api_key, "image": source_url, "name": name},
            timeout=120,
        )
        if resp.status_code >= 400:
            raise CastError(f"imgbb upload failed ({resp.status_code}): {resp.text[:200]}")
        body = resp.json()
        if not body.get("success"):
            raise CastError(f"imgbb upload rejected: {body}")
        return body["data"]["url"]

"""Dominant colour from an image, with no API and no AI.

Vision models are rate-limited and occasionally unavailable; colour is the one
attribute that actually drives product matching, and it is plain arithmetic on
pixels. This runs locally, always succeeds, and costs nothing — so a customer's
photo is never wasted just because a quota was exhausted.

It answers "what colour", not "what garment". Vision still adds category,
pattern and fabric when it is available.
"""

from __future__ import annotations

import colorsys
import io
import logging
from collections import Counter

logger = logging.getLogger(__name__)

try:
    from PIL import Image

    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    Image = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False


# Named colours as hue ranges. Hue alone is unreliable at the extremes, so
# saturation and value decide the neutrals first.
HUE_NAMES = (
    (345, 360, "red"),
    (0, 12, "red"),
    (12, 24, "rust"),
    (24, 40, "orange"),
    (40, 55, "mustard"),
    (55, 70, "yellow"),
    (70, 155, "green"),
    (155, 190, "teal"),
    (190, 250, "blue"),
    (250, 280, "purple"),
    (280, 320, "pink"),
    (320, 345, "maroon"),
)


# Colours that are usually the studio, the paper or the model's skin rather
# than the product, so they are counted but never allowed to win outright.
BACKGROUND_NAMES = frozenset(
    {"white", "off white", "black", "grey", "charcoal", "cream", "beige"}
)


def name_for(red: int, green: int, blue: int) -> str:
    """A human colour word for one RGB triple."""
    hue, lightness, saturation = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
    degrees = hue * 360

    # Neutrals are decided by lightness and saturation, never by hue. The
    # thresholds matter: catalogue photos are shot on beige and cream
    # backdrops, and a loose saturation floor turns all of that backdrop into
    # "orange", which then outvotes the garment itself.
    if lightness < 0.12:
        return "black"
    if lightness > 0.92 and saturation < 0.18:
        return "white"
    if saturation < 0.18:
        if lightness > 0.75:
            return "off white"
        return "grey" if lightness > 0.3 else "charcoal"
    # Pale and washed out: backdrop, paper, skin — not a garment colour.
    if lightness > 0.70 and saturation < 0.45:
        return "cream"
    if 0.55 < lightness <= 0.70 and saturation < 0.30:
        return "beige"

    for start, end, name in HUE_NAMES:
        if start <= degrees < end:
            # Dark reds read as maroon, pale reds as pink — the same hue.
            if name == "red" and lightness < 0.3:
                return "maroon"
            if name == "red" and lightness > 0.7:
                return "pink"
            if name == "blue" and lightness < 0.3:
                return "navy"
            if name == "orange" and lightness < 0.35:
                return "brown"
            if name == "yellow" and lightness < 0.4:
                return "olive"
            return name
    return "multi"


def dominant_colours(payload: bytes, top: int = 2) -> list[str]:
    """The most common colour names in an image, most common first.

    Near-white and near-black pixels are discounted because product photos are
    shot on white backgrounds — the garment, not the studio, is the subject.
    """
    if not PILLOW_AVAILABLE or not payload:
        return []

    try:
        with Image.open(io.BytesIO(payload)) as image:
            image = image.convert("RGB")
            # Thumbnailing is the whole speed trick: a few thousand pixels
            # describe the colour just as well as a few million.
            image.thumbnail((96, 96))
            pixels = list(image.getdata())
    except Exception as exc:  # noqa: BLE001 - a corrupt upload is not an error
        logger.warning("could not read image for colour extraction: %s", exc)
        return []

    if not pixels:
        return []

    # Each pixel votes in proportion to how vivid it is. The garment is almost
    # always the most saturated thing in a catalogue photo, while backdrops,
    # paper and skin are washed out — so weighting by saturation separates
    # subject from studio without hand-tuning a threshold per background.
    counter: Counter[str] = Counter()
    for red, green, blue in pixels:
        hue, lightness, saturation = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
        # Mid-tone and vivid wins; near-black and near-white contribute little.
        vividness = (saturation ** 2) * max(0.0, 1.0 - abs(lightness - 0.5) * 1.4)

        # Skin occupies a narrow warm band and covers a lot of a model shot,
        # so it is discounted rather than excluded — a genuinely tan garment
        # should still be able to win on volume.
        degrees = hue * 360
        if 15 <= degrees <= 50 and saturation < 0.62 and 0.30 < lightness < 0.88:
            vividness *= 0.25

        name = name_for(red, green, blue)
        counter[name] += vividness + (0.02 if name in BACKGROUND_NAMES else 0.0)

    ranked = [name for name, _ in counter.most_common()]
    coloured = [n for n in ranked if n not in BACKGROUND_NAMES]

    # Prefer actual colours, but a genuinely black or white garment must still
    # be reported rather than dropped.
    ordered = coloured + [n for n in ranked if n not in coloured]
    return ordered[:top]


def analyse(payload: bytes) -> dict[str, str]:
    """A vision-shaped result built from pixels alone."""
    found = dominant_colours(payload)
    if not found:
        return {}

    # Deliberately marked uncertain. On a full model shot, skin and warm
    # backdrops can outvote the garment, so this is a hint for the agent to
    # confirm — never something to state as fact.
    result: dict[str, str] = {
        "colour": found[0],
        "colour_source": "pixels",
        "confidence": "low",
    }
    if len(found) > 1:
        result["secondary_colour"] = found[1]
    result["description"] = f"Possibly {found[0]}, read from the image itself."
    return result

"""Readable names for video files ("Simplify file names" in settings).

A release name such as ``[SubsPlease] Honzuki no Gekokujou S4 - 13 (1080p)
[A4FE0990].mkv`` is shown as ``13 · Honzuki no Gekokujou S4``: no extension,
no bracketed release details, and the episode moved to the front. Only the
displayed text changes; sorting, history and playback keep the real name.

Built against the names in a real anime library (fansub, Sonarr-style and
scene releases). Square-bracket groups are dropped; parenthesised groups are
dropped when they hold release details (a resolution, source, codec, year or
other number) and kept when they are part of a title, as in "You Are (Not)
Alone" or "Washio Sumi's Chapter (Friends)".
"""

from __future__ import annotations

import re

# Release details: resolutions, sources, codecs, audio, bit depths, and the
# like. Matched as whole words, case-insensitively. A bare "BD" alone does not
# make a group a release detail ("BD Menu Vol.1" is a label); see _WEAK.
_WEAK = {"bd", "tv", "web", "h", "nf", "cr", "adn", "dual", "multi"}
_TECH = re.compile(
    r"^(?:\d{3,4}[pi]|\d{3,4}x\d{3,4}p?|[248]k|uhd|"
    r"bd|bds|bdrip|bdremux|bd-?rip|blu-?ray|bluray|remux|web|web-?dl|webrip|hdtv|dvd|dvdrip|tv|"
    r"x26[45]|h\.?26[45]|h|hevc|avc|av1|vp9|xvid|hi10p?|hi444pp|ma10p|10-?bits?|8-?bits?|yuv420p10|"
    r"aac[\d.x+]*|flac[\d.]*|dualflac|opus[\d.]*|ac3|eac3|ddp?[\d.]*|dts(?:-hd)?|truehd|vorbis|mp3|"
    r"dual(?:-audio)?|multi|repack|proper|uncensored|nf|amzn|cr|adn|hmax|dsnp|hulu|atvp|"
    r"\d+(?:\.\d)?ch|5\.1|2\.0)$",
    re.IGNORECASE,
)
_EXTRA = r"(?:SP|OVA|OAD|NCED|NCOP|NC|ED|OP|PV|CM)"
_SEASON_EPISODE = re.compile(r"\bS(\d{1,2})[ ._]?E(\d{1,4})(?:v\d+)?\b", re.IGNORECASE)
_CROSS = re.compile(r"(?<![\d.])(\d{1,2})x(\d{2,3})(?:v\d+)?\b")
_DASH_NUMBER = re.compile(r"(?:^|\s)-\s+(\d{1,4}(?:\.\d)?)(?:v\d+)?(?=\s|$)")
_LEADING_NUMBER = re.compile(r"^(\d{1,4}(?:\.\d)?)(?:v\d+)?\s+-\s+")
_EPISODE_WORD = re.compile(r"\b(?:EP|Ep|ep|E|Episode|episode)\.?\s?(\d{1,4}(?:\.\d)?(?:-\d{1,4})?)(?:v\d+)?\b")
_EXTRA_NUMBER = re.compile(rf"\b({_EXTRA}\d{{1,3}})(?:v\d+)?\b")
_TRAILING_NUMBER = re.compile(r"(?:^|\s)(?!(?:19|20)\d\d$)(\d{2,4}(?:\.\d)?)(?:v\d+)?$")
# Words that make a trailing number a label rather than an episode.
_NOT_EPISODE = re.compile(
    r"(?:vol|volume|part|menu|movie|card|disc|special|phase|chapter|finale|season|bonus)\.?$", re.IGNORECASE)
_BRACKETS = re.compile(r"\[[^\[\]]*\]|【[^【】]*】")
_PARENS = re.compile(r"\(([^()]*)\)")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{2,4}$")


def _is_release_detail(text: str) -> bool:
    """A group holding release details: a strong tech word, a year or a CRC."""
    text = text.strip()
    if re.fullmatch(r"(?:19|20)\d\d|[0-9A-Fa-f]{8}|v\d+", text):
        return True
    words = [w for w in re.split(r"[\s_+,&/]+", text) if w]
    return any(_TECH.match(w) and w.lower() not in _WEAK for w in words) or any(
        _TECH.match(part) and part.lower() not in _WEAK for w in words for part in w.split("-") if part)


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:[-–—~·:]\s*)+|(?:\s*[-–—·:])+$", "", text).strip()
    return re.sub(r"\s+-(?:\s+-)+\s+", " - ", text)


def _strip_trailing_details(text: str) -> str:
    """Drop release details trailing the title ("Gurren Lagann 01 BD"), and a
    trailing year after a title ("... Part 1 2024")."""
    words = text.split(" ")
    while len(words) > 1 and (_TECH.match(words[-1].strip("-_.")) or re.fullmatch(r"v\d+|(?:19|20)\d\d", words[-1])):
        words.pop()
    return " ".join(words)


def _is_tech_word(word: str) -> bool:
    return bool(_TECH.match(word.strip("-_.")) or _TECH.match(word.split("-")[0]))


def display_name(filename: str) -> str:
    """The readable form of ``filename``; the name itself when nothing is left."""
    stem = _EXTENSION.sub("", filename)
    text = stem
    # A release group stuck on after the last bracket: "...[x264][FLAC]-CW".
    text = re.sub(r"(?<=[\])])\s*-[^\s\[\]()]+$", "", text)
    # Episodes written only inside brackets ("[VCB-Studio] Love Live! S1 [01]").
    bracketed = [m.group(0)[1:-1].strip() for m in _BRACKETS.finditer(text)]
    episode_in_brackets, episode_title = None, ""
    for pattern in (r"^(\d{1,4}(?:\.\d)?)(?:v\d+)?(?:\(.*\))?$", rf"^({_EXTRA}\d{{0,3}})(?:v\d+)?$"):
        episode_in_brackets = next((m.group(1) for b in bracketed for m in [re.match(pattern, b)] if m), None)
        if episode_in_brackets:
            break
    if episode_in_brackets is None:
        # "[01 Snow halation]": the episode and its title.
        for b in bracketed:
            m = re.match(r"^(\d{1,3})\s+(.+)$", b)
            if m and not _is_release_detail(m.group(2)):
                episode_in_brackets, episode_title = m.group(1), m.group(2)
                break
    without = _BRACKETS.sub(" ", text)
    # An episode in parentheses is still the episode: "(S00E01v2)", "(Ep. 06)".
    without = re.sub(r"\(((?:S\d+E\d+|(?:Ep\.?|EP|Episode)\s?\d+(?:-\d+)?)(?:v\d+)?)\)", r"\1", without)
    without = _PARENS.sub(lambda m: " " if _is_release_detail(m.group(1)) else m.group(0), without)
    if not re.search(r"[^\W\d_]", without):
        # Everything was bracketed ("[DBD-Raws][Love Live! Superstar!! S1][01][1080P]"):
        # the title is the descriptive groups after the release group.
        titled = [b for b in bracketed[1:] if re.search(r"[^\W\d_]{2}", b) and not _is_release_detail(b)
                  and b != episode_in_brackets]
        without = " - ".join(titled)
    text = without.replace("_", " ")
    if " " not in stem.strip() and stem.count(".") >= 2:
        # Scene names separate words with dots; keep decimals such as "5.1".
        text = re.sub(r"\b(\d)\.(\d)\b", "\\1\x00\\2", text).replace(".", " ").replace("\x00", ".")
    text = _tidy(text)

    # Scene names: everything from the first release detail on goes.
    words = text.split(" ")
    for i, word in enumerate(words[1:], start=1):
        if _is_tech_word(word) and (" " not in stem.strip() or i == len(words) - 1):
            words = words[:i]
            break
    text = _strip_trailing_details(_tidy(" ".join(words)))

    label, start, end = None, None, None
    for pattern in (_SEASON_EPISODE, _CROSS):
        match = pattern.search(text)
        if match:
            label = f"S{int(match.group(1)):02d}E{match.group(2)}"
            start, end = match.span()
            break
    if label is None:
        for pattern in (_LEADING_NUMBER, _DASH_NUMBER, _EPISODE_WORD, _EXTRA_NUMBER):
            match = pattern.search(text)
            if match:
                label = match.group(1)
                start, end = match.span()
                break
    if label is None:
        match = _TRAILING_NUMBER.search(text)
        if match and not _NOT_EPISODE.search(text[:match.start()].rstrip()):
            label = match.group(1)
            start, end = match.span()
    if label is None and episode_in_brackets is not None:
        label, start, end = episode_in_brackets, len(text), len(text)
        text += f" {episode_title}" if episode_title else ""
    if label is None:
        return _tidy(text) or stem

    title = _tidy(text[:start])
    rest = _tidy(text[end:])
    if label.startswith("S") and re.match(r"^\d{2,4}\b", rest):
        rest = _tidy(re.sub(r"^\d{2,4}\s*(?:-\s*)?", "", rest))  # Sonarr's absolute number
    rest = re.sub(r"(?:^|\s)v\d+(?=\s|$)", " ", rest).strip()
    # A parenthesised remainder reads as part of the title: "Hyouka (OVA)".
    joiner = " " if rest.startswith("(") else " - "
    body = joiner.join(part for part in (title, rest) if part)
    return f"{label} · {body}" if body else label

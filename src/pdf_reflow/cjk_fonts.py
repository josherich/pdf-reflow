"""OS-specific CJK font resolution.

PyMuPDF ships four bundled CID fonts (china-s / china-t / japan / korea)
that work everywhere but render every glyph at fullwidth (1 em) and use
the rather plain Droid Sans Fallback shape. When the host system has a
better CJK font installed (PingFang on macOS, Microsoft YaHei on
Windows, Noto Sans CJK on Linux), we prefer that — it gives proper
proportional metrics for ASCII embedded in CJK runs and a more
idiomatic look per region.

The resolver is process-cached and lazy: each script is probed at most
once. Failure modes fall through cleanly to the bundled CID font so the
reflow pipeline never depends on a system font being present.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# Abstract scripts the rest of the codebase uses to ask for a CJK font.
# Layout / metrics code traffics in these names, not concrete filenames.
SCRIPT_HAN = "han"
SCRIPT_JAPAN = "japan"
SCRIPT_KOREA = "korea"
# Latin-extended fallback scripts. These exist so the layout can ask for
# a "rich" serif / sans-serif font when base14 Times/Helvetica is missing
# a glyph (IPA letters, modifier letters like U+02E4 ʔˤ, accented
# diacritics like U+01D0 ǐ). The bundled entry maps back to base14 so
# callers that hit this path on a host without a suitable system font
# degrade to the same behaviour as before.
SCRIPT_LATIN_SERIF = "latin-serif"
SCRIPT_LATIN_SANS = "latin-sans"

ALL_SCRIPTS = (SCRIPT_HAN, SCRIPT_JAPAN, SCRIPT_KOREA,
               SCRIPT_LATIN_SERIF, SCRIPT_LATIN_SANS)


def is_cjk_char(c: str) -> bool:
    """True iff ``c`` lies in a CJK / Kana / Hangul / CJK-punctuation block.

    Lives here so both ``analyze`` (line-join policy) and ``layout``
    (per-char font routing & wrap tokenization) share one definition.
    """
    if not c:
        return False
    o = ord(c)
    return (
        0x4E00 <= o <= 0x9FFF      # CJK Unified Ideographs
        or 0x3400 <= o <= 0x4DBF   # CJK Ext A
        or 0x20000 <= o <= 0x2A6DF # CJK Ext B
        or 0xF900 <= o <= 0xFAFF   # CJK Compatibility Ideographs
        or 0x3040 <= o <= 0x309F   # Hiragana
        or 0x30A0 <= o <= 0x30FF   # Katakana
        or 0xAC00 <= o <= 0xD7AF   # Hangul Syllables
        or 0x1100 <= o <= 0x11FF   # Hangul Jamo
        or 0x3130 <= o <= 0x318F   # Hangul Compatibility Jamo
        or 0x3000 <= o <= 0x303F   # CJK Symbols & Punctuation
        or 0xFF00 <= o <= 0xFFEF   # Halfwidth / Fullwidth Forms
    )


@dataclass(frozen=True)
class CJKFontEntry:
    """Concrete CJK font binding for a script.

    ``fontname`` is the name used in ``insert_text``. ``fontfile`` is the
    on-disk TTF/TTC/OTF path when this entry came from a system font;
    None means the entry is a bundled PyMuPDF CID font (in which case
    ``fontname`` is one of ``china-s`` / ``japan`` / ``korea``).
    ``fullwidth`` is True for bundled CID fonts where PyMuPDF renders
    every glyph at 1 em regardless of the real advance — width metrics
    must account for that to keep wrap boundaries aligned with what
    actually gets drawn.
    """
    fontname: str
    fontfile: Optional[str]
    fullwidth: bool

    @property
    def is_system(self) -> bool:
        return self.fontfile is not None


# Bundled fallback (always available — PyMuPDF ships these).
_BUNDLED: Dict[str, CJKFontEntry] = {
    SCRIPT_HAN: CJKFontEntry("china-s", None, fullwidth=True),
    SCRIPT_JAPAN: CJKFontEntry("japan", None, fullwidth=True),
    SCRIPT_KOREA: CJKFontEntry("korea", None, fullwidth=True),
    # No bundled Latin-extended font in PyMuPDF; degrade to base14.
    # ``fullwidth=False`` because base14 fonts use proportional metrics.
    SCRIPT_LATIN_SERIF: CJKFontEntry("times-roman", None, fullwidth=False),
    SCRIPT_LATIN_SANS: CJKFontEntry("helvetica", None, fullwidth=False),
}


# Ordered candidate paths per (platform, script). First existing file wins.
# Font names are arbitrary identifiers used to register the font on the
# output PDF; pick distinct names so different scripts can coexist.
_DARWIN: Dict[str, List[Tuple[str, str]]] = {
    SCRIPT_HAN: [
        ("PingFang-SC", "/System/Library/Fonts/PingFang.ttc"),
        ("STHeiti", "/System/Library/Fonts/STHeiti Medium.ttc"),
        ("Hiragino-SansGB", "/System/Library/Fonts/Hiragino Sans GB.ttc"),
    ],
    SCRIPT_JAPAN: [
        ("HiraginoSans", "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"),
        ("HiraginoSans", "/System/Library/Fonts/Hiragino Sans GB.ttc"),
        ("OsakaMono", "/System/Library/Fonts/Osaka.ttf"),
    ],
    SCRIPT_KOREA: [
        ("AppleSDGothicNeo", "/System/Library/Fonts/AppleSDGothicNeo.ttc"),
        ("AppleGothic", "/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
    ],
    SCRIPT_LATIN_SERIF: [
        # Apple's bundled Times has full IPA + Latin Extended-A/B.
        ("AppleTimes", "/System/Library/Fonts/Times.ttc"),
        ("Georgia", "/Library/Fonts/Georgia.ttf"),
    ],
    SCRIPT_LATIN_SANS: [
        ("AppleHelvetica", "/System/Library/Fonts/Helvetica.ttc"),
        ("HelveticaNeue", "/System/Library/Fonts/HelveticaNeue.ttc"),
    ],
}


_WIN32: Dict[str, List[Tuple[str, str]]] = {
    SCRIPT_HAN: [
        ("MicrosoftYaHei", r"C:\Windows\Fonts\msyh.ttc"),
        ("MicrosoftYaHei", r"C:\Windows\Fonts\msyh.ttf"),
        ("SimSun", r"C:\Windows\Fonts\simsun.ttc"),
    ],
    SCRIPT_JAPAN: [
        ("YuGothic", r"C:\Windows\Fonts\YuGothM.ttc"),
        ("MSGothic", r"C:\Windows\Fonts\msgothic.ttc"),
        ("Meiryo", r"C:\Windows\Fonts\meiryo.ttc"),
    ],
    SCRIPT_KOREA: [
        ("MalgunGothic", r"C:\Windows\Fonts\malgun.ttf"),
        ("MalgunGothic", r"C:\Windows\Fonts\malgun.ttc"),
        ("Gulim", r"C:\Windows\Fonts\gulim.ttc"),
    ],
    SCRIPT_LATIN_SERIF: [
        ("TimesNewRoman", r"C:\Windows\Fonts\times.ttf"),
        ("Georgia", r"C:\Windows\Fonts\georgia.ttf"),
    ],
    SCRIPT_LATIN_SANS: [
        ("Arial", r"C:\Windows\Fonts\arial.ttf"),
        ("SegoeUI", r"C:\Windows\Fonts\segoeui.ttf"),
    ],
}


# Linux distributions scatter Noto Sans CJK around. List the common
# locations; we just probe in order. ``fc-list`` would be more robust
# but adds a subprocess on every reflow.
_LINUX: Dict[str, List[Tuple[str, str]]] = {
    SCRIPT_HAN: [
        ("NotoSansCJK-SC", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ("NotoSansCJK-SC", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        ("NotoSansCJK-SC", "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"),
        ("NotoSansSC", "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf"),
        ("WQYZenHei", "/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc"),
        ("WQYMicroHei", "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc"),
        ("DroidSansFallback",
         "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
    ],
    SCRIPT_JAPAN: [
        ("NotoSansCJK-JP", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ("NotoSansCJK-JP", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        ("NotoSansCJK-JP", "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"),
        ("NotoSansJP", "/usr/share/fonts/opentype/noto/NotoSansJP-Regular.otf"),
        ("IPAGothic", "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"),
    ],
    SCRIPT_KOREA: [
        ("NotoSansCJK-KR", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ("NotoSansCJK-KR", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        ("NotoSansCJK-KR", "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"),
        ("NotoSansKR", "/usr/share/fonts/opentype/noto/NotoSansKR-Regular.otf"),
        ("NanumGothic", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    ],
    SCRIPT_LATIN_SERIF: [
        ("LiberationSerif",
         "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"),
        ("DejaVuSerif", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
        ("FreeSerif", "/usr/share/fonts/truetype/freefont/FreeSerif.ttf"),
        ("NotoSerif", "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf"),
    ],
    SCRIPT_LATIN_SANS: [
        ("LiberationSans",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("FreeSans", "/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
        ("NotoSans", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
    ],
}


def _candidates_for_platform(platform: str) -> Dict[str, List[Tuple[str, str]]]:
    if platform.startswith("darwin"):
        return _DARWIN
    if platform.startswith("win"):
        return _WIN32
    if platform.startswith("linux") or platform.startswith(("freebsd", "openbsd", "netbsd")):
        return _LINUX
    # Unknown platform: probe nothing and use the bundled fallback. Tests
    # rely on this to pin the resolver to the bundled CID fonts.
    return {}


class CJKFontStore:
    """Per-process resolver for CJK script → concrete font binding.

    Constructable for tests (override ``platform`` and ``extra_candidates``).
    The module-level singleton ``STORE`` is what production code uses.
    """

    def __init__(
        self,
        platform: Optional[str] = None,
        extra_candidates: Optional[Dict[str, List[Tuple[str, str]]]] = None,
    ):
        self._platform = platform or sys.platform
        # extra_candidates are tried first per script (useful for tests
        # that point at a sentinel TTF, and for users who want to override
        # the system font search).
        self._extra = extra_candidates or {}
        self._resolved: Dict[str, CJKFontEntry] = {}

    def resolve(self, script: str) -> CJKFontEntry:
        if script not in _BUNDLED:
            raise ValueError(f"unknown CJK script: {script!r}")
        cached = self._resolved.get(script)
        if cached is not None:
            return cached
        # User-supplied candidates first; then platform defaults.
        for fontname, path in self._extra.get(script, []):
            if path and os.path.exists(path):
                entry = CJKFontEntry(fontname, path, fullwidth=False)
                self._resolved[script] = entry
                return entry
        for fontname, path in _candidates_for_platform(self._platform).get(script, []):
            if path and os.path.exists(path):
                entry = CJKFontEntry(fontname, path, fullwidth=False)
                self._resolved[script] = entry
                return entry
        # No system font matched — fall back to the bundled CID font.
        entry = _BUNDLED[script]
        self._resolved[script] = entry
        return entry

    def by_fontname(self, fontname: str) -> Optional[CJKFontEntry]:
        """Reverse lookup: return the resolved entry that uses ``fontname``,
        or None when ``fontname`` is not a CJK font name we manage."""
        # Force resolution of every script then scan.
        for script in ALL_SCRIPTS:
            entry = self.resolve(script)
            if entry.fontname == fontname:
                return entry
        return None


# Module singleton used by the rest of the codebase.
STORE = CJKFontStore()


# Convenience: the set of fontnames the layout / render code should treat
# as CJK. Includes both bundled (china-s / japan / korea) and whatever
# system font names the store has resolved so far. We seed it eagerly
# with bundled names so checks work even before any resolve() call.
def cjk_fontnames() -> frozenset:
    names = {entry.fontname for entry in (STORE.resolve(s) for s in ALL_SCRIPTS)}
    # Also include the bundled names — STORE may have resolved to system
    # fonts, but other code paths still construct bundled names directly.
    names.update({"china-s", "china-t", "japan", "korea",
                  "china-ss", "china-ts", "japan-s", "korea-s"})
    return frozenset(names)


def fontname_for_script(script: str) -> str:
    """Return the concrete fontname to use for ``script``."""
    return STORE.resolve(script).fontname


def font_entry_for_fontname(fontname: str) -> Optional[CJKFontEntry]:
    """Reverse lookup; returns None for non-CJK fontnames."""
    return STORE.by_fontname(fontname)

"""Shared recognition of fonts whose glyphs must retain mathematical layout."""

from functools import lru_cache

_MATH_FONT_PREFIXES = (
    "CMMI", "CMSY", "CMEX", "MSAM", "MSBM", "LMMI", "LMSY", "LMEX",
    "RSFS", "EUSM", "EUSB", "EUEX", "EUFM", "EURB", "EURM", "STIXMath", "STIX-Math",
)


@lru_cache(maxsize=1024)
def is_math_font(name: str) -> bool:
    if not name:
        return False
    if "Math" in name and "MathJax" not in name:
        return True
    head = name.split("+", 1)[-1].split("-", 1)[0]
    return (any(head.upper().startswith(p.upper()) for p in _MATH_FONT_PREFIXES)
            or head in {"Symbol", "SymbolMT", "StandardSymL", "OpenSymbol"})

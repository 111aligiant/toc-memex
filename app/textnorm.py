from __future__ import annotations

import html
import re

"""Normalize OCR/PDF-extraction artifacts into plain, searchable text.

PaddleOCR-VL returns inline math as Markdown/LaTeX (``$ ... $``, ``\\pm``,
``^{\\circ}``, ``ACME_{1}.2``). The raw response is kept in
``data/ocr/<doc_id>/raw.jsonl``; this module only cleans the text that goes
into pages, TOC nodes, chunks, summaries and BM25.

Policy:
- math delimiters are removed, the content is kept;
- simple sub/superscripts become plain ``_x`` / ``^x``; degree signs and
  footnote superscripts are handled explicitly;
- a fixed list of common commands is mapped to Unicode/plain text; unknown
  commands are left untouched instead of being silently deleted;
- standalone footnote markers such as ``$ ^{a} $`` are dropped;
- no global brace stripping, so set notation and ordinary prose survive.
"""

_GREEK = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "varepsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "phi": "φ",
    "varphi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
}

_SYMBOLS = {
    "pm": "±",
    "mp": "∓",
    "times": "×",
    "div": "÷",
    "cdot": "·",
    "cdots": "…",
    "ldots": "…",
    "dots": "…",
    "le": "≤",
    "leq": "≤",
    "leqslant": "≤",
    "ge": "≥",
    "geq": "≥",
    "geqslant": "≥",
    "neq": "≠",
    "ne": "≠",
    "approx": "≈",
    "equiv": "≡",
    "sim": "~",
    "propto": "∝",
    "infty": "∞",
    "sum": "∑",
    "prod": "∏",
    "int": "∫",
    "partial": "∂",
    "nabla": "∇",
    "degree": "°",
    "circ": "°",
    "rightarrow": "→",
    "to": "→",
    "leftarrow": "←",
    "Rightarrow": "⇒",
    "langle": "⟨",
    "rangle": "⟩",
    "mid": "|",
    "bullet": "•",
    "ast": "*",
    "star": "*",
    "prime": "′",
    "wedge": "∧",
    "vee": "∨",
    "neg": "¬",
    "forall": "∀",
    "exists": "∃",
    "in": "∈",
    "notin": "∉",
    "subset": "⊂",
    "supset": "⊃",
    "cup": "∪",
    "cap": "∩",
    "emptyset": "∅",
    "angle": "∠",
    "perp": "⊥",
    "parallel": "∥",
    "because": "∵",
    "therefore": "∴",
    "upmu": "μ",
    "mu": "μ",  # stray unit forms like \muV handled by \mu + literal V
    "permil": "‰",
    "square": "□",
    "Box": "□",
    "degree_celsius": "℃",
    "sup": "^sup",  # parameterless OCR artifact: M_dB\sup -> M_dB^sup
    "lg": "lg",
    "log": "log",
    "ln": "ln",
    "max": "max",
    "min": "min",
    "lim": "lim",
    "sin": "sin",
    "cos": "cos",
    "tan": "tan",
    "quad": " ",
    "qquad": " ",
}

# Commands whose braced argument is plain content.
_TEXT_COMMANDS = re.compile(
    r"\\(?:text|textrm|mathrm|mathbf|mathit|mathcal|mathsf|mathtt|"
    r"operatorname|boldsymbol|boldmath|underline|overline|hat|dot|bar|"
    r"vec|tilde|widehat|widetilde|overset|underset|overleftrightarrow|"
    r"overrightarrow|overleftarrow|scriptsize|big|Big|bigg|Bigg|"
    r"bigl|bigr|Bigl|Bigr|biggl|biggr)\s*\{([^{}]*)\}"
)
_FRAC = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
_SQRT = re.compile(r"\\sqrt\{([^{}]*)\}")
_SPACING = re.compile(r"\\[,;:! ]|\\q?quad")
# Parameterless style/size/delimiter commands: no content, safe to drop.
# ``\scriptsize``, ``\boldmath``, ``\Big``, ``\bigg`` and friends appear
# without arguments in OCR output ("N_\scriptsize~x"). Adjacent ``~`` (TeX
# spacing) is consumed with the command; the lookahead keeps ``\bigg/``
# from being confused with ``\bigg`` + a longer command.
_STYLE_COMMANDS = re.compile(
    r"~?\\(?:scriptsize|tiny|footnotesize|small|normalsize|large|Large|LARGE|"
    r"huge|Huge|boldmath|unboldmath|bf|it|rm|sf|tt|sl|sc|itshape|"
    r"bfseries|displaystyle|limits|nolimits|big|Big|bigg|Bigg|bigl|bigr|"
    r"Bigl|Bigr|biggl|biggr|Biggl|Biggr|bigm|biggm|Bigm|"
    r"left|right|middle)(?![A-Za-z])~?"
)
_NEWLINE_BACKSLASH = re.compile(r"\\\\(?:\s*)")
# TeX thin-space ``~`` next to punctuation, but never inside a plain range
# like ``171~245`` or ``20 mm ~ 25.4 mm``: those keep their tilde.
_TILDE_AFTER_BREAK = re.compile(r"(?<=[,(（])~(?=[A-Za-z0-9])")
_TILDE_BEFORE_PUNCT = re.compile(
    r"(?<=[A-Za-z0-9À-῿‐-‧])~(?=[,;)\]}=+\s]|$)"
)
_ESCAPES = {
    r"\_": "_",
    r"\%": "%",
    r"\&": "&",
    r"\#": "#",
    r"\$": "$",
    r"\{": "{",
    r"\}": "}",
}
_MATH_SIGNAL = re.compile(r"[\\^_=<>]|[±×÷≤≥≈∞√∑∏∫ΔΩμ°]")

# Broken PDF fonts map Latin glyphs onto CJK ideographs ("犌犅／犜" for
# "ABC", "犽犵" for "kg"). These ideographs (犭-radical lookalikes) do not
# occur in real Chinese text; mapping them back restores searchable text.
# Each pair below is verified against corpus context, e.g.
# 狑犪狋犲狉狆狉狅狅犳狀犲狊狊 = waterproofness.
_FAKE_LATIN = {
    "犃": "A", "犅": "B", "犆": "C", "犇": "D", "犈": "E", "犉": "F",
    "犌": "G", "犎": "H", "犐": "I", "犑": "J", "犓": "K", "犔": "L",
    "犕": "M", "犖": "N", "犗": "O", "犘": "P", "犙": "Q", "犚": "R",
    "犛": "S", "犜": "T", "犝": "U", "犞": "V", "犠": "W", "犡": "X",
    "犢": "Y", "犣": "Z",
    "犪": "a", "犫": "b", "犮": "c", "犱": "d", "犲": "e", "犳": "f",
    "犵": "g", "犺": "h", "犻": "i", "犼": "j", "犽": "k", "犾": "l",
    "犿": "m", "狀": "n", "狅": "o", "狆": "p", "狇": "q", "狉": "r",
    "狊": "s", "狋": "t", "狌": "u", "狏": "v", "狑": "w", "狓": "x",
    "狔": "y", "狕": "z",
}
# Fullwidth digits/letters and math signs read better halfwidth and match
# ASCII queries; Chinese punctuation keeps its fullwidth forms. The
# fullwidth tilde is kept too: it is the range separator in Chinese
# standards ("10～21d") and an ASCII "~" would be eaten by the TeX
# thin-space rules below.
_WIDTH_CHARS = {
    "．": ".", "／": "/", "－": "-", "＋": "+", "＝": "=",
    "＜": "<", "＞": ">", "％": "%", "　": " ",
}
_ENCODING_TABLE = str.maketrans(
    {
        **_FAKE_LATIN,
        **_WIDTH_CHARS,
        **{chr(0xFF10 + i): str(i) for i in range(10)},
        **{chr(0xFF21 + i): chr(ord("A") + i) for i in range(26)},
        **{chr(0xFF41 + i): chr(ord("a") + i) for i in range(26)},
    }
)


def normalize_encoding_artifacts(text: str) -> str:
    """Undo font/width encoding damage; safe and cheap on clean text."""
    if not text:
        return text
    return text.translate(_ENCODING_TABLE)
_STANDALONE_SUP = re.compile(r"\s*\^\s*(?:\{\s*)?\\?[a-zA-Z*]?(?:\s*\})?\s*")
# OCR splits a parenthesized variable across math delimiters:
# ``$ (T $，单位为 °C)`` — the opening paren goes inside, the close stays
# outside. Unwrap the broken half; rejoining it is not recoverable.
_BROKEN_PAREN = re.compile(r"\$\s*\(([A-Za-z][A-Za-z0-9_\^{}]*)\s*\$")
_LITERAL_ESCAPE = re.compile(r"\\[nrt]")
_DOUBLE_BRACES = re.compile(r"\{\{([^{}]*)\}\}")
# Acronym identifiers: ACME_{1}.2 -> ACME1.2 (not ACME_1.2).
_ACRONYM_SUB = re.compile(r"\b([A-Z]{2,})_\{(\d+)\}")
# Environment wrappers: \begin{aligned}...\end{aligned} -> ...
_ENVIRONMENTS = re.compile(
    r"\\(?:begin|end)\s*\{[^{}]*\}", re.IGNORECASE
)
# A single token with optional unit, sub/superscripts or primes: OCR wraps
# these in $...$ though they are ordinary variables ("$ A $", "$ f/(Hz) $").
_PLAIN_VARIABLE = re.compile(
    r"[A-Za-zΔΩμπφ](?:\s*[/'^_.]\s*[A-Za-z0-9()\[\]{}]+)*"
    r"(?:\s*/\s*\(?[A-Za-z0-9]+\))?"
    r"|\(n\)|\w'\s?|[A-Za-z]{1,3}\s?h"
    r"|\(g\)|\([q]\)|\(log/log\)|log/log|[A-Za-z]+\([A-Za-z]+\)(?:\s*——)?|[A-Z]+\([N]\)"
    r"|[A-Za-z]\(\s*[+\-−]?[\d.]+\s*(?:dB|dB)?[A-Za-z]*\s*\)"
    r"|\d[\d.,~ ]*\s*[kKmM]?[Hh]z|[\d.]+\s*~?\s*km/h|\d+\s*Hz"
    r"|[+-]?[\d.]+\s*[cmk]?m/h|[+-]?[\d.]+\s*[cmk]?m\b|\d+\s*[kK]?g\b"
    r"|\d+\s*h\b|\d+\s*d\b|\(11000-h\)/6342\.0|M>1\.0|G\s*>\s*150"
    r"|Hz|kHz|MHz|GHz|（m）"
    # 2026-11-12 residual sweep: quantities, chemical formulas, subscripted
    # variables and simple variable arithmetic that OCR wrapped in $...$.
    r"|\d[\d.,]*\s*(?:ms|s|min|h|GHz|MHz|kHz|Hz|m/s|mm|cm|m|dB|d)\b"
    r"|\d[\d.,]*\s*~\s*\d[\d.,]*\s*(?:mm|cm|m|s|min|GHz|MHz|kHz|Hz)\b"
    r"|[A-Z][a-z]?(?:_{?[A-Za-z0-9]+}?)?(?:\s*[+\-−]\s*[A-Z][a-z]?(?:_{?[A-Za-z0-9]+}?)?)+"
    r"|[A-Z]{1,2}[a-z]?\d?(?:_{?[A-Za-z0-9]+}?)?(?:[A-Z]{1,2}[a-z]?\d?(?:_{?[A-Za-z0-9]+}?)?)*"
)


def _reduce_double_braces(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _DOUBLE_BRACES.sub(r"{\1}", text)
    return text


def _superscript(inner: str) -> str:
    value = inner.strip()
    if value in {"\\circ", "circ", "°"}:
        return "°"
    if value in {"*", "\\ast"}:
        return "*"
    return "^" + value


def _subscript(inner: str) -> str:
    return "_" + inner.strip()


_FRAC_OPERATORS = re.compile(r"[+\-−=×·/]")


def _fraction(match: re.Match[str]) -> str:
    numerator = match.group(1).strip()
    denominator = match.group(2).strip()
    if _FRAC_OPERATORS.search(numerator):
        numerator = f"({numerator})"
    if _FRAC_OPERATORS.search(denominator):
        denominator = f"({denominator})"
    return f"{numerator}/{denominator}"


_ALL_SYMBOLS = {**_SYMBOLS, **_GREEK}
_LONGEST_COMMAND = max(len(command) for command in _ALL_SYMBOLS)


def _lookup_command(command: str) -> str:
    """Resolve ``\\muV`` as ``\\mu`` + ``V``; leave unknown commands alone."""
    for length in range(min(_LONGEST_COMMAND, len(command)), 0, -1):
        symbol = _ALL_SYMBOLS.get(command[:length])
        if symbol is not None:
            return symbol + command[length:]
    return "\\" + command


def _math_span(match: re.Match[str]) -> str:
    inner = match.group(2).strip()
    if not inner:
        return ""
    # ``$ ^{a} $`` / ``$ ^{*} $`` are footnote markers, not formulas.
    if _STANDALONE_SUP.fullmatch(inner):
        return ""
    # ``$ A $``, ``$ (n) $``, ``$ f/(Hz) $``: a short symbol or a symbol with
    # a unit is a variable the OCR wrapped in math delimiters.
    if _PLAIN_VARIABLE.fullmatch(inner):
        return inner
    # OCR splits quantities: "$ 10 $d" — a bare number inside, its unit
    # letter right after the closing delimiter. Peek ahead; a letter there
    # marks the number as a quantity, not currency.
    if inner.isdigit() and match.end() < len(match.string):
        if match.string[match.end()].isalpha():
            return inner
    if match.group(1) == "$$" or _MATH_SIGNAL.search(inner):
        return inner
    # A lone ``$`` pair without math signals may be currency ("$5 - $10").
    return match.group(0)


def normalize_text(text: str) -> str:
    """Clean OCR math artifacts; safe to run more than once."""
    if not text:
        return text
    # Font/width damage is independent of math artifacts and must run even
    # when the early return below would fire ("２４ｈ" has no math markers).
    text = normalize_encoding_artifacts(text)
    if ("$" not in text and "\\" not in text and "^{" not in text
            and "_{" not in text and "{{" not in text and "&#" not in text):
        return text
    # Do not rewrite fenced code blocks.
    if "```" in text:
        return text

    # OCR tables embed HTML entities inside markdown ("$ A&#x27; $"); decode
    # them first or the entity breaks the math-span pattern around it.
    if "&#" in text or "&amp;" in text or "&lt;" in text or "&gt;" in text:
        text = html.unescape(text)

    text = _reduce_double_braces(text)
    text = _BROKEN_PAREN.sub(r"(\1", text)
    text = re.sub(r"(\${1,2})([^\n$]+?)\1", _math_span, text)
    # A leftover unpaired ``$`` at end of line: the opening/closing partner
    # was consumed or lost by OCR. Keep the text, drop the delimiter.
    text = re.sub(r"\$[ \t]*$", "", text, flags=re.MULTILINE)
    # Chunking can split a ``$ ... $`` span across chunk boundaries, leaving
    # a lone delimiter at a line start (``$。在试件...``). Drop it.
    text = re.sub(r"^[ \t]*\$\s*", "", text, flags=re.MULTILINE)
    text = _ENVIRONMENTS.sub("", text)
    # OCR line breaks and math-mode spaces inside formulas: ``\\`` and ``~``.
    text = _NEWLINE_BACKSLASH.sub(" ", text)
    text = _TILDE_AFTER_BREAK.sub(" ", text)
    text = _TILDE_BEFORE_PUNCT.sub(" ", text)
    # Parameterless style commands, repeatedly: dropping one may reveal
    # another ("\\Big\\Big("). Replaced with a space to keep token
    # separation ("N_\\scriptsize~x" -> "N_ x"); a following "/" or "(" is
    # untouched (negative lookahead keeps ``\bigg/`` from eating the slash).
    for _ in range(5):
        previous = text
        text = _STYLE_COMMANDS.sub(" ", text)
        if text == previous:
            break
    text = text.replace(r"\left", "").replace(r"\right", "")
    text = text.replace(r"\displaystyle", "").replace(r"\limits", "")
    text = text.replace(r"\nolimits", "").replace(r"\notag", "")

    # Nested TeX arguments need several passes: unwrap \\mathrm, then the
    # surrounding subscript, then the fraction that contains both.
    # Unit compounds (\\muV -> μV) map the command letter plus any trailing
    # letters; unknown commands keep their trailing letters untouched.
    for _ in range(5):
        previous = text
        text = _TEXT_COMMANDS.sub(r"\1", text)
        text = _ACRONYM_SUB.sub(r"\1\2", text)
        text = re.sub(r"\^\{([^{}]*)\}", lambda m: _superscript(m.group(1)), text)
        text = re.sub(r"_\{([^{}]*)\}", lambda m: _subscript(m.group(1)), text)
        text = re.sub(
            r"\\([A-Za-z]+)",
            lambda m: _lookup_command(m.group(1)),
            text,
        )
        text = _FRAC.sub(_fraction, text)
        text = _SQRT.sub(r"√\1", text)
        text = _SPACING.sub(" ", text)
        if text == previous:
            break

    for escaped, plain in _ESCAPES.items():
        text = text.replace(escaped, plain)

    text = _LITERAL_ESCAPE.sub(" ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([，。；：！？、,.!?;:])", r"\1", text)
    return text

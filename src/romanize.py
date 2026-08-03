"""Rule-based Devanagari to Roman fallback.

Only used when Gemini fails twice to produce a word-count-matched Hinglish
caption. The output is clumsier than what the model writes, but it maps one
input word to exactly one output word, which is the property caption timing
actually depends on.
"""

from __future__ import annotations

CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "ळ": "l",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r",
    "ढ़": "rh", "फ़": "f",
}

VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo",
    "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
}

MATRAS = {
    "ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
}

DIGITS = {"०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
          "५": "5", "६": "6", "७": "7", "८": "8", "९": "9"}

VIRAMA = "्"
NASALS = {"ं": "n", "ँ": "n"}
VISARGA = "ः"
NUKTA = "़"

# Devanagari sentence terminators have no Latin glyph; map them to a period.
PUNCT = {"।": ".", "॥": "."}


def _romanize_word(word: str) -> str:
    out: list[str] = []
    i = 0
    n = len(word)
    while i < n:
        ch = word[i]

        # Fold a following nukta into the base character when the combination
        # has a distinct romanisation (ज + ़ -> z).
        if i + 1 < n and word[i + 1] == NUKTA and (ch + NUKTA) in CONSONANTS:
            ch = ch + NUKTA
            i += 1

        if ch in CONSONANTS:
            out.append(CONSONANTS[ch])
            nxt = word[i + 1] if i + 1 < n else ""
            if nxt == VIRAMA:
                i += 2
                continue
            if nxt in MATRAS:
                out.append(MATRAS[nxt])
                i += 2
                continue
            out.append("a")  # inherent vowel
            i += 1
            continue

        if ch in VOWELS:
            out.append(VOWELS[ch])
        elif ch in NASALS:
            out.append(NASALS[ch])
        elif ch == VISARGA:
            out.append("h")
        elif ch in DIGITS:
            out.append(DIGITS[ch])
        elif ch in PUNCT:
            out.append(PUNCT[ch])
        elif ch in (NUKTA, VIRAMA):
            pass
        else:
            out.append(ch)  # punctuation and Latin pass through
        i += 1

    word_out = "".join(out)
    # Hindi drops the final inherent vowel (राम is "raam", not "raama").
    if len(word_out) > 2 and word_out.endswith("a") and not word_out.endswith("aa"):
        word_out = word_out[:-1]
    return word_out


def fallback_romanize(text: str) -> str:
    """Romanise while preserving the exact whitespace token count."""
    return " ".join(_romanize_word(w) for w in text.split())

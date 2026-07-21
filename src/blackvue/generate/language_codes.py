"""
Short (3-letter) language codes for filenames.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

#
# Maps the 2-letter (ISO 639-1) codes used by faster-whisper and
# argos-translate to 3-letter (ISO 639-2/T) codes, for use in
# generated filenames. Covers every language faster-whisper can
# detect. A couple of Whisper's own codes (e.g. "haw", "yue") have
# no ISO 639-1 form and are already 3 letters - they map to
# themselves so lookups stay uniform.
#
ISO_639_1_TO_3 = {
    "af": "afr", "am": "amh", "ar": "ara", "as": "asm", "az": "aze",
    "ba": "bak", "be": "bel", "bg": "bul", "bn": "ben", "bo": "bod",
    "br": "bre", "bs": "bos", "ca": "cat", "cs": "ces", "cy": "cym",
    "da": "dan", "de": "deu", "el": "ell", "en": "eng", "es": "spa",
    "et": "est", "eu": "eus", "fa": "fas", "fi": "fin", "fo": "fao",
    "fr": "fra", "gl": "glg", "gu": "guj", "ha": "hau", "haw": "haw",
    "he": "heb", "hi": "hin", "hr": "hrv", "ht": "hat", "hu": "hun",
    "hy": "hye", "id": "ind", "is": "isl", "it": "ita", "ja": "jpn",
    "jw": "jav", "ka": "kat", "kk": "kaz", "km": "khm", "kn": "kan",
    "ko": "kor", "la": "lat", "lb": "ltz", "ln": "lin", "lo": "lao",
    "lt": "lit", "lv": "lav", "mg": "mlg", "mi": "mri", "mk": "mkd",
    "ml": "mal", "mn": "mon", "mr": "mar", "ms": "msa", "mt": "mlt",
    "my": "mya", "ne": "nep", "nl": "nld", "nn": "nno", "no": "nor",
    "oc": "oci", "pa": "pan", "pl": "pol", "ps": "pus", "pt": "por",
    "ro": "ron", "ru": "rus", "sa": "san", "sd": "snd", "si": "sin",
    "sk": "slk", "sl": "slv", "sn": "sna", "so": "som", "sq": "sqi",
    "sr": "srp", "su": "sun", "sv": "swe", "sw": "swa", "ta": "tam",
    "te": "tel", "tg": "tgk", "th": "tha", "tk": "tuk", "tl": "tgl",
    "tr": "tur", "tt": "tat", "uk": "ukr", "ur": "urd", "uz": "uzb",
    "vi": "vie", "yi": "yid", "yo": "yor", "yue": "yue", "zh": "zho",
}


def short_code(language: str) -> str:
    """Return a 3-letter filename-friendly code for a language.

    Falls back to the input itself (lowercased) if it is not in the
    table, so an unrecognized code still produces a usable filename
    instead of raising.
    """

    language = language.strip().lower()

    return ISO_639_1_TO_3.get(language, language)


# Reverse of ISO_639_1_TO_3, for turning a filename's 3-letter code
# back into the 2-letter form Whisper/argos-translate expect.
LONG_CODE = {short: long for long, short in ISO_639_1_TO_3.items()}


def normalize_language(code: str) -> str:
    """Normalize a language code to the 2-letter form Whisper and
    argos-translate expect.

    Accepts either that 2-letter code ('en', 'sv', 'th', ...) or the
    3-letter code used in generated filenames ('eng', 'swe', 'tha',
    ...), so --language/--translate work with whichever one comes to
    mind. Falls back to the input unchanged if it matches neither
    table.
    """

    code = code.strip().lower()

    if code in ISO_639_1_TO_3:
        return code

    return LONG_CODE.get(code, code)

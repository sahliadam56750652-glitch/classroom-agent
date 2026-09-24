"""What a document is called once it leaves this machine.

Two pure functions with no transport in them, which is why they live here rather
than inside one. They used to sit in `notify/telegram.py` and `gate/messages.py`,
and both of those still re-export them, so every existing import keeps working.

They moved because the HTTP API serves the same documents and has to name them
the same way -- and `gate/messages.py` imports `gate/quiz.py`, which imports
`llm/provider.py` and `files/packs.py`. Reaching one naming function that way
would have pulled a model client and the pack builder into a process that must
be unable to call either. The alternative was a second implementation of the
naming rule, and CLAUDE.md is explicit that there is one:

    A document is delivered under its Drive title, not its Drive id. A
    `file_id` keeps the filename it was uploaded with, so changing that rule
    means `DELETE FROM telegram_files`.

That sentence is only true while one function decides it.
"""

from __future__ import annotations

from pathlib import Path

# Windows forbids these outright and Telegram's Content-Disposition cannot carry
# a quote, so they are replaced with a space rather than stripped -- "Chapter
# 1/2" becoming "Chapter 1 2" keeps both halves of what the name was meant to
# say. Control characters are here for a second reason: a CR or LF in a filename
# would end the Content-Disposition line early and let the rest of the name be
# read as another header.
_ILLEGAL_IN_FILENAMES = set('<>:"/\\|?*') | {chr(code) for code in range(32)}

# CON.pdf is not a file you can save on Windows. Vanishingly unlikely from a
# Drive title, and three lines to be sure of.
_RESERVED_STEMS = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{digit}" for prefix in ("com", "lpt") for digit in range(1, 10)
}

# Well inside every filesystem's limit, and short enough to read on a phone.
MAX_FILENAME = 120


def safe_filename(name: str, *, fallback: str = "attachment") -> str:
    """A Drive title turned into something a phone can actually save.

    Applied by `telegram._multipart` to whatever it is handed, so a caller cannot
    break the multipart headers with a quote or a newline however careless it is,
    and by the API's Content-Disposition for the same reason. Idempotent, so
    composing a name and then sanitising it again is free.
    """
    cleaned = "".join(
        " " if char in _ILLEGAL_IN_FILENAMES else char for char in str(name or "")
    )
    # Leading dots hide the file; trailing dots and spaces are silently dropped
    # by Windows, which turns "Chapter 1." into a name that does not round-trip.
    cleaned = " ".join(cleaned.split()).strip(". ")
    if not cleaned:
        return fallback

    stem, dot, suffix = cleaned.rpartition(".")
    if not dot:
        stem, suffix = cleaned, ""
    if stem.lower() in _RESERVED_STEMS:
        stem = f"_{stem}"

    room = MAX_FILENAME - len(suffix) - (1 if suffix else 0)
    if room < 1:
        # A pathological "name" that is all extension. Keep the front of it.
        return cleaned[:MAX_FILENAME]
    stem = stem[:room].rstrip(". ") or fallback
    return f"{stem}.{suffix}" if suffix else stem


def document_filename(title: str, path) -> str:
    """What a lecture should be called once it is on my phone.

    The Drive title, with the extension of the bytes actually being sent. Those
    two can disagree: a Google-native document has no extension in Drive and is
    exported to PDF locally, so "Chapter 1" has to become "Chapter 1.pdf" or the
    phone will not know what to open it with. Where the title already carries a
    different extension the real one is appended rather than substituted -- the
    file opens, and what it was called is still visible.
    """
    actual = Path(path).suffix
    name = safe_filename(title, fallback=Path(path).stem or "attachment")
    if actual and not name.lower().endswith(actual.lower()):
        name = f"{name}{actual}"
    return safe_filename(name, fallback=Path(path).name)

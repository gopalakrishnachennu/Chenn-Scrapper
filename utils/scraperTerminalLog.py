"""
Unified stderr logging for JobRight, Glassdoor, ZipRecruiter, and LinkedIn scrapers.

Terminal colors apply when stderr is a TTY. Disable with ``NO_COLOR=1`` (or any
non-empty value). Force colors when piped with ``FORCE_COLOR=1`` if your pager
supports ANSI.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone

PLATFORM_JOBRIGHT = "JobRight"
PLATFORM_GLASSDOOR = "Glassdoor"
PLATFORM_ZIPRECRUITER = "ZipRecruiter"
PLATFORM_LINKEDIN = "LinkedIn"
PLATFORM_MIDHTECH = "Midhtech"

_PROGRESS_LINE_PAD = 120

# --- ANSI (TTY only; respect NO_COLOR) -----------------------------------------
_RST = "\033[0m"
_DIM = "\033[2m"
_BLD = "\033[1m"
_GRAY = "\033[90m"
_BCYN = "\033[96m"
_BYEL = "\033[93m"
_BRED = "\033[91m"
_BMAG = "\033[95m"
_BGRN = "\033[92m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _stripAnsi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _useColor(file) -> bool:
    if os.environ.get("NO_COLOR", "").strip():
        return False
    if os.environ.get("FORCE_COLOR", "").strip():
        return True
    try:
        return bool(file.isatty())
    except (AttributeError, ValueError):
        return False


def _utcTime() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _levelStyle(level: str) -> str:
    return {
        "INFO": _BCYN,
        "WARN": _BYEL,
        "ERROR": _BRED,
        "DEBUG": _GRAY + _DIM,
        "PROGRESS": _BMAG,
    }.get(level, _BCYN)


def _formatStyledLine(
    *,
    platform: str,
    phasePart: str,
    level: str,
    message: str,
    color: bool,
) -> str:
    ts = _utcTime()
    plain = f"{ts} [{platform}] {phasePart} {level:5} {message}"
    if not color:
        return plain
    ls = _levelStyle(level)
    return (
        f"{_DIM}{ts}{_RST} "
        f"{_BLD}{_BGRN}[{platform}]{_RST} "
        f"{_DIM}{phasePart}{_RST} "
        f"{ls}{_BLD}{level:5}{_RST} "
        f"{message}"
    )


def formatPushResultSuffix(info: str) -> str:
    """
    Parenthesized detail after APPLIED/REDO (e.g. ``HTTP 200``).
    Greens 2xx, reds 4xx/5xx; long error tails stay dim. Respects NO_COLOR / TTY.
    """
    raw = (info or "").strip()
    if not raw:
        return ""
    if not _useColor(sys.stderr):
        return f"({raw})"
    if raw.startswith("HTTP "):
        parts = raw.split()
        try:
            code = int(parts[1])
        except (IndexError, ValueError):
            return f"{_DIM}({raw}){_RST}"
        if 200 <= code < 300:
            inner = f"{_BGRN}{raw}{_RST}"
        elif code >= 400:
            inner = f"{_BRED}{raw}{_RST}"
        else:
            inner = f"{_BYEL}{raw}{_RST}"
        return f"{_DIM}({_RST}{inner}{_DIM}){_RST}"
    return f"{_DIM}({raw}){_RST}"


def formatApplyStatusBadge(status: str) -> str:
    """
    Colorize classifier / DB status for one-line validate output (APPLY, EXISTING, etc.).
    Respects NO_COLOR / TTY the same as ScraperRunLog.
    """
    raw = (status or "").strip()
    if not raw:
        return ""
    if not _useColor(sys.stderr):
        return raw
    u = raw.upper().replace(" ", "_")
    if u == "APPLY":
        return f"{_BGRN}{raw}{_RST}"
    if u == "EXISTING":
        return f"{_BCYN}{raw}{_RST}"
    if u in ("DO_NOT_APPLY", "DONOTAPPLY") or u.startswith("DO_NOT"):
        return f"{_BYEL}{raw}{_RST}"
    if u == "APPLIED":
        return f"{_BGRN}{raw}{_RST}"
    if u == "REDO":
        return f"{_BMAG}{raw}{_RST}"
    return f"{_DIM}{raw}{_RST}"


class ScraperRunLog:
    """
    Terminal log lines: ``HH:MM:SS [Platform] [phase] LEVEL message``.
    On a color TTY: dim time, green platform tag, level-colored badge, plain message.
    Set NO_COLOR=1 to disable. WARN/ERROR optionally mirror to scrape-*.log (plain text).
    """

    __slots__ = ("platform", "phaseLabel", "mirrorToScrapeLog")

    def __init__(
        self,
        platform: str,
        phaseLabel: str = "",
        *,
        mirrorToScrapeLog: bool = True,
    ) -> None:
        self.platform = platform
        self.phaseLabel = (phaseLabel or "").strip()
        self.mirrorToScrapeLog = mirrorToScrapeLog

    def bindPhase(self, phaseLabel: str) -> ScraperRunLog:
        self.phaseLabel = (phaseLabel or "").strip()
        return self

    def _phasePart(self) -> str:
        return f"[{self.phaseLabel}]" if self.phaseLabel else "[-]"

    def _emit(
        self,
        level: str,
        message: str,
        *,
        file=sys.stderr,
    ) -> None:
        phasePart = self._phasePart()
        useColor = _useColor(file)
        styled = _formatStyledLine(
            platform=self.platform,
            phasePart=phasePart,
            level=level,
            message=message,
            color=useColor,
        )
        print(styled, file=file, flush=True)
        if (
            self.mirrorToScrapeLog
            and level in ("WARN", "ERROR")
            and message.strip()
        ):
            try:
                from .dataManager import appendScrapeLog

                appendScrapeLog(
                    f"{level} {phasePart} {message}",
                    platform=self.platform,
                )
            except Exception:
                pass

    def debug(self, message: str) -> None:
        self._emit("DEBUG", message)

    def info(self, message: str) -> None:
        self._emit("INFO", message)

    def warning(self, message: str) -> None:
        self._emit("WARN", message)

    def error(self, message: str) -> None:
        self._emit("ERROR", message)

    def phaseStart(self, phaseNum: int, phaseTotal: int, label: str, mode: str) -> None:
        self.info(f"Phase {phaseNum}/{phaseTotal} — {label!r} — {mode}")

    def existingJobsNotice(self, count: int, sourceFileName: str) -> None:
        if count > 0:
            self.info(
                f"{count} jobId(s) already in {sourceFileName}; "
                "those list rows will be skipped."
            )

    def jobLine(self, index: int, total: int, message: str) -> None:
        self.info(f"[{index}/{total}] {message}")

    def jobSkip(self, index: int, total: int, reason: str, detail: str = "") -> None:
        msg = f"[{index}/{total}] skip ({reason})"
        if detail:
            msg += f": {detail}"
        self.info(msg)

    def jobError(self, index: int, message: str, exc: BaseException | None = None) -> None:
        if exc is not None:
            self.error(f"[{index}] {message}: {exc}")
        else:
            self.error(f"[{index}] {message}")

    def mergeCheckpoint(
        self,
        pathDisplay: str,
        added: int,
        mergeSkipped: int,
        extra: str = "",
    ) -> None:
        tail = f" {extra}" if extra else ""
        self.info(
            f"merge checkpoint → {pathDisplay}: +{added} new, {mergeSkipped} merge-skip{tail}"
        )

    def phaseDone(self, label: str, message: str) -> None:
        self.info(f"Phase {label!r} done: {message}")

    def runDone(self, message: str) -> None:
        self.info(f"Run complete — {message}")

    def driverRetry(self, attempt: int, maxAttempts: int, exc: BaseException) -> None:
        self.warning(f"driver.get failed ({exc!r}); retry {attempt}/{maxAttempts - 1}")

    def httpErrorBody(self, body: str) -> None:
        self.error(f"HTTP response body (truncated):\n{body}")

    # --- Live progress (TTY carriage return) — JobRight list scroll -----------------
    def progressBodyLine(
        self,
        body: str,
        *,
        finalize: bool = False,
        pad: int = _PROGRESS_LINE_PAD,
    ) -> None:
        """
        Same prefix as other logs, level PROGRESS; updates one line on a TTY when
        finalize is False. Skips noisy sleep ticks when stderr is not a TTY.
        """
        file = sys.stderr
        useColor = _useColor(file)
        ts = _utcTime()
        phasePart = self._phasePart()
        level = "PROGRESS"
        if useColor:
            ls = _levelStyle(level)
            prefix = (
                f"{_DIM}{ts}{_RST} "
                f"{_BLD}{_BGRN}[{self.platform}]{_RST} "
                f"{_DIM}{phasePart}{_RST} "
                f"{ls}{_BLD}{level:5}{_RST} "
            )
        else:
            prefix = f"{ts} [{self.platform}] {phasePart} {level:5} "
        line = f"{prefix}{body}"
        padLen = max(0, pad - len(_stripAnsi(line)))
        padded = f"{line}{' ' * padLen}"
        tty = file.isatty()
        end = "\n" if finalize or not tty else "\r"
        out = padded + (_RST if useColor else "")
        print(out, end=end, file=file, flush=True)

    def siteDetail(self, message: str) -> None:
        """Glassdoor load-more, Zip wait messages, etc. — same format, INFO."""
        self.info(f"[site] {message}")

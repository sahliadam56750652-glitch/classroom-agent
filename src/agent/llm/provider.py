"""The single seam between this project and a language model.

Everything that calls a model goes through `LLMProvider`. Phase 3's briefing
prose and quiz generation will implement the same interface, so swapping Gemini
for something else stays a change to one file rather than a change to the gate.

Plain HTTP over urllib, not the vendor SDK. The same reasoning as notify/
telegram.py: the calls here are one POST with a JSON body, the retry policy has
to be ours because quota exhaustion is a state the caller must be told about
rather than an exception to swallow, and a personal tool that has to run on a
free-tier ARM box benefits from one less dependency.

Errors are split by what the caller can do about them, which is the whole point
of having them:

  LLMAuthError       the key is wrong or unauthorised. Never retried, always
                     raised -- a misconfigured key that degraded silently would
                     leave the library quietly untranscribed for weeks.
  LLMQuotaError      the free tier is spent. Expected, not exceptional: the
                     caller records the work as pending and tries tomorrow.
  LLMUnavailable     network or 5xx, after retries. Also recoverable later.
  LLMTimeout         a kind of LLMUnavailable, kept separate so a run can say
                     how many pages timed out instead of hiding them.
  LLMRefused         the model returned nothing usable for this input.

Every one of these is raised deliberately. Nothing from urllib, ssl, socket or
http.client is allowed out of _http_post unclassified: an unhandled network
condition escaping as a traceback kills a whole batch, which is how a read
timeout once ended a twenty-page run on its first page.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Flash-class: this transcribes a few hundred lecture pages, and the Pro-class
# models cost more and are slower for no gain on legible printed material.
#
# Model retirement is a recurring event, not an anomaly -- gemini-2.5-flash was
# withdrawn from new keys and answered every request with a 404 naming its
# replacement. So this is only the default: set GEMINI_MODEL in .env to move to
# the next one without touching code, and see LLMModelUnavailable below, which
# exists to make the next retirement diagnose itself.
DEFAULT_MODEL = "gemini-3.6-flash"

KEY_ENV = "GEMINI_API_KEY"
MODEL_ENV = "GEMINI_MODEL"
RPM_ENV = "GEMINI_RPM"

# The free tier limits requests per minute as well as per day, and a loop that
# sends as fast as it can spends the first few seconds of every minute being
# refused. Pacing costs nothing on a run that was going to take half an hour
# anyway, so the default is deliberately below the published Flash free-tier
# allowance rather than at it. Set GEMINI_RPM to raise it; 0 disables pacing.
DEFAULT_RPM = 10

# How long to wait for one page. Lowered from 120s: on a 20-request daily
# allowance, spending two minutes on a request that has almost certainly
# already died is most of a minute of quota bought for nothing, and a retry
# costs less than the wait it replaces.
READ_TIMEOUT_SECONDS = 60

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# 401/403 are the key being wrong. 400 usually is too, when the message says so.
AUTH_STATUS = frozenset({401, 403})

MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 2.0

# A 429 may name any delay it likes. Honour it, but a cron run must not block
# for an hour on one page -- past this we give up and let the page stay pending.
MAX_RETRY_AFTER_SECONDS = 60.0

# A 429 whose stated retry delay is under this is a short-window ceiling, not a
# daily cap. Nothing that resets tomorrow asks to be retried in seconds.
SHORT_WINDOW_SECONDS = 300.0

# Deterministic as the API allows. Re-transcribing the same slide must not
# produce different text every run; the page cache in files/ocr.py is what
# actually guarantees it, but there is no reason to add avoidable variance.
TEMPERATURE = 0.0

Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


class LLMError(Exception):
    """A model call failed."""


class LLMAuthError(LLMError):
    """The credential is missing, malformed or rejected. Never retried."""


class LLMQuotaError(LLMError):
    """The daily quota is spent. Nothing more will succeed today.

    The caller stops asking. Distinct from LLMRateLimited below, because the
    two demand opposite responses and a run that treats a per-minute ceiling as
    a daily cap gives up after one page.
    """


class LLMRateLimited(LLMError):
    """The per-minute allowance is spent. Waiting is enough.

    Carries the delay the API asked for, when it named one.
    """

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMUnavailable(LLMError):
    """Transport failure or persistent 5xx."""


class LLMTimeout(LLMUnavailable):
    """The request took longer than the read timeout.

    A subclass of LLMUnavailable so it joins the retry path automatically and
    every existing handler keeps working, but its own type so a run can report
    "3 pages timed out" rather than burying them in a pending count. Timeouts
    say something different from a refusal: the request may have been served
    and the answer lost, and it cost the full timeout to find out.
    """


class LLMModelUnavailable(LLMError):
    """The configured model does not exist for this key -- usually retired.

    Distinct from a generic API error on purpose. Google withdraws a model,
    every single request starts returning 404, and folded into LLMError that
    surfaces as several hundred pages quietly marked "pending" with no visible
    reason. It behaves like LLMAuthError instead: raised immediately, because
    no page will ever succeed until the configuration changes.
    """


class LLMRefused(LLMError):
    """The model returned no usable content for this input."""


class LLMProvider(ABC):
    """What the rest of the codebase is allowed to know about a model."""

    #: Recorded alongside generated text so a later run can tell what produced it.
    name: str = "unknown"

    @abstractmethod
    def transcribe_image(
        self, image: bytes, prompt: str, *, mime_type: str = "image/png"
    ) -> str:
        """Return the model's reading of one image.

        Raises LLMQuotaError when the caller should try again later, and
        LLMAuthError when it should stop and fix the configuration.
        """

    @abstractmethod
    def generate_json(self, prompt: str, schema: dict[str, Any]) -> Any:
        """Return the model's answer already decoded, not as text to repair.

        The schema is the contract, and it is enforced by the API rather than
        by a parser here: asking a model for JSON in prose and then repairing
        fenced code blocks is a class of bug that a constrained decode simply
        does not have. What the caller still has to check is meaning -- four
        options, exactly one of them correct -- because a schema can require a
        list of four strings and cannot require that they are four plausible
        answers to the question above them.

        Raises the same errors as transcribe_image, plus LLMRefused for a body
        that arrives outside the schema anyway.
        """


class _ApiError(LLMError):
    """An HTTP-level failure, carrying what the retry loop needs to decide."""

    def __init__(
        self, message: str, *, status: int, retry_after: float | None, body: str = ""
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        # Kept verbatim so `agent ocr --verbose` can show what the API actually
        # said. The parsed message loses details the raw body still carries.
        self.body = body


def _retry_delay(decoded: dict[str, Any]) -> float | None:
    """Seconds from a RetryInfo detail, when the error carries one.

    Gemini puts the server's own opinion of how long to wait in
    error.details[] as {"@type": ".../google.rpc.RetryInfo",
    "retryDelay": "37s"}. Guessing when we have been told is how a backoff
    loop ends up hammering an exhausted quota.
    """
    error = decoded.get("error")
    if not isinstance(error, dict):
        return None
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        if not str(detail.get("@type", "")).endswith("RetryInfo"):
            continue
        raw = str(detail.get("retryDelay") or "").strip()
        try:
            return float(raw[:-1]) if raw.endswith("s") else float(raw)
        except ValueError:
            return None
    return None


def _quota_scope(err: _ApiError) -> str:
    """'minute', 'day' or 'unknown' for a 429.

    Gemini names the ceiling that was hit in a QuotaFailure detail, as a
    quotaId like "GenerateRequestsPerMinutePerProjectPerModel-FreeTier" or
    "...PerDayPerProjectPerModel-FreeTier". The two mean opposite things to the
    caller -- wait sixty seconds, or come back tomorrow -- and nothing else in
    the response distinguishes them.

    Unknown is treated as a daily cap by the caller. Stopping a run that could
    have continued costs a rerun; hammering a spent daily quota costs the
    quota.
    """
    try:
        decoded = json.loads(err.body or "{}")
    except json.JSONDecodeError:
        return "unknown"

    error = decoded.get("error")
    if not isinstance(error, dict):
        return "unknown"

    ids: list[str] = []
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        if not str(detail.get("@type", "")).endswith("QuotaFailure"):
            continue
        for violation in detail.get("violations") or []:
            if isinstance(violation, dict):
                ids.append(str(violation.get("quotaId") or ""))

    haystack = " ".join(ids).lower()
    if "perday" in haystack:
        return "day"
    if "perminute" in haystack:
        return "minute"

    # The quotaId does not always name a window. Measured against the real free
    # tier, the violation reads "generate_content_free_tier_requests" with no
    # period in it at all, and the only thing separating a sixty-second ceiling
    # from a daily one is the delay the server asks for. A server that says
    # "please retry in 16.27s" is not describing a cap that resets tomorrow.
    if err.retry_after is not None and err.retry_after <= SHORT_WINDOW_SECONDS:
        return "minute"

    message = str((error.get("message") or "")).lower()
    if "per day" in message or "daily" in message:
        return "day"
    return "unknown"


def _http_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """POST JSON and decode the reply."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=READ_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))

    # HTTPError first: it is a subclass of URLError and carries the API's own
    # answer, which is the useful part of the failure.
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {}
        message = (decoded.get("error") or {}).get("message") or raw.strip() or "no message"
        raise _ApiError(
            f"Gemini returned {err.code}: {message}",
            status=err.code,
            retry_after=_retry_delay(decoded),
            body=raw,
        ) from err

    # A timeout while READING the response arrives raw, not wrapped in
    # URLError -- which is how it escaped every handler here and killed a run
    # with a traceback. TimeoutError and socket.timeout are the same class.
    except TimeoutError as err:
        raise LLMTimeout(
            f"Gemini did not respond within {READ_TIMEOUT_SECONDS:.0f}s"
        ) from err

    except urllib.error.URLError as err:
        # A timeout while CONNECTING is wrapped, so it arrives here instead.
        # Same condition, same verdict.
        if isinstance(err.reason, TimeoutError):
            raise LLMTimeout(
                f"Gemini did not respond within {READ_TIMEOUT_SECONDS:.0f}s"
            ) from err
        raise LLMUnavailable(f"could not reach the Gemini API: {err.reason}") from err

    # Everything else the network can do. Audited rather than guessed: of the
    # conditions that can be raised here -- connection reset, DNS failure, SSL
    # errors, a half-closed connection, a truncated body -- NONE is a subclass
    # of URLError, so catching URLError alone let every one of them escape as a
    # crash. They are all OSError or HTTPException, and both are caught now.
    # This is the backstop that keeps an unhandled network condition a report.
    except (OSError, http.client.HTTPException) as err:
        raise LLMUnavailable(
            f"could not reach the Gemini API: {type(err).__name__}: {err}"
        ) from err

    except json.JSONDecodeError as err:
        raise LLMUnavailable(f"Gemini returned a body that is not JSON: {err}") from err


class GeminiProvider(LLMProvider):
    """Gemini over the REST API, with the key from the environment."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        requests_per_minute: int = DEFAULT_RPM,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        api_base: str = API_BASE,
    ):
        if not api_key:
            raise LLMAuthError(
                f"{KEY_ENV} is empty. Put it in the .env file beside config.yaml:\n"
                f"    {KEY_ENV}=AIza...\n"
                f"  Get one from https://aistudio.google.com/apikey. It is a "
                f"secret and never goes in config.yaml."
            )
        self.api_key = api_key
        self.model = model
        self.name = f"gemini:{model}"
        self._transport = transport or _http_post
        self._sleep = sleep
        self._clock = clock
        self._api_base = api_base.rstrip("/")
        self.requests_per_minute = requests_per_minute
        self._min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._last_call: float | None = None

    def _pace(self) -> None:
        """Hold off until the configured request rate allows the next call.

        Self-imposed, ahead of the limit, rather than reactive: discovering the
        per-minute ceiling by being refused wastes an attempt and a slice of
        the retry budget on every single page.
        """
        if self._min_interval <= 0:
            return
        if self._last_call is not None:
            waited = self._clock() - self._last_call
            if waited < self._min_interval:
                self._sleep(self._min_interval - waited)
        self._last_call = self._clock()

    def transcribe_image(
        self, image: bytes, prompt: str, *, mime_type: str = "image/png"
    ) -> str:
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64.b64encode(image).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {"temperature": TEMPERATURE},
        }
        self._pace()
        response = self._request(f"models/{self.model}:generateContent", payload)
        return _first_text(response)

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> Any:
        """Ask for JSON and get JSON, because the API is told the shape.

        responseMimeType plus responseSchema make the decoder itself
        constrained, so the answer cannot arrive wrapped in ```json or with a
        sentence of preamble. That matters more here than it looks: on a ~20
        request daily allowance, a body that has to be repaired and retried is
        5% of the day's quiz capacity spent on punctuation.
        """
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": TEMPERATURE,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        self._pace()
        response = self._request(f"models/{self.model}:generateContent", payload)
        text = _first_text(response)
        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            # Constrained decoding makes this close to impossible, which is
            # exactly why it must not be an unhandled crash if it happens:
            # LLMRefused is the error the caller already knows how to degrade.
            raise LLMRefused(
                f"the model returned a body that is not JSON despite being "
                f"asked for it ({err}); first 200 characters: {text[:200]!r}"
            ) from err

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._api_base}/{path}"
        # In a header rather than ?key=, so the credential stays out of URLs,
        # proxy logs and tracebacks.
        headers = {"x-goog-api-key": self.api_key}
        last: _ApiError | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                return self._transport(url, payload, headers)
            except _ApiError as err:
                if err.status in AUTH_STATUS or _looks_like_bad_key(err):
                    raise LLMAuthError(str(err)) from err
                if err.status == 404:
                    raise LLMModelUnavailable(self._retirement_message(err)) from err
                if err.status not in RETRYABLE_STATUS:
                    raise LLMError(str(err)) from err
                if err.status == 429 and _quota_scope(err) == "day":
                    # The first response already said the allowance is gone for
                    # the day. Retrying it four times behind a capped backoff
                    # spends about three minutes relearning that, per page.
                    # Only a verdict of "day" short-circuits: "unknown" is not a
                    # determination, so it keeps its retries.
                    raise LLMQuotaError(str(err)) from err
                last = err
                if attempt == MAX_ATTEMPTS - 1:
                    break
                self._sleep(self._delay(err, attempt))
            except LLMUnavailable as err:
                # A dropped connection or a DNS blip is every bit as transient
                # as a 503, and used to get a single attempt with no backoff
                # purely because it arrives as a different exception type.
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                self._sleep(BACKOFF_BASE_SECONDS * (2**attempt))

        assert last is not None  # only reachable through the retry branch
        if last.status == 429:
            # Which ceiling was hit decides whether the run waits or stops, so
            # the two are separate exceptions rather than one "rate limited".
            if _quota_scope(last) == "minute":
                raise LLMRateLimited(str(last), retry_after=last.retry_after) from last
            raise LLMQuotaError(str(last)) from last
        raise LLMUnavailable(str(last)) from last

    def _delay(self, err: _ApiError, attempt: int) -> float:
        if err.retry_after is not None:
            return min(max(err.retry_after, 0.0), MAX_RETRY_AFTER_SECONDS)
        return BACKOFF_BASE_SECONDS * (2**attempt)

    def _retirement_message(self, err: _ApiError) -> str:
        """Say which model failed, what the API suggested, and how to change it.

        Google's own 404 body names the replacement model, so it is quoted
        rather than paraphrased -- it is more current than anything written
        here can be.
        """
        return (
            f"The model '{self.model}' is not available for this API key.\n"
            f"  Gemini said: {err}\n"
            f"  Models are retired periodically. Set {MODEL_ENV} in the .env "
            f"file beside config.yaml to the replacement it names:\n"
            f"      {MODEL_ENV}=<model-name>\n"
            f"  No code change is needed; {DEFAULT_MODEL} is only the default."
        )


def _looks_like_bad_key(err: _ApiError) -> bool:
    """A 400 that is really an authentication failure.

    Gemini answers a malformed key with 400 API_KEY_INVALID rather than 401,
    and retrying that four times before reporting an unrelated-sounding error
    is a bad first-run experience.
    """
    return err.status == 400 and "api key" in str(err).lower()


def _first_text(response: dict[str, Any]) -> str:
    """The text of the first candidate, or an explanation of why there is none."""
    candidates = response.get("candidates") or []
    if not candidates:
        blocked = (response.get("promptFeedback") or {}).get("blockReason")
        raise LLMRefused(f"no candidates returned (blockReason={blocked or 'none given'})")

    candidate = candidates[0] or {}
    parts = ((candidate.get("content") or {}).get("parts")) or []
    text = "".join(part.get("text") or "" for part in parts if isinstance(part, dict))
    if not text.strip():
        raise LLMRefused(
            f"empty response (finishReason={candidate.get('finishReason') or 'none given'})"
        )
    return text


def from_env(*, transport: Transport | None = None, model: str | None = None) -> GeminiProvider:
    """Build the provider from .env, or raise an error naming what is missing.

    load_config() has already pulled .env into the environment; a provider
    built directly in a test has not, which is why this reads os.environ rather
    than taking a Config.
    """
    key = (os.environ.get(KEY_ENV) or "").strip()
    if not key:
        raise LLMAuthError(
            f"{KEY_ENV} is not set, so there is no way to reach the model.\n"
            f"  Put it in the .env file beside config.yaml:\n"
            f"      {KEY_ENV}=AIza...\n"
            f"  Get one from https://aistudio.google.com/apikey. It is a secret "
            f"and never goes in config.yaml."
        )
    chosen = model or (os.environ.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL

    raw_rpm = (os.environ.get(RPM_ENV) or "").strip()
    try:
        rpm = int(raw_rpm) if raw_rpm else DEFAULT_RPM
    except ValueError:
        # A typo here would otherwise disable pacing silently and spend the
        # per-minute allowance in the first three seconds of the run.
        raise LLMAuthError(
            f"{RPM_ENV} must be a whole number of requests per minute, "
            f"got {raw_rpm!r}. Remove it from .env to use the default "
            f"({DEFAULT_RPM}), or set 0 to disable pacing."
        ) from None

    return GeminiProvider(key, model=chosen, requests_per_minute=rpm, transport=transport)

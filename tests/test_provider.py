"""The LLM boundary.

No test reaches the network: the transport is a callable and every test passes
its own. The cases that matter are about which failures the caller is expected
to survive and which it must not -- a quota error means "try tomorrow", an auth
error means "stop and fix the key", and confusing the two is how a library goes
quietly untranscribed for a month.
"""

from __future__ import annotations

import base64
import http.client
import json
import socket
import ssl

import pytest

from agent.llm import provider as p


def ok_response(text="transcribed text"):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def api_error(status, *, message="nope", retry_delay=None):
    body = {"error": {"code": status, "message": message}}
    if retry_delay is not None:
        body["error"]["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": retry_delay,
            }
        ]
    return p._ApiError(
        f"Gemini returned {status}: {message}",
        status=status,
        retry_after=p._retry_delay(body),
    )


class FakeTransport:
    """Replays a scripted sequence of responses or exceptions."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, payload, headers):
        self.calls.append((url, payload, headers))
        outcome = self.outcomes.pop(0) if self.outcomes else ok_response()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make(transport, *, slept=None, model="gemini-2.5-flash"):
    return p.GeminiProvider(
        "test-key",
        model=model,
        transport=transport,
        sleep=slept.append if slept is not None else (lambda _s: None),
    )


# --------------------------------------------------------------------------
# the request
# --------------------------------------------------------------------------


def test_the_image_is_sent_as_base64_inline_data():
    transport = FakeTransport(ok_response())

    make(transport).transcribe_image(b"\x89PNG-bytes", "read this")

    _url, payload, _headers = transport.calls[0]
    parts = payload["contents"][0]["parts"]
    assert parts[0]["text"] == "read this"
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == b"\x89PNG-bytes"
    assert parts[1]["inline_data"]["mime_type"] == "image/png"


def test_the_key_travels_in_a_header_not_the_url():
    """Query-string credentials end up in proxy logs and tracebacks."""
    transport = FakeTransport(ok_response())

    make(transport).transcribe_image(b"x", "read this")

    url, _payload, headers = transport.calls[0]
    assert headers["x-goog-api-key"] == "test-key"
    assert "test-key" not in url


def test_temperature_is_zero():
    """Re-reading the same slide should not produce different text each run."""
    transport = FakeTransport(ok_response())

    make(transport).transcribe_image(b"x", "read this")

    assert transport.calls[0][1]["generationConfig"]["temperature"] == 0.0


def test_the_model_appears_in_the_url_and_the_provider_name():
    transport = FakeTransport(ok_response())
    provider = make(transport, model="gemini-2.5-flash")

    provider.transcribe_image(b"x", "read this")

    assert "models/gemini-2.5-flash:generateContent" in transport.calls[0][0]
    assert provider.name == "gemini:gemini-2.5-flash"


def test_the_text_of_the_first_candidate_is_returned():
    transport = FakeTransport(ok_response("$E = mc^2$"))

    assert make(transport).transcribe_image(b"x", "read this") == "$E = mc^2$"


def test_multiple_parts_are_joined():
    transport = FakeTransport(
        {"candidates": [{"content": {"parts": [{"text": "line one\n"}, {"text": "line two"}]}}]}
    )

    assert make(transport).transcribe_image(b"x", "p") == "line one\nline two"


# --------------------------------------------------------------------------
# failures the caller has to tell apart
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_status_raises_immediately(status):
    """Never retried: a wrong key does not become right on the fourth attempt."""
    transport = FakeTransport(api_error(status), ok_response())

    with pytest.raises(p.LLMAuthError):
        make(transport).transcribe_image(b"x", "p")
    assert len(transport.calls) == 1


def test_a_400_about_the_api_key_is_an_auth_error():
    """Gemini answers a malformed key with 400 API_KEY_INVALID, not 401."""
    transport = FakeTransport(api_error(400, message="API key not valid. Please pass a valid API key."))

    with pytest.raises(p.LLMAuthError):
        make(transport).transcribe_image(b"x", "p")


def test_an_exhausted_quota_is_its_own_error():
    """The caller defers the page rather than failing the run."""
    transport = FakeTransport(*[api_error(429) for _ in range(p.MAX_ATTEMPTS)])

    with pytest.raises(p.LLMQuotaError):
        make(transport).transcribe_image(b"x", "p")
    assert len(transport.calls) == p.MAX_ATTEMPTS


def test_a_persistent_5xx_is_unavailable_not_quota():
    transport = FakeTransport(*[api_error(503) for _ in range(p.MAX_ATTEMPTS)])

    with pytest.raises(p.LLMUnavailable):
        make(transport).transcribe_image(b"x", "p")


def test_a_429_that_resolves_is_retried_and_succeeds():
    transport = FakeTransport(api_error(429), ok_response("done"))

    assert make(transport).transcribe_image(b"x", "p") == "done"
    assert len(transport.calls) == 2


def test_the_servers_own_retry_delay_is_honoured():
    """Guessing when the API has told us is how you hammer a spent quota."""
    slept: list[float] = []
    transport = FakeTransport(api_error(429, retry_delay="37s"), ok_response())

    make(transport, slept=slept).transcribe_image(b"x", "p")

    assert slept == [37.0]


def test_an_absurd_retry_delay_is_capped():
    """A cron run must not block for an hour on one page."""
    slept: list[float] = []
    transport = FakeTransport(api_error(429, retry_delay="3600s"), ok_response())

    make(transport, slept=slept).transcribe_image(b"x", "p")

    assert slept == [p.MAX_RETRY_AFTER_SECONDS]


def test_backoff_grows_without_a_stated_delay():
    slept: list[float] = []
    transport = FakeTransport(api_error(503), api_error(503), ok_response())

    make(transport, slept=slept).transcribe_image(b"x", "p")

    assert slept[0] < slept[1]


def test_a_retired_model_raises_its_own_error_not_a_generic_one():
    """The real 404 that cost an hour: folded into LLMError it reads as nothing."""
    transport = FakeTransport(
        api_error(
            404,
            message=(
                "This model models/gemini-2.5-flash is no longer available to new "
                "users. Please update your code to use models/gemini-3.6-flash"
            ),
        )
    )

    with pytest.raises(p.LLMModelUnavailable) as err:
        make(transport, model="gemini-2.5-flash").transcribe_image(b"x", "prompt")

    message = str(err.value)
    assert "gemini-2.5-flash" in message           # which model failed
    assert "gemini-3.6-flash" in message           # what Google said to use
    assert p.MODEL_ENV in message                  # how to change it without code
    assert len(transport.calls) == 1               # a 404 is not retried


def test_a_retired_model_is_not_confused_with_quota_or_auth():
    transport = FakeTransport(api_error(404, message="model not found"))

    with pytest.raises(p.LLMModelUnavailable) as err:
        make(transport).transcribe_image(b"x", "prompt")
    assert not isinstance(err.value, (p.LLMQuotaError, p.LLMAuthError))


def test_the_default_model_is_not_the_retired_one():
    assert p.DEFAULT_MODEL == "gemini-3.6-flash"


# --------------------------------------------------------------------------
# network failures share the retry path with 5xx
# --------------------------------------------------------------------------


def test_a_transient_network_error_is_retried_and_succeeds():
    """It used to get one attempt and no backoff purely for being a different type."""
    slept: list[float] = []
    transport = FakeTransport(p.LLMUnavailable("connection reset"), ok_response("read it"))

    assert make(transport, slept=slept).transcribe_image(b"x", "p") == "read it"
    assert len(transport.calls) == 2
    assert slept  # it waited rather than hammering


def test_repeated_network_errors_back_off_then_give_up():
    slept: list[float] = []
    transport = FakeTransport(*[p.LLMUnavailable("dns") for _ in range(p.MAX_ATTEMPTS)])

    with pytest.raises(p.LLMUnavailable):
        make(transport, slept=slept).transcribe_image(b"x", "p")

    assert len(transport.calls) == p.MAX_ATTEMPTS
    assert slept[0] < slept[1]


def test_a_network_error_then_a_429_still_ends_as_quota():
    """Mixed failures must not lose the distinction the caller acts on."""
    transport = FakeTransport(
        p.LLMUnavailable("blip"), *[api_error(429) for _ in range(p.MAX_ATTEMPTS - 1)]
    )

    with pytest.raises(p.LLMQuotaError):
        make(transport).transcribe_image(b"x", "p")


# --------------------------------------------------------------------------
# pacing and the two kinds of 429
# --------------------------------------------------------------------------


def quota_error(scope, *, retry_delay=None):
    """A 429 naming the ceiling it hit, the way Gemini actually does."""
    quota_id = {
        "minute": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
        "day": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        "unknown": "",
    }[scope]
    body = {
        "error": {
            "code": 429,
            "message": "Resource has been exhausted",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": quota_id}] if quota_id else [],
                }
            ],
        }
    }
    if retry_delay is not None:
        body["error"]["details"].append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
        )
    return p._ApiError(
        "Gemini returned 429: Resource has been exhausted",
        status=429,
        retry_after=p._retry_delay(body),
        body=json.dumps(body),
    )


def test_a_per_minute_ceiling_is_not_a_daily_cap():
    """Treating it as exhaustion gives up a whole day's allowance over 60 seconds."""
    transport = FakeTransport(*[quota_error("minute") for _ in range(p.MAX_ATTEMPTS)])

    with pytest.raises(p.LLMRateLimited) as err:
        make(transport).transcribe_image(b"x", "prompt")
    assert not isinstance(err.value, p.LLMQuotaError)


def test_a_daily_cap_stays_a_quota_error():
    transport = FakeTransport(*[quota_error("day") for _ in range(p.MAX_ATTEMPTS)])

    with pytest.raises(p.LLMQuotaError):
        make(transport).transcribe_image(b"x", "prompt")


def test_a_daily_cap_is_not_retried_at_all():
    """The first response carries the answer. Measured: retrying it cost ~180s a page."""
    slept: list[float] = []
    transport = FakeTransport(*[quota_error("day") for _ in range(p.MAX_ATTEMPTS)])

    with pytest.raises(p.LLMQuotaError):
        make(transport, slept=slept).transcribe_image(b"x", "prompt")

    assert len(transport.calls) == 1
    assert slept == []


def test_a_per_minute_429_still_retries_and_succeeds():
    """The distinction the classifier exists for: wait one out, give up on the other."""
    slept: list[float] = []
    transport = FakeTransport(
        quota_error("minute", retry_delay="16s"), ok_response("transcribed at last")
    )

    result = make(transport, slept=slept).transcribe_image(b"x", "prompt")

    assert result == "transcribed at last"
    assert len(transport.calls) == 2
    assert slept == [16.0]


def test_an_unknown_429_keeps_its_retries():
    """'unknown' is not a determination, so it is not treated as one."""
    transport = FakeTransport(quota_error("unknown"), ok_response("through"))

    assert make(transport).transcribe_image(b"x", "prompt") == "through"
    assert len(transport.calls) == 2


def test_an_unlabelled_429_is_treated_as_the_daily_cap():
    """Stopping a run that could continue costs a rerun; the other way costs the quota."""
    transport = FakeTransport(*[quota_error("unknown") for _ in range(p.MAX_ATTEMPTS)])

    with pytest.raises(p.LLMQuotaError):
        make(transport).transcribe_image(b"x", "prompt")


def test_a_rate_limit_carries_the_delay_the_api_asked_for():
    transport = FakeTransport(
        *[quota_error("minute", retry_delay="42s") for _ in range(p.MAX_ATTEMPTS)]
    )

    with pytest.raises(p.LLMRateLimited) as err:
        make(transport).transcribe_image(b"x", "prompt")
    assert err.value.retry_after == 42.0


@pytest.mark.parametrize(
    "scope,expected",
    [("minute", "minute"), ("day", "day"), ("unknown", "unknown")],
)
def test_quota_scope_parsing(scope, expected):
    assert p._quota_scope(quota_error(scope)) == expected


def test_a_short_retry_delay_means_a_short_window_whatever_the_quota_id_says():
    """The real free tier: quotaId names no period, but it asks for 16 seconds.

    Measured verbatim against the live API. Reading only the quotaId classified
    this as a daily cap and stopped a run that would have resumed in a minute.
    """
    err = quota_error("unknown", retry_delay="16.274706098s")

    assert p._quota_scope(err) == "minute"


def test_a_long_retry_delay_is_not_treated_as_a_short_window():
    assert p._quota_scope(quota_error("unknown", retry_delay="7200s")) == "unknown"


def test_a_message_naming_a_daily_limit_is_believed():
    err = p._ApiError(
        "Gemini returned 429", status=429, retry_after=None,
        body=json.dumps({"error": {"message": "Quota exceeded: requests per day"}}),
    )
    assert p._quota_scope(err) == "day"


def test_the_real_free_tier_429_is_classified_as_recoverable():
    """The exact body the live API returned, kept as a regression fixture."""
    body = {
        "error": {
            "code": 429,
            "message": (
                "You exceeded your current quota, please check your plan and billing "
                "details. * Quota exceeded for metric: "
                "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                "limit: 20, model: gemini-3.6-flash\nPlease retry in 16.274706098s."
            ),
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {"quotaId": "generate_content_free_tier_requests"}
                    ],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "16.274706098s",
                },
            ],
        }
    }
    transport = FakeTransport(
        *[
            p._ApiError("Gemini returned 429", status=429,
                        retry_after=p._retry_delay(body), body=json.dumps(body))
            for _ in range(p.MAX_ATTEMPTS)
        ]
    )

    with pytest.raises(p.LLMRateLimited):
        make(transport).transcribe_image(b"x", "prompt")


def test_calls_are_paced_to_the_configured_rate():
    """Discovering the per-minute ceiling by being refused wastes an attempt a page."""
    slept: list[float] = []
    # Half a second of real work passes between each call, so the pacer should
    # top up the remaining half of the one-second interval, not sleep a full one.
    clock = iter([0.0, 0.5, 1.0, 1.5, 2.0])
    provider = p.GeminiProvider(
        "k",
        requests_per_minute=60,  # one per second
        transport=FakeTransport(ok_response(), ok_response(), ok_response()),
        sleep=slept.append,
        clock=lambda: next(clock),
    )

    for _ in range(3):
        provider.transcribe_image(b"x", "prompt")

    # First call is free; the next two wait out the remainder of the second.
    assert slept == [0.5, 0.5]


def test_a_slow_call_is_not_delayed_further():
    """If the request itself took longer than the interval, do not add to it."""
    slept: list[float] = []
    clock = iter([0.0, 99.0, 99.0])
    provider = p.GeminiProvider(
        "k", requests_per_minute=60,
        transport=FakeTransport(ok_response(), ok_response()),
        sleep=slept.append, clock=lambda: next(clock),
    )

    provider.transcribe_image(b"x", "prompt")
    provider.transcribe_image(b"x", "prompt")

    assert slept == []


def test_the_first_call_never_waits():
    slept: list[float] = []
    provider = p.GeminiProvider(
        "k", requests_per_minute=1, transport=FakeTransport(ok_response()),
        sleep=slept.append, clock=lambda: 0.0,
    )

    provider.transcribe_image(b"x", "prompt")

    assert slept == []


def test_pacing_can_be_switched_off():
    slept: list[float] = []
    provider = p.GeminiProvider(
        "k", requests_per_minute=0,
        transport=FakeTransport(ok_response(), ok_response()),
        sleep=slept.append, clock=lambda: 0.0,
    )

    provider.transcribe_image(b"x", "prompt")
    provider.transcribe_image(b"x", "prompt")

    assert slept == []


def test_from_env_reads_the_rate_and_defaults_conservatively(monkeypatch):
    monkeypatch.setenv(p.KEY_ENV, "AIza-test")
    monkeypatch.delenv(p.RPM_ENV, raising=False)
    assert p.from_env().requests_per_minute == p.DEFAULT_RPM

    monkeypatch.setenv(p.RPM_ENV, "4")
    assert p.from_env().requests_per_minute == 4


def test_a_malformed_rate_is_refused_rather_than_ignored(monkeypatch):
    """Silently defaulting would spend the minute's allowance in three seconds."""
    monkeypatch.setenv(p.KEY_ENV, "AIza-test")
    monkeypatch.setenv(p.RPM_ENV, "ten")

    with pytest.raises(p.LLMAuthError) as err:
        p.from_env()
    assert p.RPM_ENV in str(err.value)


# --------------------------------------------------------------------------
# timeouts and the other network conditions
# --------------------------------------------------------------------------


def test_a_read_timeout_is_classified_not_raised(monkeypatch):
    """The crash: a read timeout arrives RAW, not wrapped in URLError.

    It is a TimeoutError -- an OSError, not a URLError -- so the handler that
    covered network failures never saw it and it escaped as a traceback that
    killed a twenty-page run on its first page.
    """
    def urlopen(request, timeout=None):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(p.urllib.request, "urlopen", urlopen)

    with pytest.raises(p.LLMTimeout):
        p._http_post("https://example.invalid", {}, {})


def test_a_connect_timeout_is_also_a_timeout(monkeypatch):
    """A timeout while connecting IS wrapped. Same condition, same verdict."""
    def urlopen(request, timeout=None):
        raise p.urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(p.urllib.request, "urlopen", urlopen)

    with pytest.raises(p.LLMTimeout):
        p._http_post("https://example.invalid", {}, {})


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError(104, "Connection reset by peer"),
        socket.gaierror(-2, "Name or service not known"),
        ssl.SSLError(1, "decryption failed"),
        ssl.SSLCertVerificationError(1, "certificate verify failed"),
        http.client.RemoteDisconnected("Remote end closed connection"),
        http.client.IncompleteRead(b"half a response"),
        http.client.BadStatusLine("garbage"),
        OSError(101, "Network is unreachable"),
    ],
)
def test_no_network_condition_escapes_unclassified(monkeypatch, error):
    """The audit. NONE of these is a URLError subclass, so all of them escaped.

    IncompleteRead and BadStatusLine are not even OSErrors -- they are
    HTTPExceptions -- which is why the backstop has to catch both trees.
    """
    def urlopen(request, timeout=None):
        raise error

    monkeypatch.setattr(p.urllib.request, "urlopen", urlopen)

    with pytest.raises(p.LLMError):  # classified, whatever it is
        p._http_post("https://example.invalid", {}, {})


def test_a_real_socket_timeout_reaches_the_classifier():
    """Driven through a real socket rather than a stubbed exception.

    A server that accepts the connection and then says nothing is exactly the
    condition that crashed the run, and nothing here is faked.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    monkey = p.READ_TIMEOUT_SECONDS
    try:
        p.READ_TIMEOUT_SECONDS = 1  # do not make the suite wait a minute
        with pytest.raises(p.LLMTimeout):
            p._http_post(f"http://127.0.0.1:{port}/v1beta/models", {"a": 1}, {})
    finally:
        p.READ_TIMEOUT_SECONDS = monkey
        server.close()


def test_a_timeout_is_retried_before_giving_up():
    """It is a subclass of LLMUnavailable, so it takes the network retry path."""
    slept: list[float] = []
    transport = FakeTransport(p.LLMTimeout("timed out"), ok_response("second try"))

    assert make(transport, slept=slept).transcribe_image(b"x", "p") == "second try"
    assert len(transport.calls) == 2
    assert slept


def test_a_persistent_timeout_stays_a_timeout_after_retries():
    """The type has to survive the retry loop, or the count cannot be reported."""
    transport = FakeTransport(*[p.LLMTimeout("timed out") for _ in range(p.MAX_ATTEMPTS)])

    with pytest.raises(p.LLMTimeout):
        make(transport).transcribe_image(b"x", "p")
    assert len(transport.calls) == p.MAX_ATTEMPTS


def test_the_read_timeout_is_sixty_seconds():
    """120s of a 20-request daily allowance is most of a minute bought for nothing."""
    assert p.READ_TIMEOUT_SECONDS == 60


def test_the_timeout_is_actually_passed_to_urlopen(monkeypatch):
    seen = {}

    def urlopen(request, timeout=None):
        seen["timeout"] = timeout
        raise TimeoutError("boom")

    monkeypatch.setattr(p.urllib.request, "urlopen", urlopen)

    with pytest.raises(p.LLMTimeout):
        p._http_post("https://example.invalid", {}, {})
    assert seen["timeout"] == p.READ_TIMEOUT_SECONDS


def test_a_non_retryable_status_is_not_retried():
    transport = FakeTransport(api_error(404, message="model not found"), ok_response())

    with pytest.raises(p.LLMError):
        make(transport).transcribe_image(b"x", "p")
    assert len(transport.calls) == 1


def test_a_blocked_prompt_is_a_refusal():
    transport = FakeTransport({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(p.LLMRefused):
        make(transport).transcribe_image(b"x", "p")


def test_an_empty_answer_is_a_refusal_not_empty_text():
    """Silently returning '' would write a blank page over real content."""
    transport = FakeTransport({"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]})

    with pytest.raises(p.LLMRefused):
        make(transport).transcribe_image(b"x", "p")


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------


def test_an_empty_key_is_refused_at_construction():
    with pytest.raises(p.LLMAuthError):
        p.GeminiProvider("")


def test_from_env_names_the_variable_when_it_is_missing(monkeypatch):
    monkeypatch.delenv(p.KEY_ENV, raising=False)

    with pytest.raises(p.LLMAuthError) as err:
        p.from_env()
    assert p.KEY_ENV in str(err.value)
    assert "config.yaml" in str(err.value)  # says where it must NOT go


def test_from_env_reads_the_key_and_the_model_override(monkeypatch):
    monkeypatch.setenv(p.KEY_ENV, "AIza-test")
    monkeypatch.setenv(p.MODEL_ENV, "gemini-flash-latest")

    provider = p.from_env()

    assert provider.api_key == "AIza-test"
    assert provider.model == "gemini-flash-latest"


def test_from_env_defaults_to_a_flash_model(monkeypatch):
    monkeypatch.setenv(p.KEY_ENV, "AIza-test")
    monkeypatch.delenv(p.MODEL_ENV, raising=False)

    assert "flash" in p.from_env().model


@pytest.mark.parametrize(
    "raw,expected",
    [("37s", 37.0), ("0.5s", 0.5), ("12", 12.0), ("nonsense", None), (None, None)],
)
def test_retry_delay_parsing(raw, expected):
    body = {"error": {"details": []}} if raw is None else {
        "error": {"details": [{"@type": ".../google.rpc.RetryInfo", "retryDelay": raw}]}
    }
    assert p._retry_delay(body) == expected


def test_a_body_with_no_retry_info_yields_no_delay():
    assert p._retry_delay(json.loads('{"error": {"message": "x"}}')) is None


# --------------------------------------------------------------------------
# generate_json
# --------------------------------------------------------------------------

def test_the_schema_is_sent_and_json_is_asked_for(monkeypatch):
    """Constrained decoding rather than prose to repair. On ~20 requests a day,
    a body that has to be retried for punctuation is 5% of the allowance."""
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    transport = FakeTransport(ok_response('{"a": "b"}'))

    result = make(transport).generate_json("ask me", schema)

    assert result == {"a": "b"}
    _, payload, _ = transport.calls[0]
    config = payload["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"] == schema
    assert config["temperature"] == 0.0
    assert payload["contents"][0]["parts"][0]["text"] == "ask me"


def test_a_json_array_comes_back_as_a_list():
    result = make(FakeTransport(ok_response('[1, 2, 3]'))).generate_json("x", {})
    assert result == [1, 2, 3]


def test_a_body_that_is_not_json_is_a_refusal_not_a_crash():
    """Close to impossible under a constrained decode, which is exactly why it
    must degrade rather than escape as a ValueError nobody catches."""
    with pytest.raises(p.LLMRefused) as err:
        make(FakeTransport(ok_response("Sure! Here you go: {oops"))).generate_json("x", {})
    assert "not JSON" in str(err.value)


def test_an_empty_answer_is_a_refusal():
    with pytest.raises(p.LLMRefused):
        make(FakeTransport(ok_response("   "))).generate_json("x", {})


def test_generate_json_uses_the_same_error_taxonomy():
    """Quota, auth and retirement mean the same three things here as they do
    for a transcription, and the quiz already knows how to degrade on each."""
    with pytest.raises(p.LLMQuotaError):
        make(FakeTransport(*[api_error(429, message="quota exceeded per day")] * 4)
             ).generate_json("x", {})
    with pytest.raises(p.LLMAuthError):
        make(FakeTransport(api_error(403))).generate_json("x", {})
    with pytest.raises(p.LLMModelUnavailable):
        make(FakeTransport(api_error(404))).generate_json("x", {})


def test_generate_json_is_paced_like_every_other_call(monkeypatch):
    slept = []
    provider = p.GeminiProvider(
        "test-key", transport=FakeTransport(ok_response("{}"), ok_response("{}")),
        sleep=slept.append, clock=lambda: 0.0, requests_per_minute=10,
    )
    provider.generate_json("one", {})
    provider.generate_json("two", {})
    assert slept, "the second call must wait for the self-imposed rate"


def test_the_abstract_provider_declares_both_methods():
    assert "generate_json" in p.LLMProvider.__abstractmethods__
    assert "transcribe_image" in p.LLMProvider.__abstractmethods__

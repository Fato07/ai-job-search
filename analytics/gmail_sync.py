from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable
from urllib.parse import urlparse

from analytics.events import mail_event
from analytics.feedback import CATEGORIES, mail_feedback
from analytics.model import hash_source_ref, redact_email_addresses


EXPECTED_ACCOUNT_ALIAS = "job-search"
EXPECTED_MAILBOX = "fathindos.fd@gmail.com"
MAX_EXCERPT_LENGTH = 280
MAX_SPILLED_OUTPUT_BYTES = 20 * 1024 * 1024
_FALLBACK_WORKERS = 4
_MAX_FALLBACK_MESSAGES = 32
_MAX_DISCOVERED_MESSAGES = 2_000
_MAX_QUERY_PAGES = 64
_MAX_DISCOVERY_QUERIES = 32
_COMPANY_QUERY_BATCH_SIZE = 5
_SUBJECT_QUERY_BATCH_SIZE = 3
_DISCOVERY_DEADLINE_SECONDS = 270.0
_COMPOSIO_TIMEOUT_SECONDS = 60.0
_EXCLUDED_ALERT_SENDER = "jobalerts-noreply@linkedin.com"
_SUBJECT_DISCOVERY_TERMS = (
    "your application",
    "application status",
    "thanks for your interest",
    "thank you for applying",
    "job application",
    "application at",
    "application to",
    "next steps",
    "interview",
    "assessment",
    "offer",
)
_ATS_SENDERS = ("ashby", "greenhouse", "lever", "workable", "teamtailor")
_DISCOVERY_QUERY_TERMS = (
    "decided not to move forward",
    "decided not to proceed",
    "move forward with other candidates",
    "move forward",
    "moving forward",
    "will not progress",
    "unable to move forward",
    "proceed with your application",
    "other candidates",
    "application was not selected",
    "received your application",
    "application has been received",
    "application was received",
    "application has been successfully submitted",
    "application was successfully submitted",
    "thank you for applying",
    "invite you to interview",
    "invite you to an interview",
    "interview invitation",
    "schedule an interview",
    "schedule your interview",
    "would like to meet with you",
    "would like to speak with you",
    "application update",
    "application status",
)


class ComposioError(RuntimeError):
    """A safe, payload-free error from the Composio adapter."""



class ComposioUnavailableError(ComposioError):
    """The CLI or authenticated Gmail connection is not currently available."""


_UNAVAILABLE_ERROR = re.compile(
    r"\b(?:not connected|disconnected|no connected account|"
    r"connection (?:is )?(?:missing|not found|inactive|expired)|"
    r"auth(?:entication)? (?:is )?(?:required|expired)|"
    r"(?:token|credentials?) (?:is |are )?expired|unauthori[sz]ed)\b",
    re.IGNORECASE,
)


def _composio_failure(message: str) -> ComposioError:
    if _UNAVAILABLE_ERROR.search(message):
        return ComposioUnavailableError(
            "Composio Gmail connection is unavailable; reconnect account 'job-search'"
        )
    return ComposioError(message)

@dataclass(frozen=True)
class MailSignal:
    occurred_at: str
    company: str
    role: str
    event_type: str
    evidence_tier: str
    category: str
    signal: str
    excerpt: str
    required_action: str
    source_ref: str
    sender: str
    subject: str


@dataclass(frozen=True)
class MatchResult:
    application_id: str | None
    candidates: tuple[str, ...]
    score: float
    reason: str

def _freeze(value: Any, active: set[int] | None = None) -> Any:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise TypeError("SyncProposal floats must be finite")
        return value

    if active is None:
        active = set()
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        marker = id(value)
        if marker in active:
            raise TypeError("SyncProposal values must be acyclic and JSON-like")
        active.add(marker)
        try:
            if isinstance(value, Mapping):
                if any(not isinstance(key, str) for key in value):
                    raise TypeError("SyncProposal mapping keys must be JSON-like strings")
                return MappingProxyType(
                    {key: _freeze(item, active) for key, item in value.items()}
                )
            if isinstance(value, (list, tuple)):
                return tuple(_freeze(item, active) for item in value)
            return frozenset(_freeze(item, active) for item in value)
        finally:
            active.remove(marker)
    raise TypeError(f"SyncProposal values must be JSON-like, not {type(value).__name__}")


@dataclass(frozen=True)
class SyncProposal:
    events: tuple[Mapping[str, object], ...]
    feedback: tuple[Mapping[str, object], ...]
    tracker_updates: tuple[Mapping[str, object], ...]
    review_items: tuple[Mapping[str, object], ...]
    checkpoint: Mapping[str, object]

    def __post_init__(self) -> None:
        for field_name in ("events", "feedback", "tracker_updates", "review_items"):
            rows = getattr(self, field_name)
            object.__setattr__(self, field_name, tuple(_freeze(row) for row in rows))
        object.__setattr__(self, "checkpoint", _freeze(self.checkpoint))


@dataclass(frozen=True)
class MailboxDiscovery:
    proposal: SyncProposal
    scanned: int
    matched: int


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


_URL = re.compile(r"(?i)\bhttps?://\S+")
_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(?P<label>verification\s+code|access\s+token|one[- ]time\s+(?:code|password)|"
    r"passcode|otp|pin|code|token)\s*"
    r"(?P<separator>[:=#]|\s+-\s+|\s+is\s+)\s*"
    r"(?P<value>\S+?)(?P<terminal>[.!?,;]*)(?=\s|$)"
)
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
_WHITESPACE = re.compile(r"\s+")
_UNAMBIGUOUS_SECRET_LABELS = frozenset(
    {
        "access token", "one time code", "one time password", "otp", "passcode",
        "pin", "verification code",
    }
)
_AMBIGUOUS_PROSE_PREDICATES = frozenset(
    {"available", "invalid", "optional", "required", "reviewed", "unavailable", "valid"}
)


def _redact_sensitive_value(match: re.Match[str]) -> str:
    label = " ".join(match.group("label").casefold().replace("-", " ").split())
    predicate = match.group("value").casefold()
    separator = match.group("separator").strip().casefold()
    if (
        label not in _UNAMBIGUOUS_SECRET_LABELS
        and separator == "is"
        and predicate in _AMBIGUOUS_PROSE_PREDICATES
    ):
        return match.group(0)
    return f"{match.group('label')} [removed]{match.group('terminal')}"


_REJECTION_PATTERNS = (
    re.compile(r"\bwe (?:have )?decided not to (?:move forward|proceed)\b", re.I),
    re.compile(r"\bwe (?:have )?chosen to move forward with other candidates\b", re.I),
    re.compile(r"\bwe (?:will not|won't) (?:be )?(?:move|moving) forward\b", re.I),
    re.compile(r"\bwe will not progress\b", re.I),
    re.compile(r"\bwe (?:are )?unable to move forward\b", re.I),
    re.compile(r"\bnot (?:to )?proceed with your application\b", re.I),
    re.compile(r"\bother candidates (?:were|are|whose experience is) a closer match\b", re.I),
    re.compile(r"\byour application was not selected\b", re.I),
)
_CONFIRMATION_PATTERNS = (
    re.compile(r"\bwe (?:have )?received your application\b", re.I),
    re.compile(r"\byour application (?:has been|was) received\b", re.I),
    re.compile(r"\bapplication (?:has been|was) successfully submitted\b", re.I),
    re.compile(r"\bthank you for applying\b", re.I),
)
_INTERVIEW_PATTERNS = (
    re.compile(r"\binvite you to (?:an )?interview\b", re.I),
    re.compile(r"\binterview invitation\b", re.I),
    re.compile(r"\bschedule (?:an|your) interview\b", re.I),
    re.compile(r"\bwould like to (?:meet|speak) with you\b", re.I),
)
_LOGISTICS = re.compile(
    r"\b(?:work authori[sz]ation|visa sponsorship|cannot sponsor|unable to sponsor|"
    r"immigration support|office attendance|relocat(?:e|ion)|hybrid requirement)\b",
    re.I,
)
_ML_EVALUATION = re.compile(
    r"\b(?:genai|generative ai|machine learning|ml)\b.{0,80}\b(?:evaluation|experimentation)\b|"
    r"\b(?:evaluation|experimentation)\b.{0,80}\b(?:genai|generative ai|machine learning|ml)\b",
    re.I,
)
_TECHNICAL = re.compile(r"\b(?:technical depth|hands-on technical|systems design|coding depth)\b", re.I)
_ROLE_ALIGNMENT = re.compile(r"\b(?:seniority|leadership scope|role-specific experience)\b", re.I)

_COMPANY_ALIASES = {
    "bolt": "bolt",
    "bolt new": "bolt",
    "stackblitz": "bolt",
    "stackblitz bolt new": "bolt",
    "luminor": "luminor",
    "luminor bank": "luminor",
    "luminor group": "luminor",
    "robco": "robco",
    "robco gmbh": "robco",
}
_COMPANY_SUFFIXES = frozenset(
    {"gmbh", "inc", "limited", "llc", "ltd", "oy", "plc", "talent", "team", "recruiting", "careers"}
)
_ROLE_STOPWORDS = frozenset({"m", "f", "d", "role", "position"})
_ROLE_MARKERS = frozenset(
    {
        "analyst", "architect", "consultant", "developer", "director", "engineer",
        "engineering", "lead", "manager", "scientist", "security", "specialist",
    }
)
_JOB_METADATA = re.compile(
    r"\b(?:application|candidate|interview|recruit(?:er|ing)?|talent|hiring|position|role|careers?)\b",
    re.I,
)


def _diagnostic(value: object, fallback: str) -> str:
    if isinstance(value, Mapping):
        value = value.get("message") or value.get("error") or value.get("code")
    if not isinstance(value, str) or not value.strip():
        return fallback
    return _WHITESPACE.sub(" ", value).strip()[:500]


def _failed(result: Mapping[str, object]) -> bool:
    return result.get("successful") is False or result.get("success") is False


def unwrap_composio_result(result: Mapping[str, object]) -> dict:
    """Return inline/parallel/spilled JSON while keeping failures payload-free."""
    if not isinstance(result, Mapping):
        raise ComposioError("Composio result must be a JSON object")
    if _failed(result):
        message = _diagnostic(result.get("error"), "Composio execution failed")
        raise _composio_failure(message)

    if result.get("storedInFile") is True:
        raw_path = result.get("outputFilePath")
        if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
            raise ComposioError("Composio spilled output path is invalid")
        path = Path(raw_path).expanduser()
        try:
            if path.suffix.casefold() != ".json" or not path.is_file():
                raise ComposioError("Composio spilled output is not an existing JSON file")
            if path.stat().st_size > MAX_SPILLED_OUTPUT_BYTES:
                raise ComposioError("Composio spilled output exceeds the size limit")
            with path.open(encoding="utf-8") as handle:
                loaded = json.load(handle)
        except ComposioError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ComposioError("Composio spilled output is not valid JSON") from None
        if not isinstance(loaded, Mapping):
            raise ComposioError("Composio spilled output must contain a JSON object")
        return unwrap_composio_result(loaded)

    parallel = result.get("results")
    if parallel is not None:
        if isinstance(parallel, (str, bytes)) or not isinstance(parallel, Sequence):
            raise ComposioError("Composio parallel results must be a JSON array")
        unwrapped: list[dict] = []
        for item in parallel:
            if not isinstance(item, Mapping):
                raise ComposioError("Composio parallel result must be a JSON object")
            unwrapped.append(unwrap_composio_result(item))
        normalized = dict(result)
        normalized["results"] = unwrapped
        return normalized

    return dict(result)


class ComposioClient:
    def __init__(self, account: str = EXPECTED_ACCOUNT_ALIAS):
        if account != EXPECTED_ACCOUNT_ALIAS:
            raise ValueError("Gmail reads require the Composio account alias 'job-search'")
        self.account = account

    def execute(
        self,
        slug: str,
        data: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> dict:
        timeout = (
            _COMPOSIO_TIMEOUT_SECONDS
            if timeout_seconds is None
            else min(_COMPOSIO_TIMEOUT_SECONDS, max(0.001, timeout_seconds))
        )
        try:
            completed = subprocess.run(
                [
                    "composio",
                    "execute",
                    slug,
                    "--account",
                    self.account,
                    "-d",
                    json.dumps(data, separators=(",", ":")),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ComposioError("Composio command timed out") from None
        except subprocess.CalledProcessError as exc:
            output = " ".join(
                str(value or "")
                for value in (getattr(exc, "stderr", ""), getattr(exc, "stdout", ""))
            )
            failure = _composio_failure(output)
            if isinstance(failure, ComposioUnavailableError):
                raise failure from None
            raise ComposioError("Composio command failed") from None
        except FileNotFoundError:
            raise ComposioUnavailableError("Composio CLI is unavailable") from None
        except OSError:
            raise ComposioError("Composio command failed") from None

        try:
            parsed = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            raise ComposioError("Composio returned invalid JSON") from None
        if not isinstance(parsed, Mapping):
            raise ComposioError("Composio returned a non-object JSON result")
        return unwrap_composio_result(parsed)


def _execute_with_retry(
    client: ComposioClient,
    slug: str,
    data: Mapping[str, object],
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    for attempt in range(2):
        remaining = None if deadline is None else deadline - monotonic()
        if remaining is not None and remaining <= 0:
            raise ComposioError("Gmail refresh deadline exceeded")
        try:
            if isinstance(client, ComposioClient):
                result = client.execute(
                    slug,
                    data,
                    timeout_seconds=remaining,
                )
            else:
                result = client.execute(slug, data)
            if deadline is not None and monotonic() >= deadline:
                raise ComposioError("Gmail refresh deadline exceeded")
            return result
        except ComposioError:
            if deadline is not None and monotonic() >= deadline:
                raise ComposioError("Gmail refresh deadline exceeded") from None
            if attempt:
                raise
    raise AssertionError("unreachable")


def verify_mailbox(
    client: ComposioClient,
    expected_address: str,
    *,
    execute: Callable[[str, Mapping[str, object]], dict] | None = None,
) -> None:
    if expected_address.strip().casefold() != EXPECTED_MAILBOX:
        raise ComposioError("Gmail reads require the configured expected mailbox")
    if getattr(client, "account", None) != EXPECTED_ACCOUNT_ALIAS:
        raise ComposioError("Composio account alias must be 'job-search'")
    runner = execute or (
        lambda slug, data: _execute_with_retry(client, slug, data)
    )
    profile = runner("GMAIL_GET_PROFILE", {"user_id": "me"})
    data = profile.get("data")
    address = data.get("emailAddress") if isinstance(data, Mapping) else None
    if not isinstance(address, str) or address.strip().casefold() != expected_address.strip().casefold():
        raise ComposioError("Composio mailbox mismatch")


def _html_text(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return value
    return " ".join(parser.parts)


def _sanitize_text(value: object, limit: int | None = None) -> str:
    if not isinstance(value, str):
        return ""
    text = _html_text(value)
    text = _URL.sub("[link]", text)
    text = redact_email_addresses(text)
    text = _SENSITIVE_VALUE.sub(_redact_sensitive_value, text)
    text = _PHONE.sub("[number removed]", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _full_body(message: Mapping[str, object]) -> str:
    for key in ("messageText", "text", "body"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return _sanitize_text(value)
    payload = message.get("payload")
    if isinstance(payload, Mapping):
        return _sanitize_text(_mime_text(payload))
    return ""


def _message_body(message: Mapping[str, object]) -> str:
    body = _full_body(message)
    if body:
        return body
    return _sanitize_text(message.get("snippet"))


def _sender_display(value: object) -> str:
    text = _sanitize_text(value, 160)
    text = re.sub(r"\s*<[^>]*>\s*", " ", text)
    text = _WHITESPACE.sub(" ", text).strip(" <>,-")
    return text or "unknown"


def _looks_like_role(value: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    return 1 < len(tokens) <= 16 and bool(set(tokens) & _ROLE_MARKERS)


def _extract_role(message: Mapping[str, object], body: str, subject: str) -> str:
    supplied = _sanitize_text(message.get("role"), 200)
    if supplied:
        return supplied

    subject_patterns = (
        re.compile(r"(?i)^(.{2,120}?)\s*(?:[-:|]\s*)application\s+(?:update|status)\b"),
        re.compile(r"(?i)^update (?:on|for) (?:your )?(.{2,120}?) application\b"),
    )
    for pattern in subject_patterns:
        match = pattern.search(subject)
        if match:
            candidate = _sanitize_text(match.group(1), 200).strip(" ,.-")
            if _looks_like_role(candidate):
                return candidate

    body_patterns = (
        re.compile(r"(?i)\bapplication for (?:the )?(.{2,120}?)(?: role| position|[.!])"),
        re.compile(r"(?i)\bfor (?:the )?(.{2,120}?) role\b"),
    )
    for pattern in body_patterns:
        match = pattern.search(body)
        if match:
            return _sanitize_text(match.group(1), 200).strip(" ,.-")

    standalone = re.match(r"(?i)^(.{2,120}?)\.\s+(?=we\b)", body)
    if standalone:
        candidate = _sanitize_text(standalone.group(1), 200).strip(" ,.-")
        if _looks_like_role(candidate):
            return candidate
    return ""


def _extract_company(message: Mapping[str, object], sender: str) -> str:
    supplied = _sanitize_text(message.get("company"), 160)
    if supplied:
        return supplied
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", sender.casefold())
        if word not in _COMPANY_SUFFIXES and word not in {"no", "reply"}
    ]
    return " ".join(words).strip()


def classify_message(message: Mapping[str, object]) -> MailSignal | None:
    if not isinstance(message, Mapping):
        raise TypeError("message must be a mapping")
    body = _message_body(message)
    subject = _sanitize_text(message.get("subject"), 200)
    searchable = f"{subject} {body}".strip()

    event_type: str | None = None
    if any(pattern.search(searchable) for pattern in _REJECTION_PATTERNS):
        event_type = "rejected"
    elif any(pattern.search(searchable) for pattern in _INTERVIEW_PATTERNS):
        event_type = "interview"
    elif any(pattern.search(searchable) for pattern in _CONFIRMATION_PATTERNS):
        event_type = "received"
    if event_type is None:
        return None

    raw_source = message.get("messageId") or message.get("threadId") or message.get("id")
    if not isinstance(raw_source, str) or not raw_source.strip():
        raise ValueError("candidate Gmail message is missing a source identity")

    if event_type == "rejected" and _LOGISTICS.search(body):
        evidence_tier = "explicit"
        category = "logistics_work_authorization"
        signal = "The role had an explicit location, work-authorization, or sponsorship constraint."
        required_action = "Verify location, work authorization, and sponsorship constraints before drafting."
    elif event_type == "rejected" and _ML_EVALUATION.search(body):
        evidence_tier = "explicit"
        category = "ml_genai_evaluation"
        signal = "Selected candidates had stronger hands-on ML/GenAI evaluation evidence."
        required_action = "Lead with hands-on ML/GenAI evaluation evidence for evaluation-heavy roles."
    elif event_type == "rejected" and _TECHNICAL.search(body):
        evidence_tier = "explicit"
        category = "technical_depth"
        signal = "The rejection identified a role-specific technical-depth gap."
        required_action = "Lead with role-specific technical depth evidence."
    elif event_type == "rejected" and _ROLE_ALIGNMENT.search(body):
        evidence_tier = "explicit"
        category = "role_seniority_alignment"
        signal = "The rejection identified a role or seniority alignment gap."
        required_action = "Use role-specific evidence that matches the opening's seniority and scope."
    elif event_type == "rejected":
        evidence_tier = "boilerplate"
        category = "competition_no_specific_signal"
        signal = "Other candidates were a closer match."
        required_action = ""
    elif event_type == "interview":
        evidence_tier = "explicit"
        category = "interview_invitation"
        signal = "The employer invited the candidate to interview."
        required_action = "Review interview logistics and respond."
    else:
        evidence_tier = "explicit"
        category = "application_status"
        signal = "The employer confirmed receipt of the application."
        required_action = ""

    sender = _sender_display(message.get("sender"))
    return MailSignal(
        occurred_at=_sanitize_text(message.get("messageTimestamp") or message.get("occurred_at"), 40),
        company=_extract_company(message, sender),
        role=_extract_role(message, body, subject),
        event_type=event_type,
        evidence_tier=evidence_tier,
        category=category,
        signal=signal,
        excerpt=_sanitize_text(body, MAX_EXCERPT_LENGTH),
        required_action=required_action,
        source_ref=hash_source_ref(raw_source.strip()),
        sender=sender,
        subject=subject,
    )


def _canonical_company(value: object) -> str:
    normalized = " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))
    if normalized in _COMPANY_ALIASES:
        return _COMPANY_ALIASES[normalized]
    words = [word for word in normalized.split() if word not in _COMPANY_SUFFIXES]
    reduced = " ".join(words)
    return _COMPANY_ALIASES.get(reduced, reduced)


def _role_tokens(value: object) -> frozenset[str]:
    return frozenset(
        token
        for token in re.findall(r"[a-z0-9]+", str(value).casefold())
        if token not in _ROLE_STOPWORDS
    )


def _canonical_role(value: object) -> str:
    return " ".join(sorted(_role_tokens(value)))


def _strong_role_overlap(left: object, right: object) -> bool:
    left_tokens = _role_tokens(left)
    right_tokens = _role_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens) >= 0.60


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _recognized_sender(sender: str, company: str) -> bool:
    sender_tokens = set(re.findall(r"[a-z0-9]+", sender.casefold()))
    company_tokens = set(_canonical_company(company).split())
    if company_tokens and company_tokens <= sender_tokens:
        return True
    return bool(sender_tokens & {"ashby", "greenhouse", "lever", "workable", "teamtailor"})


def match_application(
    signal: MailSignal,
    applications: Sequence[Mapping[str, object]],
) -> MatchResult:
    signal_company = _canonical_company(signal.company)
    message_date = _parse_date(signal.occurred_at)
    scored: list[tuple[float, str, bool]] = []

    for application in applications:
        application_id = application.get("application_id")
        if not isinstance(application_id, str) or not application_id:
            continue
        if not signal_company or _canonical_company(application.get("company", "")) != signal_company:
            continue

        score = 0.50
        if signal.role and _canonical_role(signal.role) == _canonical_role(application.get("role", "")):
            score += 0.35
        elif signal.role and _strong_role_overlap(signal.role, application.get("role", "")):
            score += 0.25

        discovered = _parse_date(application.get("discovered_at"))
        chronological = message_date is not None and discovered is not None and message_date >= discovered
        if chronological:
            score += 0.10
        if _recognized_sender(signal.sender, signal.company):
            score += 0.05
        scored.append((round(score, 2), application_id, chronological))

    if not scored:
        return MatchResult(None, (), 0.0, "no canonical company match")

    scored.sort(key=lambda item: (-item[0], item[1]))
    candidates = tuple(item[1] for item in scored)
    best_score, best_id, best_chronological = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    best_count = sum(score == best_score for score, _, _ in scored)

    if not best_chronological:
        return MatchResult(None, candidates, best_score, "message predates candidate application")
    if best_score < 0.85:
        detail = " and no unique best" if best_count != 1 else ""
        return MatchResult(None, candidates, best_score, f"ambiguous: score below 0.85{detail}")
    if best_count != 1:
        return MatchResult(None, candidates, best_score, "ambiguous: no unique best application")
    if round(best_score - second_score, 2) < 0.20:
        return MatchResult(None, candidates, best_score, "ambiguous: score margin below 0.20")
    return MatchResult(best_id, candidates, best_score, "unique high-confidence match")


def _result_data(result: Mapping[str, object]) -> Mapping[str, object]:
    data = result.get("data")
    if isinstance(data, Mapping):
        message = data.get("message")
        return message if isinstance(message, Mapping) else data
    return result


def _message_identity(message: Mapping[str, object]) -> str:
    display_url = message.get("display_url")
    if isinstance(display_url, str):
        parsed = urlparse(display_url)
        fragment_parts = [part for part in parsed.fragment.split("/") if part]
        if (
            parsed.scheme == "https"
            and parsed.hostname == "mail.google.com"
            and fragment_parts
            and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", fragment_parts[-1])
        ):
            return fragment_parts[-1]
    value = message.get("messageId") or message.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ComposioError("Gmail metadata is missing a message identity")
    return value.strip()


def _metadata_messages(result: Mapping[str, object]) -> tuple[list[Mapping[str, object]], str]:
    data = _result_data(result)
    messages = data.get("messages")
    if messages is None:
        messages = result.get("messages")
    if not isinstance(messages, list) or any(not isinstance(item, Mapping) for item in messages):
        raise ComposioError("Gmail metadata response has an invalid messages list")
    token = data.get("nextPageToken") or result.get("nextPageToken") or ""
    if not isinstance(token, str):
        raise ComposioError("Gmail metadata response has an invalid page token")
    return list(messages), token


def _decode_body_data(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        raw = value.encode("ascii")
        raw += b"=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(raw).decode("utf-8", errors="replace")
    except (UnicodeEncodeError, ValueError):
        return ""


def _mime_text(part: Mapping[str, object]) -> str:
    mime_type = part.get("mimeType")
    body = part.get("body")
    if isinstance(mime_type, str) and mime_type.casefold() in {"text/plain", "text/html"}:
        if isinstance(body, Mapping):
            text = _decode_body_data(body.get("data"))
            if text:
                return text
    parts = part.get("parts")
    if not isinstance(parts, list):
        return ""
    plain: list[str] = []
    html: list[str] = []
    for child in parts:
        if not isinstance(child, Mapping):
            continue
        text = _mime_text(child)
        if not text:
            continue
        if str(child.get("mimeType", "")).casefold() == "text/html":
            html.append(text)
        else:
            plain.append(text)
    return "\n".join(plain or html)


def _headers(payload: Mapping[str, object]) -> dict[str, str]:
    values: dict[str, str] = {}
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return values
    for header in headers:
        if not isinstance(header, Mapping):
            continue
        name = header.get("name")
        value = header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            values[name.casefold()] = value
    return values


def _message_timestamp(message: Mapping[str, object], headers: Mapping[str, str]) -> str:
    value = message.get("messageTimestamp") or message.get("occurred_at")
    if isinstance(value, str) and value:
        return value
    internal = message.get("internalDate")
    if isinstance(internal, (str, int)):
        try:
            return (
                datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
        except (ValueError, OverflowError):
            pass
    date_header = headers.get("date")
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
        except (TypeError, ValueError, OverflowError):
            pass
    return ""


def _normalize_full_message(
    result: Mapping[str, object],
    metadata: Mapping[str, object],
    message_id: str,
) -> dict[str, object]:
    data = dict(_result_data(result))
    payload = data.get("payload")
    headers = _headers(payload) if isinstance(payload, Mapping) else {}
    normalized = dict(metadata)
    normalized.update(data)
    normalized["messageId"] = message_id
    normalized["subject"] = data.get("subject") or headers.get("subject") or metadata.get("subject", "")
    normalized["sender"] = data.get("sender") or headers.get("from") or metadata.get("sender", "")
    normalized["messageTimestamp"] = _message_timestamp(normalized, headers)
    if not _message_body(normalized) and isinstance(payload, Mapping):
        normalized["messageText"] = _mime_text(payload)
    return normalized


def _normalize_bulk_message(message: Mapping[str, object]) -> dict[str, object]:
    message_id = _message_identity(message)
    return _normalize_full_message(message, message, message_id)


def _needs_feedback_body(message: Mapping[str, object]) -> bool:
    if _message_body(message):
        return False
    subject = _sanitize_text(message.get("subject"), 200)
    return any(pattern.search(subject) for pattern in _REJECTION_PATTERNS)


def _hydrate_message(
    execute: Callable[[str, Mapping[str, object]], dict],
    metadata: Mapping[str, object],
) -> dict[str, object]:
    message_id = _message_identity(metadata)
    result = execute(
        "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
        {"message_id": message_id, "user_id": "me", "format": "full"},
    )
    return _normalize_full_message(result, metadata, message_id)


def _candidate_metadata(message: Mapping[str, object]) -> bool:
    try:
        if classify_message(message) is not None:
            return True
    except ValueError:
        pass
    metadata_text = " ".join(
        _sanitize_text(message.get(field), 240)
        for field in ("subject", "sender", "snippet")
    )
    return _JOB_METADATA.search(metadata_text) is not None


def _scan_start(
    applications: Sequence[Mapping[str, object]],
    checkpoint: Mapping[str, object],
) -> date:
    last_successful = checkpoint.get("last_successful_at")
    if last_successful is None:
        discovered = sorted(
            str(item.get("discovered_at"))
            for item in applications
            if isinstance(item.get("discovered_at"), str) and item.get("discovered_at")
        )
        if not discovered:
            raise ValueError("first Gmail scan requires a tracker discovered_at")
        try:
            return date.fromisoformat(discovered[0]) - timedelta(days=1)
        except ValueError as exc:
            raise ValueError("tracker discovered_at must be an ISO date") from exc
    if isinstance(last_successful, str):
        try:
            parsed = datetime.fromisoformat(last_successful.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("checkpoint last_successful_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("checkpoint last_successful_at must be timezone-aware")
        return parsed.astimezone(timezone.utc).date() - timedelta(days=8)
    raise ValueError("checkpoint last_successful_at must be a string or null")


def _query_phrase(value: object) -> str:
    normalized = " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))
    return f'"{normalized}"' if normalized else ""


def _scan_queries(
    applications: Sequence[Mapping[str, object]],
    checkpoint: Mapping[str, object],
) -> tuple[str, ...]:
    after = f"after:{_scan_start(applications, checkpoint):%Y/%m/%d}"
    terms = " ".join(f'"{term}"' for term in _DISCOVERY_QUERY_TERMS)
    queries = [f"{after} {{{terms}}}"]
    for offset in range(0, len(_SUBJECT_DISCOVERY_TERMS), _SUBJECT_QUERY_BATCH_SIZE):
        batch = _SUBJECT_DISCOVERY_TERMS[
            offset:offset + _SUBJECT_QUERY_BATCH_SIZE
        ]
        subject_terms = " ".join(f'subject:"{term}"' for term in batch)
        queries.append(
            f"{after} {{{subject_terms}}} -from:{_EXCLUDED_ALERT_SENDER}"
        )
    queries.extend(
        f"{after} from:{sender}"
        for sender in _ATS_SENDERS
    )
    submitted = [
        application
        for application in applications
        if str(application.get("submitted_at") or "").strip()
    ]
    submitted.sort(
        key=lambda application: (
            _query_phrase(application.get("company")),
            str(application.get("application_id") or ""),
        )
    )
    submitted.sort(
        key=lambda application: str(application.get("submitted_at") or ""),
        reverse=True,
    )
    submitted.sort(
        key=lambda application: (
            str(application.get("stage") or "").strip().casefold() != "closed"
        ),
        reverse=True,
    )
    companies = list(dict.fromkeys(
        phrase
        for application in submitted
        if (phrase := _query_phrase(application.get("company")))
    ))
    company_capacity = max(
        0, (_MAX_DISCOVERY_QUERIES - len(queries)) * _COMPANY_QUERY_BATCH_SIZE
    )
    for offset in range(
        0,
        min(len(companies), company_capacity),
        _COMPANY_QUERY_BATCH_SIZE,
    ):
        batch = companies[offset:offset + _COMPANY_QUERY_BATCH_SIZE]
        queries.append(
            f"{after} {{{' '.join(f'from:{company}' for company in batch)}}} "
            "{application candidate interview hiring}"
        )
    return tuple(queries)


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _signal_date(signal: MailSignal) -> str:
    try:
        parsed = datetime.fromisoformat(signal.occurred_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("mail signal occurred_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("mail signal occurred_at must be timezone-aware")
    return parsed.astimezone(timezone.utc).date().isoformat()


def _tracker_update(signal: MailSignal, application_id: str) -> dict[str, object]:
    occurred_date = _signal_date(signal)
    if signal.event_type == "received":
        return {
            "application_id": application_id,
            "stage": "submitted",
            "status": f"APPLICATION RECEIVED {occurred_date}",
            "status_updated_at": occurred_date,
            "submitted_at": occurred_date,
        }
    if signal.event_type == "interview":
        return {
            "application_id": application_id,
            "stage": "interview",
            "status": f"INTERVIEW {occurred_date}",
            "status_updated_at": occurred_date,
        }
    if signal.event_type == "rejected":
        return {
            "application_id": application_id,
            "stage": "closed",
            "status": f"REJECTED {occurred_date}",
            "status_updated_at": occurred_date,
        }
    raise ValueError(f"unsupported mail event type: {signal.event_type!r}")


def _review_item(signal: MailSignal, match: MatchResult) -> dict[str, object]:
    material = "\x1f".join((signal.source_ref, match.reason))
    return {
        "review_id": f"review-{hashlib.sha256(material.encode('utf-8')).hexdigest()}",
        "occurred_at": signal.occurred_at,
        "sender": signal.sender,
        "subject": signal.subject,
        "company": signal.company,
        "role": signal.role,
        "candidate_application_ids": json.dumps(
            list(match.candidates), separators=(",", ":")
        ),
        "reason": match.reason,
        "source_ref": signal.source_ref,
        "status": "pending",
    }


def discover_mailbox(
    client: ComposioClient,
    applications: Sequence[Mapping[str, object]],
    checkpoint: Mapping[str, object],
    now: datetime,
) -> MailboxDiscovery:
    monotonic = time.monotonic
    deadline = monotonic() + _DISCOVERY_DEADLINE_SECONDS

    def execute(slug: str, data: Mapping[str, object]) -> dict:
        return _execute_with_retry(
            client,
            slug,
            data,
            deadline=deadline,
            monotonic=monotonic,
        )

    verify_mailbox(client, EXPECTED_MAILBOX, execute=execute)
    queries = _scan_queries(applications, checkpoint)
    messages_for_classification: list[dict[str, object]] = []
    seen_source_identities: set[str] = set()
    total_pages = 0
    for query_index, query in enumerate(queries, start=1):
        seen_page_tokens: set[str] = set()
        page_token = ""
        while True:
            total_pages += 1
            if total_pages > _MAX_QUERY_PAGES:
                raise ComposioError("Gmail discovery page limit exceeded")
            request: dict[str, object] = {
                "query": query,
                "user_id": "me",
                "verbose": True,
                "ids_only": False,
                "label_ids": [],
                "max_results": 500,
                "include_payload": False,
                "include_spam_trash": False,
            }
            if page_token:
                request["page_token"] = page_token
            try:
                page = execute("GMAIL_FETCH_EMAILS", request)
            except ComposioError as exc:
                raise ComposioError(
                    f"Gmail discovery query {query_index} failed: {exc}"
                ) from None
            messages, next_page_token = _metadata_messages(page)
            for message in messages:
                source_identity = hash_source_ref(_message_identity(message))
                if source_identity in seen_source_identities:
                    continue
                seen_source_identities.add(source_identity)
                if len(seen_source_identities) > _MAX_DISCOVERED_MESSAGES:
                    raise ComposioError("Gmail discovery message limit exceeded")
                messages_for_classification.append(_normalize_bulk_message(message))
            if not next_page_token:
                break
            if next_page_token in seen_page_tokens:
                raise ComposioError("Gmail pagination repeated a page token")
            seen_page_tokens.add(next_page_token)
            page_token = next_page_token
    fallback = [
        (index, message)
        for index, message in enumerate(messages_for_classification)
        if not _full_body(message) and _candidate_metadata(message)
    ]
    if len(fallback) > _MAX_FALLBACK_MESSAGES:
        raise ComposioError("Gmail missing-body fallback limit exceeded")
    if fallback:
        with ThreadPoolExecutor(
            max_workers=min(_FALLBACK_WORKERS, len(fallback))
        ) as executor:
            futures = [
                executor.submit(_hydrate_message, execute, metadata)
                for _, metadata in fallback
            ]
            hydrated = [future.result() for future in futures]
        for (index, _), message in zip(fallback, hydrated, strict=True):
            if not _full_body(message):
                raise ComposioError("Gmail candidate content unavailable")
            messages_for_classification[index] = message

    created_at = _utc_timestamp(now)
    applications_by_id = {
        str(application.get("application_id")): application
        for application in applications
        if application.get("application_id")
    }
    events: list[Mapping[str, object]] = []
    feedback: list[Mapping[str, object]] = []
    tracker_updates: list[Mapping[str, object]] = []
    review_items: list[Mapping[str, object]] = []
    matched = 0
    seen_source_refs: set[str] = set()

    for message in messages_for_classification:
        signal = classify_message(message)
        if signal is None or signal.source_ref in seen_source_refs:
            continue
        seen_source_refs.add(signal.source_ref)
        match = match_application(signal, applications)
        if match.application_id is None:
            review_items.append(_review_item(signal, match))
            continue
        application = applications_by_id.get(match.application_id)
        if application is None:
            raise ValueError("mail match references an unknown application")
        matched += 1
        tracker_updates.append(_tracker_update(signal, match.application_id))
        events.append(
            mail_event(
                application_id=match.application_id,
                occurred_at=signal.occurred_at,
                event_type=signal.event_type,
                detail=signal.signal,
                source_ref=signal.source_ref,
                created_at=created_at,
            )
        )
        if signal.category in CATEGORIES:
            feedback.append(
                mail_feedback(
                    application=application,
                    occurred_at=signal.occurred_at,
                    evidence_tier=signal.evidence_tier,
                    category=signal.category,
                    signal=signal.signal,
                    evidence_excerpt=signal.excerpt,
                    required_action=signal.required_action,
                    confidence=match.score,
                    source_ref=signal.source_ref,
                    created_at=created_at,
                )
            )

    proposal = SyncProposal(
        tuple(events),
        tuple(feedback),
        tuple(tracker_updates),
        tuple(review_items),
        {"last_successful_at": created_at},
    )
    return MailboxDiscovery(
        proposal=proposal,
        scanned=len(messages_for_classification),
        matched=matched,
    )

from __future__ import annotations

import json
import math
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from types import MappingProxyType
from typing import Any

from analytics.model import hash_source_ref


EXPECTED_ACCOUNT_ALIAS = "job-search"
EXPECTED_MAILBOX = "fathindos.fd@gmail.com"
MAX_EXCERPT_LENGTH = 280
MAX_SPILLED_OUTPUT_BYTES = 20 * 1024 * 1024


class ComposioError(RuntimeError):
    """A safe, payload-free error from the Composio adapter."""


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


_EMAIL = re.compile(r"(?i)\b[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[a-z]{2,}\b")
_URL = re.compile(r"(?i)\bhttps?://\S+")
_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(?P<label>verification\s+code|access\s+token|one[- ]time\s+(?:code|password)|"
    r"passcode|otp|pin|code|token)\s*"
    r"(?P<separator>[:=#]|\s+-\s+|\s+is\s+)\s*"
    r"(?P<value>\S+?)(?P<terminal>[.!?,;]?)(?=\s|$)"
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
        raise ComposioError(_diagnostic(result.get("error"), "Composio execution failed"))

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

    def execute(self, slug: str, data: Mapping[str, object]) -> dict:
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
            )
        except subprocess.CalledProcessError as exc:
            diagnostic = _diagnostic(exc.stderr, "Composio command failed")
            raise ComposioError(diagnostic) from None
        except OSError as exc:
            raise ComposioError(_diagnostic(str(exc), "Composio CLI is unavailable")) from None

        try:
            parsed = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            raise ComposioError("Composio returned invalid JSON") from None
        if not isinstance(parsed, Mapping):
            raise ComposioError("Composio returned a non-object JSON result")
        return unwrap_composio_result(parsed)


def verify_mailbox(client: ComposioClient, expected_address: str) -> None:
    if expected_address.strip().casefold() != EXPECTED_MAILBOX:
        raise ComposioError("Gmail reads require the configured expected mailbox")
    if getattr(client, "account", None) != EXPECTED_ACCOUNT_ALIAS:
        raise ComposioError("Composio account alias must be 'job-search'")
    profile = client.execute("GMAIL_GET_PROFILE", {"user_id": "me"})
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
    text = _EMAIL.sub("[address removed]", text)
    text = _SENSITIVE_VALUE.sub(_redact_sensitive_value, text)
    text = _PHONE.sub("[number removed]", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _message_body(message: Mapping[str, object]) -> str:
    for key in ("messageText", "text", "body", "snippet"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return _sanitize_text(value)
    return ""


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

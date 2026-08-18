"""Password protection for the dashboard when it listens on the network.

The dashboard is read-only, but ``/api/run`` starts real CLI subprocesses, so a
listener bound to a LAN address must not answer strangers. This module holds the
credential store and the request gate; the HTTP wiring lives in ``dashboard``.

The password is never written to the repository. It is kept as a salted PBKDF2
digest under ``~/.config/federal-per-diem/``, readable only by its owner, and
can be overridden for one run through the environment.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from hashlib import pbkdf2_hmac, sha256
from ipaddress import ip_address
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)

REALM = "Federal Per Diem Dashboard"

ENV_PASSWORD = "FEDERAL_PER_DIEM_DASHBOARD_PASSWORD"
ENV_AUTH_FILE = "FEDERAL_PER_DIEM_DASHBOARD_AUTH_FILE"

ALGORITHM = "pbkdf2_sha256"
PBKDF2_ROUNDS = 600_000
SALT_BYTES = 16

# A wrong password costs the caller half a second, and ten wrong passwords cost
# them a minute. Online guessing against a short password stays impractical
# without making an honest typo annoying.
FAILURE_DELAY_SECONDS = 0.5
LOCKOUT_THRESHOLD = 10
LOCKOUT_SECONDS = 60.0
FAILURE_WINDOW_SECONDS = 300.0

# HTTP Basic re-sends the password on every request, and one page load pulls
# dozens of assets. Deriving the digest each time would spend real time per
# asset, so a correct password is memoised under a per-process key for a while.
VERIFIED_CACHE_SECONDS = 900.0
VERIFIED_CACHE_ENTRIES = 64


class Decision(Enum):
    """Why the gate let a request through, or why it did not."""

    EXEMPT = "exempt"
    AUTHENTICATED = "authenticated"
    CHALLENGE = "challenge"
    REJECTED = "rejected"
    LOCKED_OUT = "locked-out"


@dataclass(frozen=True, slots=True)
class Credential:
    """A stored password digest."""

    algorithm: str
    rounds: int
    salt: bytes
    digest: bytes

    @classmethod
    def create(cls, password: str, *, rounds: int = PBKDF2_ROUNDS) -> "Credential":
        if not password:
            raise ValueError("The password must not be empty")
        salt = secrets.token_bytes(SALT_BYTES)
        return cls(
            algorithm=ALGORITHM,
            rounds=rounds,
            salt=salt,
            digest=_derive(password, salt, rounds),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Credential":
        algorithm = str(payload.get("algorithm", ""))
        if algorithm != ALGORITHM:
            raise ValueError(f"Unsupported password algorithm {algorithm!r}")
        try:
            rounds = int(payload["rounds"])
            salt = bytes.fromhex(str(payload["salt"]))
            digest = bytes.fromhex(str(payload["hash"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("The stored credential is malformed") from exc
        if rounds < 1 or not salt or not digest:
            raise ValueError("The stored credential is malformed")
        return cls(algorithm=algorithm, rounds=rounds, salt=salt, digest=digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "rounds": self.rounds,
            "salt": self.salt.hex(),
            "hash": self.digest.hex(),
        }

    def verify(self, password: str) -> bool:
        """Return whether ``password`` matches, in constant time."""

        candidate = _derive(password, self.salt, self.rounds)
        return hmac.compare_digest(candidate, self.digest)


def _derive(password: str, salt: bytes, rounds: int) -> bytes:
    return pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)


def credentials_path() -> Path:
    """Where the password digest lives, outside the source tree."""

    override = os.getenv(ENV_AUTH_FILE)
    if override:
        return Path(override).expanduser()
    base = os.getenv("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "federal-per-diem" / "dashboard-auth.json"


def save_password(password: str, *, path: Path | None = None) -> Path:
    """Store ``password`` as a salted digest that only its owner can read."""

    target = path or credentials_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(Credential.create(password).to_dict(), indent=2) + "\n"
    # Create the file private, then write. Writing first would leave the digest
    # briefly world-readable under a permissive umask.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(target, 0o600)
    return target


def load_credential(*, path: Path | None = None) -> Credential | None:
    """Load the configured password, preferring the environment override."""

    injected = os.getenv(ENV_PASSWORD)
    if injected:
        return Credential.create(injected)
    target = path or credentials_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"Cannot read {target}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{target} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{target} does not contain a credential object")
    return Credential.from_dict(payload)


def is_loopback(host: str) -> bool:
    """Return whether ``host`` is this machine talking to itself."""

    if not host:
        return False
    candidate = host.split("%", 1)[0]
    try:
        parsed = ip_address(candidate)
    except ValueError:
        return False
    if parsed.is_loopback:
        return True
    # ::ffff:127.0.0.1 is loopback wearing an IPv6 costume.
    mapped = getattr(parsed, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


class PasswordGate:
    """Decides whether a request may proceed, and throttles wrong guesses."""

    def __init__(
        self,
        credential: Credential,
        *,
        exempt_loopback: bool = True,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
    ) -> None:
        self._credential = credential
        self._exempt_loopback = exempt_loopback
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        # Keyed under a secret that never leaves this process, so the cache
        # holds no material worth stealing even in a memory dump.
        self._cache_key = secrets.token_bytes(32)
        self._verified: OrderedDict[bytes, float] = OrderedDict()

    def exempt(self, client_host: str) -> bool:
        return self._exempt_loopback and is_loopback(client_host)

    def check(self, header: str | None, client_host: str) -> Decision:
        """Classify one request's credentials."""

        if self.exempt(client_host):
            return Decision.EXEMPT
        if self._locked(client_host):
            return Decision.LOCKED_OUT
        password = _password_from_header(header)
        if password is None:
            # No credentials offered at all: that is a browser's first request,
            # not a guess, so it must not count toward the lockout.
            return Decision.CHALLENGE
        if self._remembered(password):
            return Decision.AUTHENTICATED
        if self._credential.verify(password):
            self._remember(password)
            self._reset(client_host)
            return Decision.AUTHENTICATED
        self._record_failure(client_host)
        self._sleep(FAILURE_DELAY_SECONDS)
        return Decision.REJECTED

    # -- throttling -------------------------------------------------------

    def _locked(self, client_host: str) -> bool:
        now = self._clock()
        with self._lock:
            until = self._locked_until.get(client_host)
            if until is None:
                return False
            if until > now:
                return True
            del self._locked_until[client_host]
            self._failures.pop(client_host, None)
            return False

    def _record_failure(self, client_host: str) -> None:
        now = self._clock()
        with self._lock:
            recent = [
                stamp
                for stamp in self._failures.get(client_host, [])
                if now - stamp < FAILURE_WINDOW_SECONDS
            ]
            recent.append(now)
            self._failures[client_host] = recent
            if len(recent) >= LOCKOUT_THRESHOLD:
                self._locked_until[client_host] = now + LOCKOUT_SECONDS
                self._failures[client_host] = []
                LOGGER.warning(
                    "Locking out %s for %.0fs after %d wrong passwords",
                    client_host,
                    LOCKOUT_SECONDS,
                    LOCKOUT_THRESHOLD,
                )

    def _reset(self, client_host: str) -> None:
        with self._lock:
            self._failures.pop(client_host, None)
            self._locked_until.pop(client_host, None)

    # -- verified-password memo -------------------------------------------

    def _token(self, password: str) -> bytes:
        return hmac.new(self._cache_key, password.encode("utf-8"), sha256).digest()

    def _remembered(self, password: str) -> bool:
        token = self._token(password)
        now = self._clock()
        with self._lock:
            expiry = self._verified.get(token)
            if expiry is None:
                return False
            if expiry <= now:
                del self._verified[token]
                return False
            self._verified.move_to_end(token)
            return True

    def _remember(self, password: str) -> None:
        token = self._token(password)
        with self._lock:
            self._verified[token] = self._clock() + VERIFIED_CACHE_SECONDS
            self._verified.move_to_end(token)
            while len(self._verified) > VERIFIED_CACHE_ENTRIES:
                self._verified.popitem(last=False)


def _password_from_header(header: str | None) -> str | None:
    """Extract the password from an HTTP Basic ``Authorization`` header.

    Any user name is accepted. Coworkers only get told a password, and making
    them guess a matching user name adds no security.
    """

    if not header:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded.strip():
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if ":" not in text:
        return None
    return text.split(":", 1)[1]

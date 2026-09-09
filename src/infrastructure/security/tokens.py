"""JWT issuance and verification."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import jwt

from src.application.ports.services import TokenPair
from src.config import Settings


class JwtTokenService:
    def __init__(self, settings: Settings) -> None:
        self._secret = settings.jwt_secret
        self._alg = settings.jwt_algorithm
        self._access_ttl = timedelta(minutes=settings.jwt_access_ttl_minutes)
        self._refresh_ttl = timedelta(days=settings.jwt_refresh_ttl_days)
        # Deliberately short. A reset link sits in an inbox indefinitely, so its
        # useful life should be measured in minutes, not the days a refresh
        # token gets.
        self._reset_ttl = timedelta(minutes=settings.password_reset_ttl_minutes)

    def issue(
        self, *, user_id: str, tenant_id: str, role: str, is_platform_admin: bool = False
    ) -> TokenPair:
        now = datetime.now(UTC)
        base = {
            "sub": user_id,
            "tenant_id": tenant_id,
            "role": role,
            "is_platform_admin": is_platform_admin,
            "iat": now,
        }
        access = jwt.encode(
            {**base, "type": "access", "exp": now + self._access_ttl},
            self._secret,
            algorithm=self._alg,
        )
        refresh = jwt.encode(
            {**base, "type": "refresh", "exp": now + self._refresh_ttl},
            self._secret,
            algorithm=self._alg,
        )
        return TokenPair(access_token=access, refresh_token=refresh)

    def decode(self, token: str) -> dict:
        return jwt.decode(token, self._secret, algorithms=[self._alg])

    def issue_password_reset(self, *, user_id: str, password_hash: str) -> str:
        """A short-lived token that authorises exactly one password change.

        Stateless — no table, no cleanup job — but still single-use, because the
        current password hash is fingerprinted into the token. Changing the
        password changes the hash, which invalidates every token issued against
        the old one. That also means a stolen link stops working the moment the
        real owner resets, and a token cannot be replayed to undo a reset.
        """
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": user_id,
                "type": "password_reset",
                "pw": _fingerprint(password_hash),
                "iat": now,
                "exp": now + self._reset_ttl,
            },
            self._secret,
            algorithm=self._alg,
        )

    def decode_password_reset(self, token: str) -> dict:
        """Decode and confirm this is a reset token.

        The type check matters: without it an ordinary access token would be
        accepted here, turning any valid session into a password-change
        authority.
        """
        claims = jwt.decode(token, self._secret, algorithms=[self._alg])
        if claims.get("type") != "password_reset":
            raise jwt.InvalidTokenError("not a password reset token")
        return claims

    @staticmethod
    def reset_matches_current(claims: dict, password_hash: str) -> bool:
        return bool(claims.get("pw")) and claims["pw"] == _fingerprint(password_hash)


def _fingerprint(password_hash: str) -> str:
    """A short digest of the stored hash. Never the hash itself — the token
    travels by email and lands in logs and browser history."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]

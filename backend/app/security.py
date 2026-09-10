"""Token generation.

There is no password hashing here any more, and no JWT signing. Authentication
is a database lookup against `access_tokens`: a client is issued an opaque
random token and it is checked on every request.

That is a deliberate trade. A signed JWT verifies without touching the
database, but it stays valid until it expires — you cannot take one back. A
token that is looked up costs one indexed primary-key read and can be revoked
instantly, which matters far more when the credential is handed to a client and
may need pulling at short notice.
"""

from __future__ import annotations

import secrets

# Identifies our tokens at a glance in a log or a support ticket, and follows
# the convention scanners look for when hunting leaked credentials in repos.
TOKEN_PREFIX = "pp_live_"

# 32 bytes of urlsafe base64 is ~43 characters, so a full token is ~51 —
# comfortably inside the 128-character column.
TOKEN_ENTROPY_BYTES = 32


def generate_token(prefix: str = TOKEN_PREFIX) -> str:
    """A new access token, from the OS cryptographic random source."""
    return f"{prefix}{secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)}"


def tokens_equal(left: str, right: str) -> bool:
    """Constant-time comparison, for anywhere a token is compared by hand."""
    return secrets.compare_digest(left or "", right or "")

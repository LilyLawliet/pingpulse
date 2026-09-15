"""Generate a VAPID keypair for browser push.

Self-signed and self-hosted. There is no account to open, nothing to pay for,
and no third party involved - which is why push is the one alerting channel
that works without the client arranging anything first.

Run it once, put both lines in the production .env, and never regenerate them
casually: every browser that has already subscribed did so against the old
public key, and they all go silent the moment it changes.

    python scripts/make_vapid_keys.py
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64(raw: bytes) -> str:
    """base64url, unpadded - what both the browser and pywebpush expect."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def main() -> None:
    key = ec.generate_private_key(ec.SECP256R1())

    # pywebpush wants the raw 32-byte scalar.
    private = _b64(key.private_numbers().private_value.to_bytes(32, "big"))
    # The browser wants the uncompressed public point, all 65 bytes of it.
    public = _b64(
        key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )

    print("# Paste these into the production .env, then redeploy.")
    print(f"VAPID_PUBLIC_KEY={public}")
    print(f"VAPID_PRIVATE_KEY={private}")


if __name__ == "__main__":
    main()

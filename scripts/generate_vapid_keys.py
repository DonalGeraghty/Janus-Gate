"""Generate URL-safe VAPID values suitable for deployment secrets."""

import base64
import secrets

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid


def _base64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def main():
    vapid = Vapid()
    vapid.generate_keys()
    private_value = vapid.private_key.private_numbers().private_value
    private_key = private_value.to_bytes(32, "big")
    public_key = vapid.public_key.public_bytes(
        Encoding.X962,
        PublicFormat.UncompressedPoint,
    )

    print(f"VAPID_PRIVATE_KEY={_base64url(private_key)}")
    print(f"VAPID_PUBLIC_KEY={_base64url(public_key)}")
    print(f"PUSH_CRON_SECRET={secrets.token_urlsafe(32)}")


if __name__ == "__main__":
    main()

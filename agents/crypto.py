"""Secret storage for agent connections.

API keys are encrypted at rest with a Fernet key derived from Django's
SECRET_KEY: the DB alone (backups, dumps, a stolen sqlite file) is not
enough to read them — you also need the server's secret. This is
symmetric encryption, not a vault: rotating SECRET_KEY invalidates
stored keys (they must be re-entered), which we accept for v1.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ""  # SECRET_KEY rotated: the key must be re-entered


def mask(plaintext: str) -> str:
    """A displayable hint that never reveals the key: sk-…f3ab."""
    if not plaintext:
        return ""
    return f"{plaintext[:3]}…{plaintext[-4:]}" if len(plaintext) > 10 else "•••"

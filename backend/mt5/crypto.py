import os
from cryptography.fernet import Fernet

def _fernet() -> Fernet:
    key = os.getenv("MT5_CRED_FERNET_KEY")
    if not key:
        raise RuntimeError("Missing MT5_CRED_FERNET_KEY")
    return Fernet(key.encode("utf-8"))

def encrypt_password(pw: str) -> str:
    return _fernet().encrypt(pw.encode("utf-8")).decode("utf-8")

def decrypt_password(token: str) -> str:
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")

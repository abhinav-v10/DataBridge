from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class SecretKeyError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().secret_key
    if not key:
        raise SecretKeyError(
            "DATABRIDGE_SECRET_KEY is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode())
    except ValueError as e:
        raise SecretKeyError(f"DATABRIDGE_SECRET_KEY is not a valid Fernet key: {e}") from e


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise SecretKeyError("Stored secret cannot be decrypted (wrong DATABRIDGE_SECRET_KEY?)") from e

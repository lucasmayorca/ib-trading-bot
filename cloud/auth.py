"""
Auth helpers — JWT tokens + bcrypt password hashing.
"""

import os
import datetime
import bcrypt
import jwt

_DEV_SECRET = "change-me-in-production"
JWT_SECRET = os.environ.get("JWT_SECRET", _DEV_SECRET)
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24

# Con el secreto por defecto cualquiera forja un JWT valido con el user_id y el
# email que quiera — incluido ADMIN_EMAIL, que ademas abre GET /api/feedback.
# En produccion eso tiene que ser un arranque fallido, no un warning que se
# pierde entre los logs. En local (sin DATABASE_URL) se permite para desarrollo.
if JWT_SECRET == _DEV_SECRET:
    if os.environ.get("DATABASE_URL") or os.environ.get("RAILWAY_ENVIRONMENT"):
        raise RuntimeError(
            "JWT_SECRET no esta configurado: con el valor por defecto cualquiera "
            "puede firmar un token de administrador. Definir JWT_SECRET en el entorno."
        )
    print("[auth] AVISO: usando JWT_SECRET de desarrollo (no apto para produccion)")


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def check_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_jwt(user_id: int, email: str) -> str:
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None

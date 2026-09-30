import datetime
import os
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
import bcrypt
from sqlalchemy.orm import Session

from . import models
from .db import get_db

# In a real deployment set ANAMNESIS_SECRET as an env var. A random fallback
# is generated per-process so tokens simply stop working on restart if unset,
# which is safe-by-default for a hackathon demo.
SECRET_KEY = os.environ.get("ANAMNESIS_SECRET", os.urandom(32).hex())
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 12

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

ROLE_RANK = {"guest": 0, "intern": 1, "member": 2, "manager": 3, "admin": 4, "owner": 5}


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("utf-8"))


def create_access_token(user_id: int) -> str:
    expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> models.User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if user is None or not user.active:
        raise credentials_exception
    return user


def require_role(min_role: str):
    """FastAPI dependency factory: require the current user to have at least
    `min_role` seniority (guest < member < manager < admin < owner)."""

    def dependency(user: models.User = Depends(get_current_user)) -> models.User:
        if ROLE_RANK.get(user.role, 0) < ROLE_RANK.get(min_role, 0):
            raise HTTPException(status_code=403, detail=f"Requires role '{min_role}' or higher")
        return user

    return dependency


def rank(user: models.User) -> int:
    return ROLE_RANK.get(user.role, 0)

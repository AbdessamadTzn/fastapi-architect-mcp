from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import User


def get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


SessionDep = Annotated[Session, Depends(get_db)]


def get_current_user(db=Depends(get_db)) -> User:
    user = db.get(User, 1)
    if user is None:
        raise HTTPException(status_code=401)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    return user

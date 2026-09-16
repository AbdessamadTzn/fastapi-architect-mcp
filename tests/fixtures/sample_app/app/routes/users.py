from fastapi import APIRouter, Depends

from app import crud
from app.deps import SessionDep, get_current_user, get_db, require_admin
from app.schemas import UserCreate, UserPublic

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/", response_model=list[UserPublic])
def list_users(db: SessionDep):
    return crud.get_users(db)


@router.post("/", response_model=UserPublic)
def create_user(user: UserCreate, db=Depends(get_db)):
    return crud.create_user(db, user)


@router.delete("/{user_id}", dependencies=[Depends(require_admin)])
async def delete_user(user_id: int, *, db: SessionDep):
    crud.delete_user(db, user_id)

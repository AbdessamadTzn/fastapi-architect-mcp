from fastapi import APIRouter

from app.deps import CurrentUser, SessionDep
from app.schemas import ItemCreate, ItemPublic

router = APIRouter(prefix="/items")


@router.get("/", response_model=list[ItemPublic])
def list_items(db: SessionDep):
    return db.execute("SELECT * FROM items").all()


@router.post("/")
def create_item(item: ItemCreate, user: CurrentUser, db: SessionDep):
    db.execute("INSERT INTO items (title, owner_id) VALUES (:t, :o)", {"t": item.title, "o": user.id})

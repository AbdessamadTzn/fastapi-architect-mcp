from app.database import User
from app.schemas import UserCreate


def get_users(db):
    return db.query(User).all()


def create_user(db, user: UserCreate):
    db_user = User(email=user.email, hashed_password=user.password + "!")
    db.add(db_user)
    return db_user


def delete_user(db, user_id: int):
    db.execute("DELETE FROM users WHERE id = :id", {"id": user_id})

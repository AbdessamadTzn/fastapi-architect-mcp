from pydantic import BaseModel, field_validator


class UserBase(BaseModel):
    email: str


class UserCreate(UserBase):
    password: str

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("too short")
        return v


class UserPublic(UserBase):
    id: int
    items: list["ItemPublic"] = []


class ItemCreate(BaseModel):
    title: str


class ItemPublic(ItemCreate):
    id: int
    owner_id: int


class Unused(BaseModel):
    value: int = 0

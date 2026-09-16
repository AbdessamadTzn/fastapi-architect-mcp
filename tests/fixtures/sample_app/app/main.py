from fastapi import FastAPI

from app.routes import items, users

app = FastAPI()
app.include_router(users.router, prefix="/api")
app.include_router(items.router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok"}

"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import DATABASE_URL, FRONTEND_URL
from db import close_pool, init_pool
from routes import auth_routes, questions, exams, ai, images, feedback


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL must be configured before starting the API")
    init_pool()
    try:
        yield
    finally:
        close_pool()


app = FastAPI(title="Study GT API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:5173", "http://127.0.0.1:5173", "*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth_routes.router)
app.include_router(questions.router)
app.include_router(exams.router)
app.include_router(ai.router)
app.include_router(images.router)
app.include_router(feedback.router)


@app.get("/health")
def health_check():
    return {"status": "ok"}

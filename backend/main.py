from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.database import Base, engine
from typing import Optional 
from backend.routers import donors, blood_requests, hospitals, users
from backend.firebase import initialize_firebase
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from backend.core.rate_limiter import limiter
from fastapi import WebSocket, WebSocketDisconnect
from backend.core.websocket_manager import manager
import logging
from backend.core.scheduler import start_scheduler
from backend.config import get_settings  
import os
from backend.models import User, Hospital
from firebase_admin import auth as firebase_auth
from backend.database import SessionLocal

settings = get_settings() 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

initialize_firebase()

app = FastAPI(
    title="Drop4Life",
    description="API for connecting blood donors with those in need",
    version="1.0.0",
    # redirect_slashes=False,
)

@app.on_event("startup")
async def startup_websocket_manager():
    await manager.start()


@app.on_event("shutdown")
async def shutdown_websocket_manager():
    await manager.stop()

app.add_middleware(
    CORSMiddleware,
    # In dev: reads from .env → "http://localhost:3000,http://localhost:5173"
    # In prod: reads from Render env vars → "https://yourapp.com"
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,    # ← Now safe because origins are explicit
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Rate limiting
BENCHMARK_MODE = os.getenv("BENCHMARK_MODE", "false").lower() == "true"

if not BENCHMARK_MODE:
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(
        RateLimitExceeded,
        _rate_limit_exceeded_handler
    )


start_scheduler()

app.include_router(donors.router)
app.include_router(blood_requests.router)
app.include_router(hospitals.router) 
app.include_router(users.router) 

@app.get("/")
def read_root():
    return {
        "status": "running",
        "message": "Welcome to Drop4Life!",
        "version": "1.0.0",
    }






@app.websocket("/ws/{room}")
async def websocket_endpoint(websocket: WebSocket, room: str):
    token = websocket.query_params.get("token")

    if not token:
        await websocket.close(code=1008)
        return

    try:
        decoded_token = firebase_auth.verify_id_token(token)
    except Exception:
        await websocket.close(code=1008)
        return

    firebase_uid = decoded_token.get("uid")

    # Admin room
    if room == "admin":
        db = SessionLocal()
        try:
            user = db.query(User).filter(
                User.firebase_uid == firebase_uid
            ).first()

            if not user or not user.is_active or user.role != "admin":
                await websocket.close(code=1008)
                return
        finally:
            db.close()

    # Hospital room
    elif room.startswith("hospital_"):
        try:
            hospital_id = int(room.split("_", 1)[1])
        except (ValueError, IndexError):
            await websocket.close(code=1008)
            return

        db = SessionLocal()
        try:
            hospital = db.query(Hospital).filter(
                Hospital.id == hospital_id,
                Hospital.firebase_uid == firebase_uid,
                Hospital.is_active == True,
            ).first()

            if not hospital:
                await websocket.close(code=1008)
                return
        finally:
            db.close()

    # Unknown room
    else:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, room)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, room)
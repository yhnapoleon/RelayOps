"""CORS config helper."""

from typing import List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def add_cors(app: FastAPI, allow_origins: Optional[List[str]] = None) -> None:
    """Add CORS middleware. Default allows Vite dev server (localhost:5173) and React dev (localhost:3000)."""
    if allow_origins is None:
        allow_origins = ["http://localhost:5173", "http://localhost:3000"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

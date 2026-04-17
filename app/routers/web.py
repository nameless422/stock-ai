from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings


router = APIRouter()


def _read_template(name: str) -> str:
    path = Path(settings.base_dir) / "templates" / name
    return path.read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(content=_read_template("index.html"), status_code=200)


@router.get("/screener", response_class=HTMLResponse)
def screener_page() -> HTMLResponse:
    return HTMLResponse(content=_read_template("screener.html"), status_code=200)


@router.get("/strategies", response_class=HTMLResponse)
def strategies_page() -> HTMLResponse:
    return HTMLResponse(content=_read_template("strategies.html"), status_code=200)

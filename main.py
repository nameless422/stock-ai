"""
Web application entrypoint.
"""

from app import create_app
from app.config import settings


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, workers=settings.web_concurrency)

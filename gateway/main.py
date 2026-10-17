import logging

import uvicorn

from .app import create_app
from .config import Settings

settings = Settings()
logging.basicConfig(level=settings.LOG_LEVEL)
app = create_app(settings)

if __name__ == "__main__":
    uvicorn.run("gateway.main:app", host="0.0.0.0", port=8000)

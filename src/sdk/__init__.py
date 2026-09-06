"""Portable RelayOps server entry point."""
import uvicorn
from core.config import get_config
from core.logging import setup_logging


def serve(host='127.0.0.1', port=8000):
    setup_logging()
    config = get_config()
    uvicorn.run('api.main:app', host=host, port=port, reload=config.server_reload,
                workers=1, access_log=config.server_access_log)

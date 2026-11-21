import logging
import os

import uvicorn

from .config import get_settings


def main() -> None:
    logging.basicConfig(level=os.environ.get("DD_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    s = get_settings()
    os.makedirs(s.config_dir, exist_ok=True)
    uvicorn.run("dailydig.api:app", host="0.0.0.0", port=s.port, log_level="info")


if __name__ == "__main__":
    main()

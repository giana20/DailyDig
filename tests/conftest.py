import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dailydig.config import Settings, set_settings  # noqa: E402
from dailydig.context import Context  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    s = Settings(config_dir=str(tmp_path / "config"), music_dir=str(tmp_path / "music"))
    s.lb_user = "tester"
    s.enable_scheduler = False
    s.discovery_subdir, s.expire_days = "_discovery", 30     # most tests exercise the staging mode
    os.makedirs(s.music_dir, exist_ok=True)
    set_settings(s)
    return s


@pytest.fixture
async def ctx(settings):
    c = Context.create(settings)
    yield c
    await c.close()

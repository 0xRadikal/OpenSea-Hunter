"""Runtime configuration from environment (values live in an env file OUTSIDE the repo, chmod 600)."""
import os
from dataclasses import dataclass, field


def _ids(v):
    return {int(x) for x in (v or "").replace(" ", "").split(",") if x}


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: os.environ.get("BOT_TOKEN", ""))
    admin_ids: set = field(default_factory=lambda: _ids(os.environ.get("ADMIN_IDS")))
    data_dir: str = field(default_factory=lambda: os.environ.get("DATA_DIR", "/var/lib/opensea-hunter"))
    master_key_file: str = field(default_factory=lambda: os.environ.get("MASTER_KEY_FILE", "/root/osh-master.key"))
    price_stars: int = field(default_factory=lambda: int(os.environ.get("PRICE_STARS", "500")))
    sub_days: int = field(default_factory=lambda: int(os.environ.get("SUB_DAYS", "30")))
    keypage_url: str = field(default_factory=lambda: os.environ.get("KEYPAGE_URL", ""))   # https URL of keypage/index.html
    telegram_base_url: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BASE_URL", ""))  # tests only
    max_active_tasks: int = field(default_factory=lambda: int(os.environ.get("MAX_ACTIVE_TASKS", "3")))
    spawn_before_s: int = field(default_factory=lambda: int(os.environ.get("SPAWN_BEFORE_S", "7200")))
    opensea_base: str = field(default_factory=lambda: os.environ.get("OPENSEA_BASE", "https://api.opensea.io"))

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "service.db")

    @property
    def log_dir(self):
        return os.path.join(self.data_dir, "task-logs")

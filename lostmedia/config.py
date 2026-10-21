import os
import logging
from dataclasses import dataclass
from pathlib import Path

# HTTPX INFO logs include query strings (the official YouTube key is a parameter).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Local credentials are ignored by Git. Never include them in diagnostics.
_env_file=Path(__file__).resolve().parents[1]/".env"
if _env_file.exists():
    for _line in _env_file.read_text("utf-8").splitlines():
        if _line.startswith("YOUTUBE_API_KEY="):
            os.environ.setdefault("YOUTUBE_API_KEY",_line.split("=",1)[1].strip())


@dataclass
class Settings:
    root: Path = Path(os.environ.get("LOSTMEDIA_DATA", "data")).resolve()
    interval: float = 2.0
    reference_interval: float = 0.25  # references are short: sample them densely
    batch_size: int = 128
    downloads: int = 6
    processes: int = 0  # CPU decode workers; 0 = min(12, cpus-2)
    max_slots_per_source: int = 12
    adaptive_pacing: bool = True
    resume_paused: bool = False  # keep running until paused platforms resume
    backend: str = "dinov2"
    device: str = "auto"
    crops: object = True  # True: 7 views per frame; "fine": + 8 small corner windows (PiP)
    max_duration: int = 7200
    max_download_mb: int = 2048
    min_free_gb: float = 5
    lease_seconds: int = 300
    disk_budget_gb: float = 500
    delete_downloads: bool = True
    keep_all_thumbnails: bool = False
    feature_precision: str = "float16"
    campaign_through: str = "2007-08-31"

    def __post_init__(self):
        self.root = Path(self.root).resolve()
        if self.interval <= 0 or self.batch_size < 1 or self.downloads < 1:
            raise ValueError("interval, batch_size and downloads must be positive")
        if self.backend not in {"phash", "dinov2", "clip"}:
            raise ValueError("Unknown backend")
        if self.feature_precision not in {"float16","float32"}:
            raise ValueError("feature_precision must be float16 or float32")
        if self.disk_budget_gb<=0 or self.min_free_gb<0:
            raise ValueError("Invalid disk budget")
        for directory in ("media", "features", "thumbs", "indexes", "uploads"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)


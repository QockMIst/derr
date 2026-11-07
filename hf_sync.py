import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional


class HuggingFacePersistence:
    def __init__(self, db_path: Path, interval_seconds: int = 600):
        self.db_path = Path(db_path)
        self.interval_seconds = interval_seconds
        self.hf_token = os.environ.get("HF_TOKEN", "").strip()
        self.dataset_repo = os.environ.get("HF_DATASET_REPO", "").strip()
        self.enabled = bool(self.hf_token and self.dataset_repo)

    def restore_if_available(self) -> bool:
        if not self.enabled:
            return False
        url = f"https://huggingface.co/datasets/{self.dataset_repo}/resolve/main/void_data.sqlite3"
        try:
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {self.hf_token}", "User-Agent": "Carbon/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if len(data) > 4096 and data.startswith(b"SQLite format 3"):
                    self.db_path.write_bytes(data)
                    return True
        except Exception:
            pass
        return False

    def backup_once(self) -> bool:
        if not self.enabled or not self.db_path.exists():
            return False
        try:
            from huggingface_hub import HfApi

            api = HfApi(token=self.hf_token)
            api.upload_file(
                path_or_fileobj=str(self.db_path),
                path_in_repo="void_data.sqlite3",
                repo_id=self.dataset_repo,
                repo_type="dataset",
                commit_message=f"Auto backup {int(time.time())}",
            )
            return True
        except Exception:
            return False

    def start_periodic_backup(self, stop_event: threading.Event) -> Optional[threading.Thread]:
        if not self.enabled:
            return None

        def _loop():
            while not stop_event.wait(self.interval_seconds):
                self.backup_once()

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        return t

---
title: Void Market Tools
emoji: 🔮
colorFrom: purple
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Void Market Tools — Free 24/7 Hugging Face Space Setup (No Credit Card Required)

## 1. Create a Free Hugging Face Space
1. Sign up at https://huggingface.co (email only, no card required).
2. Go to **Spaces** → **Create new Space**:
   - **Space name**: `void-market-tools`
   - **Select the Space SDK**: **Docker** → **Blank**
   - **Space Hardware**: `CPU basic · 2 vCPU · 16 GB · FREE`
   - **Visibility**: Private (or Public if using cron-job.org pinger without headers)
3. Upload the contents of this `void_tool/` folder to the Space repository.

## 2. Enable Persistent SQLite Proof Backup (So Restarts Never Wipe Data)
1. On Hugging Face, click your profile → **New Dataset** → name it `void-db-backup` and make it **Private**.
2. Go to **Settings** → **Access Tokens** → create a token with **Write** permission.
3. Open your Space → **Settings** → **Variables and secrets**:
   - Add Secret `HF_TOKEN` = your write token
   - Add Variable `HF_DATASET_REPO` = `your-username/void-db-backup`
   The app will automatically restore `void_data.sqlite3` on boot and back it up every 10 minutes.

## 3. Keep It Awake 24/7
1. Create a free account at https://cron-job.org (no card required).
2. Add a cron job pointing to `https://<your-username>-void-market-tools.hf.space/api/status` every **10 minutes**.

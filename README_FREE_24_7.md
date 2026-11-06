# 100% Free 24/7 Cloud Scanner Setup (GitHub Actions + Render — No Credit Card)

Hugging Face now locks Docker Spaces behind a paid PRO plan. Instead, **GitHub Actions** gives **unlimited free minutes** on public repositories with **no credit card required**, and **Render.com** hosts the live web UI for free without a credit card when you select the Free Python Web Service tier manually.

---

## Option A: Run the 24/7 Scanner on GitHub Actions (100% Free, No Card) + Use Local Dashboard

### Step 1: Create a Free GitHub Repository
1. Sign in (or sign up with email only) at https://github.com.
2. Click the **`+`** icon (top right) → **New repository**:
   - **Repository name**: `void-scanner`
   - **Visibility**: **Public** *(Public repositories get unlimited free GitHub Actions minutes; Private gets 2,000 mins/month).*
   - Check **Add a README file**.
   - Click **Create repository**.

### Step 2: Upload the Files via the GitHub Web Page
1. Inside your new `void-scanner` repo, click **Add file** → **Upload files**.
2. Open `c:\Users\admin\Documents\folder for cool\void_tool\` on your PC, select everything inside it (**including** the `.github` folder, `cloud_scan_batch.py`, `app.py`, `data/`, `tracker/`, `web/`, etc.), and drag them into the GitHub browser window.
3. Click **Commit changes** (green button at the bottom).

### Step 3: Turn On Write Permissions for the Scanner Bot
1. In your GitHub repo, click **Settings** (top tab) → **Actions** (left sidebar) → **General**.
2. Scroll down to **Workflow permissions**:
   - Select **Read and write permissions**.
   - Click **Save**.

### Step 4: How to Trigger & Check the 24/7 Scanner
1. Click the **Actions** tab at the top of your GitHub repository.
2. Click **Void 24/7 Cloud Scanner** on the left.
3. Click **Run workflow** (right side) → **Run workflow** (green button) to start the first scan immediately (after that, it runs automatically every 15 minutes 24/7).
4. **How to check it**:
   - Click on the running job (`scan`) → click **Run Void Scanner Batch** to watch the live log output:
     - `Items indexed: 2,586 (2,583 Rolimons)`
     - `Tracked traders: X (Y scanned)`
     - `Tracked UAIDs: Z`
     - `Verified Proofs: N`
   - Every time a batch finishes, the workflow automatically saves the updated `void_data.sqlite3` back into your GitHub repo.

### Step 5: Link Your PC Dashboard to Your 24/7 Cloud Database
1. Open `c:\Users\admin\Documents\folder for cool\void_tool\config.toml` in Notepad.
2. Set `github_db_raw_url` to your repository's raw SQLite URL:
   ```toml
   github_db_raw_url = "https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/void-scanner/main/void_data.sqlite3"
   ```
3. Now, whenever you open `run_void.bat` on your PC, it automatically downloads all the proofs and UAIDs collected 24/7 by GitHub!

---

## Option B: Also Host the Web UI Online for Free (Render.com — No Card)
If you also want to open the web dashboard from your phone or browser without running `run_void.bat`:
1. Go to https://render.com and sign up using **Continue with GitHub** (no credit card needed for manual Free Web Services).
2. Click **New +** → **Web Service** → select your `void-scanner` GitHub repository.
3. Fill in:
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Select **Free ($0/month)**
4. Click **Deploy Web Service**. Render gives you an `https://void-scanner.onrender.com` URL that you can open anytime (or keep awake with `cron-job.org`).

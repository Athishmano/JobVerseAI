# JobBot 🤖

An AI-powered job scraping and evaluation pipeline. 

JobBot automates the tedious process of searching for jobs across multiple portals (LinkedIn, Naukri, Indeed, Wellfound). It scrapes listings, applies your strict offline heuristic filters (saving you from blacklisted companies and mismatched roles), and then uses Google's Gemini AI to rigorously assess the surviving jobs against your custom `UserProfile`. 

The results are presented in a sleek, dark-mode, glassmorphic local dashboard.

---

## 🌟 Features
- **Multi-Portal Scraping:** Seamlessly pulls jobs from top portals using Playwright.
- **Persistent Sessions:** Saves your browser state (cookies/auth) so you only log in once.
- **Offline Filtering:** Fast heuristic rejection of junk jobs (blacklisted keywords, missing salaries, etc.) before they ever reach the AI, saving API costs.
- **AI Scoring:** Uses `gemini-2.5-flash` to generate a 0-100 match score and concrete reasoning on why you should (or shouldn't) apply.
- **Beautiful Dashboard:** A premium, responsive local UI to review your matched jobs, complete with filtering and AI assessments.

---

## 🛠️ Installation

### Prerequisites
- Python 3.10+
- A Google Gemini API Key

### Local Setup
1. **Clone the repository:**
   ```bash
   git clone <your-repo-url>
   cd job-bot
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

3. **Configuration:**
   - Copy `.env.example` to `.env` and add your `GEMINI_API_KEY`.
   - Edit `config/profile.json` to reflect your actual experience, skills, and job preferences.
   - (Optional) Add `config/resume.pdf` if you plan to extend the AI to read your raw resume.

---

## 🚀 Usage

JobBot is designed to be triggered manually via the CLI when you want to hunt for jobs. There are no automated background schedulers.

### Running the Pipeline
```bash
# Scrape the default number of jobs from all active portals (25 per portal)
python main.py

# Scrape 100 jobs specifically from naukri
python main.py --sites naukri --limit 100

# Scrape 50 jobs specifically from linkedin and wellfound
python main.py --sites linkedin,wellfound --limit 50

# Scrape 200 jobs from all portals
python main.py --sites all --limit 200

# Run with verbose debug logging
python main.py --debug
```

Once the pipeline successfully finishes, it will automatically spawn the FastAPI backend and open your browser to the local dashboard (`http://localhost:3000`).

### Viewing Past Runs
If you just want to view the dashboard without scraping new jobs:
```bash
python -m uvicorn job_bot.api.main:app --port 3000
```
Then navigate to `http://localhost:3000` in your browser.

---

## 📂 Architecture

- **`config/`**: User configuration (`profile.json`) and secrets (`.env`).
- **`data/`**: Persistent Playwright state (cookies, local storage) and internal caches.
- **`results/`**: Output data.
  - `Passed/`: JSON files containing jobs that passed filtering and met your minimum AI score.
  - `Failed/`: JSON files containing jobs that were rejected.
- **`job_bot/`**: Core logic.
  - `scrapers/`: Individual Playwright scraper implementations.
  - `core/`: Browser management, Storage I/O, AI Engine, Filters, Logger.
  - `schemas/`: Pydantic models enforcing strict data validation.
  - `services/`: The orchestration pipeline.
  - `api/`: FastAPI backend serving results to the frontend.
- **`frontend/`**: Vanilla JS/HTML/CSS dashboard.

---

## ⚠️ Hard Rules & Design Decisions
1. **No Databases:** Flat JSON files only. Zero setup overhead.
2. **Fail-Fast Configuration:** If your Gemini API key is missing, the app exits immediately.
3. **No Schedulers:** You control when it runs via the CLI.
4. **Data Privacy:** Your profile and secrets stay local. They are only sent to the Gemini API for scoring.

Happy Job Hunting! 🎯

# Comment Scraper

Standalone local application for scraping YouTube videos and comments.
The application runs directly on the host system.

## Features

- Scrape YouTube channels by handle, channel ID, or URL
- Scrape individual videos by YouTube URL
- Capture comment threads and replies
- Clean video descriptions with Ollama using `llama3.2`
- Start, pause, resume, and cancel scraping jobs
- Persist each video incrementally in SQLite
- Show live job status and detailed logs in the frontend
- Browse stored videos and comments

## Local requirements

- Python 3.14.7 (or another supported Python 3 version)
- Ollama installed and running locally
- YouTube Data API key

Ollama must be available at `http://localhost:11434`. The configured model is
`llama3.2`.

## Setup on Windows

Run these commands from the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.ensure_model
```

The local data directories and the SQLite database are created at:

```text
data/sql/comment_scraper.sqlite3
data/logs/
```

The database is a regular local SQLite file and can be opened with DB Browser
for SQLite. The application uses a fixed project-relative data path.

## Start the application

Start the Streamlit frontend:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app/frontend.py
```

Open [http://localhost:8503](http://localhost:8503).

The frontend starts the local crawler automatically on 127.0.0.1:8000 and
checks the local Ollama service. If a local Ollama executable is installed but
the service is not running, the frontend attempts to start it automatically.
Service logs are written to data/logs/.

For one-click startup, run:

```powershell
python start.py
```

The launcher uses `.venv` automatically when it exists, prepares the local
services and `llama3.2`, starts Streamlit, and opens the frontend in the web
browser.

The YouTube API key is entered only in the frontend and kept in memory for the
current job. It is never written to files, SQLite, or logs.

## Local development

The crawler API is available at `http://127.0.0.1:8000`. The Streamlit frontend
uses this address by default and can be started with the Python executable from
`.venv` as shown above.

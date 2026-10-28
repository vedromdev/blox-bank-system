# Blox RP Bank System (Discord Bot + Web GUI + Database)

All-in-one system ready for Railway deployment.

## Features
- **/submit {photo}**: Staff command in Discord uploading in-game bank screenshot.
- **Supervisor Review**: Interactive Accept & Deny buttons with modal popup for entering verified minutes.
- **Web GUI Panel**: Dark-mode dashboard showing Hired/Fired/Retired staff, weekly pay, lifetime earnings, and quota status.
- **Built-in SQLite DB**: Stores staff records and shift logs.

## Setup Instructions

### 1. Set Environment Variables
In Railway (or in your local `.env`), set:
- `BOT_TOKEN`: Your Discord Bot Token
- `VERIFICATION_CHANNEL_ID`: Channel ID where supervisors review shifts
- `PORT`: (Handled automatically by Railway, default `8080`)

### 2. Run Locally (Testing)
```bash
pip install -r requirements.txt
export BOT_TOKEN="your_token"
export VERIFICATION_CHANNEL_ID="1234567890"
python main.py
```
Open `http://localhost:8080` in your browser to view the GUI Panel.

### 3. Deploy to Railway
1. Push this directory to a GitHub repository.
2. Link the repository on [Railway](https://railway.app).
3. Under **Variables**, add `BOT_TOKEN` and `VERIFICATION_CHANNEL_ID`.
4. Under **Settings** -> **Networking**, click **Generate Domain** to get your public GUI panel link.

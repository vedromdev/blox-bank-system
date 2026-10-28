import os
import sqlite3
import asyncio
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# ----------------- CONFIGURATION -----------------
# Load from environment variables (or .env if present)
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip().strip('"').strip("'")
raw_channel = os.environ.get("VERIFICATION_CHANNEL_ID", "0").strip().strip('"').strip("'")
try:
    VERIFICATION_CHANNEL_ID = int(raw_channel)
except ValueError:
    VERIFICATION_CHANNEL_ID = 0

DB_FILE = "bank_data.db"

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    print("\n" + "="*50)
    print("❌ ERROR: BOT_TOKEN is not set or invalid!")
    print("👉 Set the BOT_TOKEN variable in the 'Startup' tab of your Bot-Hosting panel,")
    print("   or upload a .env file with BOT_TOKEN=your_token.")
    print("="*50 + "\n")
# -------------------------------------------------

# ----------------- DATABASE SETUP -----------------
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            username TEXT PRIMARY KEY,
            discord_id TEXT,
            rank TEXT DEFAULT 'Teller',
            status TEXT DEFAULT 'Hired',
            quota REAL DEFAULT 120,
            current_mins REAL DEFAULT 0,
            current_earnings REAL DEFAULT 0,
            lifetime_mins REAL DEFAULT 0,
            lifetime_made REAL DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT PRIMARY KEY,
            timestamp TEXT,
            username TEXT,
            discord_id TEXT,
            minutes REAL,
            proof_url TEXT,
            status TEXT,
            reviewer TEXT,
            notes TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("INSERT OR IGNORE INTO settings VALUES ('base_pay', '1000')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('bonus_rate', '500')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('default_quota', '120')")
    conn.commit()
    conn.close()

init_db()

def get_setting(key, default):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

# ----------------- DISCORD BOT -----------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

class AcceptShiftModal(discord.ui.Modal, title="Verify & Credit Shift"):
    def __init__(self, submitter: discord.Member, proof_url: str, message: discord.Message):
        super().__init__()
        self.submitter = submitter
        self.proof_url = proof_url
        self.message = message

        self.username_input = discord.ui.TextInput(
            label="Roblox Username",
            default=submitter.display_name,
            placeholder="Username seen in screenshot",
            required=True
        )
        self.minutes_input = discord.ui.TextInput(
            label="Minutes Credited (from screenshot)",
            placeholder="e.g. 60",
            required=True
        )
        self.notes_input = discord.ui.TextInput(
            label="Shift Notes",
            default="Regular Shift",
            required=False
        )
        self.add_item(self.username_input)
        self.add_item(self.minutes_input)
        self.add_item(self.notes_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            minutes = float(self.minutes_input.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Minutes must be a valid number!", ephemeral=True)
            return

        username = self.username_input.value.strip()
        notes = self.notes_input.value.strip()
        sub_id = f"SUB-{int(datetime.now().timestamp())}"

        base_pay = float(get_setting("base_pay", 1000))
        bonus_rate = float(get_setting("bonus_rate", 500))
        default_quota = float(get_setting("default_quota", 120))

        # Update SQLite DB
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (sub_id, datetime.now().strftime("%Y-%m-%d %H:%M"), username, str(self.submitter.id),
              minutes, self.proof_url, "ACCEPTED", interaction.user.display_name, notes))

        c.execute("SELECT * FROM staff WHERE LOWER(username) = LOWER(?)", (username,))
        row = c.fetchone()

        if row:
            cur_mins = row["current_mins"] + minutes
            life_mins = row["lifetime_mins"] + minutes
            quota = row["quota"]

            bonus = 0
            if cur_mins > quota and quota > 0:
                bonus = int((cur_mins - quota) // 30) * bonus_rate
            cur_earnings = (base_pay + bonus) if cur_mins >= quota else 0
            life_made = row["lifetime_made"] + (minutes * 10)

            c.execute("""
                UPDATE staff SET current_mins=?, current_earnings=?, lifetime_mins=?, lifetime_made=?
                WHERE LOWER(username)=LOWER(?)
            """, (cur_mins, cur_earnings, life_mins, life_made, username))
        else:
            c.execute("""
                INSERT INTO staff (username, discord_id, rank, status, quota, current_mins, current_earnings, lifetime_mins, lifetime_made)
                VALUES (?, ?, 'Teller', 'Hired', ?, ?, 0, ?, ?)
            """, (username, str(self.submitter.id), default_quota, minutes, minutes, minutes * 10))

        conn.commit()
        conn.close()

        # Disable buttons
        disabled_view = discord.ui.View()
        for item in self.message.components[0].children:
            btn = discord.ui.Button(label=item.label, style=item.style, disabled=True, emoji=item.emoji)
            disabled_view.add_item(btn)
        await self.message.edit(view=disabled_view)

        await interaction.followup.send(
            f"✅ **Approved** by {interaction.user.mention}!\n"
            f"👤 **Staff:** {self.submitter.mention} (`{username}`)\n"
            f"⏱️ **Credited:** `{minutes} mins` | **Notes:** `{notes}`"
        )

class VerificationButtons(discord.ui.View):
    def __init__(self, submitter: discord.Member, proof_url: str):
        super().__init__(timeout=None)
        self.submitter = submitter
        self.proof_url = proof_url

    @discord.ui.button(label="Accept & Credit", style=discord.ButtonStyle.green, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AcceptShiftModal(self.submitter, self.proof_url, interaction.message))

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(f"❌ Submission from {self.submitter.mention} was **DENIED** by {interaction.user.mention}.")

@bot.tree.command(name="submit", description="Submit your Blox RP Bank progress screenshot")
@app_commands.describe(photo="Screenshot of your bank shift progress")
async def submit(interaction: discord.Interaction, photo: discord.Attachment):
    if not photo.content_type or not photo.content_type.startswith("image/"):
        await interaction.response.send_message("❌ Please upload a valid image screenshot.", ephemeral=True)
        return

    channel = bot.get_channel(VERIFICATION_CHANNEL_ID)
    if not channel:
        await interaction.response.send_message(
            f"❌ Verification channel `{VERIFICATION_CHANNEL_ID}` not found. Check VERIFICATION_CHANNEL_ID.",
            ephemeral=True
        )
        return

    embed = discord.Embed(title="🏦 New Shift Progress Submission", color=0xf1c40f)
    embed.add_field(name="Submitted By", value=interaction.user.mention, inline=True)
    embed.add_field(name="Account Name", value=f"`{interaction.user.name}`", inline=True)
    embed.set_image(url=photo.url)
    embed.set_footer(text="Click 'Accept & Credit' to verify shift minutes")

    view = VerificationButtons(submitter=interaction.user, proof_url=photo.url)
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Your screenshot has been submitted to supervisors!", ephemeral=True)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (Slash commands synced)")

# ----------------- MODERN LUXURY WEB GUI PANEL -----------------
HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Blox RP Bank — Executive Management Portal</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-dark: #070b14;
      --card-bg: rgba(15, 23, 42, 0.75);
      --card-border: rgba(51, 65, 85, 0.55);
      --accent-gold: #f59e0b;
      --accent-emerald: #10b981;
      --accent-blue: #3b82f6;
      --text-muted: #94a3b8;
    }
    * { box-sizing: border-box; }
    body {
      background-color: var(--bg-dark);
      background-image: 
        radial-gradient(at 0% 0%, rgba(59, 130, 246, 0.12) 0px, transparent 50%),
        radial-gradient(at 100% 100%, rgba(245, 158, 11, 0.08) 0px, transparent 50%),
        radial-gradient(at 50% 50%, rgba(16, 185, 129, 0.05) 0px, transparent 50%);
      color: #f8fafc;
      font-family: 'Plus Jakarta Sans', sans-serif;
      min-height: 100vh;
      overflow-x: hidden;
    }
    .navbar-glass {
      background: rgba(15, 23, 42, 0.85);
      backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--card-border);
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .glass-card {
      background: var(--card-bg);
      backdrop-filter: blur(14px);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4);
      transition: all 0.25s ease;
    }
    .glass-card:hover {
      border-color: rgba(99, 102, 241, 0.45);
      transform: translateY(-2px);
    }
    .stat-badge {
      width: 48px;
      height: 48px;
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.25rem;
    }
    .nav-pills .nav-link {
      color: var(--text-muted);
      border-radius: 10px;
      padding: 0.6rem 1.2rem;
      font-weight: 600;
      font-size: 0.95rem;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .nav-pills .nav-link:hover {
      color: #fff;
      background: rgba(255, 255, 255, 0.05);
    }
    .nav-pills .nav-link.active {
      color: #fff;
      background: linear-gradient(135deg, #2563eb, #1d4ed8);
      box-shadow: 0 4px 15px rgba(37, 99, 235, 0.35);
    }
    .badge-status {
      font-weight: 600;
      padding: 0.35rem 0.75rem;
      border-radius: 20px;
      font-size: 0.8rem;
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
    }
    .badge-Hired { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); }
    .badge-Fired { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.35); }
    .badge-Retired { background: rgba(148, 163, 184, 0.15); color: #cbd5e1; border: 1px solid rgba(148, 163, 184, 0.35); }
    .badge-OnLeave { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.35); }
    
    .table-custom {
      color: #f1f5f9;
      vertical-align: middle;
      margin-bottom: 0;
    }
    .table-custom thead th {
      background: rgba(15, 23, 42, 0.9);
      border-bottom: 1px solid var(--card-border);
      color: var(--text-muted);
      font-weight: 600;
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 1rem 0.75rem;
    }
    .table-custom tbody tr {
      border-bottom: 1px solid rgba(51, 65, 85, 0.25);
      transition: background 0.15s ease;
    }
    .table-custom tbody tr:hover {
      background: rgba(30, 41, 59, 0.5);
    }
    .avatar-circle {
      width: 40px;
      height: 40px;
      border-radius: 50%;
      background: linear-gradient(135deg, #1e293b, #334155);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 0.9rem;
      border: 1px solid var(--card-border);
    }
    .progress-custom {
      height: 8px;
      background-color: rgba(30, 41, 59, 0.8);
      border-radius: 10px;
      overflow: hidden;
    }
    .modal-content {
      background: #0f172a;
      border: 1px solid var(--card-border);
      border-radius: 20px;
      color: #f8fafc;
    }
    .form-control, .form-select {
      background-color: #1e293b;
      border: 1px solid #334155;
      color: #f8fafc;
      border-radius: 10px;
    }
    .form-control:focus, .form-select:focus {
      background-color: #1e293b;
      border-color: #3b82f6;
      color: #fff;
      box-shadow: 0 0 0 0.25rem rgba(59, 130, 246, 0.25);
    }
    .toast-container { position: fixed; bottom: 20px; right: 20px; z-index: 1055; }
    .pulse-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      display: inline-block;
      animation: pulse 1.8s infinite;
    }
    @keyframes pulse {
      0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
      70% { transform: scale(1); box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
      100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
    }
  </style>
</head>
<body>

  <!-- Top Navigation Bar -->
  <nav class="navbar navbar-expand-lg navbar-dark navbar-glass py-3">
    <div class="container-fluid px-4">
      <div class="d-flex align-items-center gap-3">
        <div class="stat-badge bg-warning bg-opacity-10 text-warning border border-warning border-opacity-25">
          <i class="fa-solid fa-building-columns"></i>
        </div>
        <div>
          <h5 class="mb-0 fw-bold tracking-tight">Blox RP National Bank</h5>
          <small class="text-secondary d-flex align-items-center gap-2">
            <span class="pulse-dot bg-success"></span> System Connected & Active
          </small>
        </div>
      </div>

      <ul class="nav nav-pills mt-3 mt-lg-0" id="mainTabs" role="tablist">
        <li class="nav-item">
          <button class="nav-link active" data-bs-toggle="pill" data-bs-target="#tab-staff">
            <i class="fa-solid fa-users"></i> Staff Roster
          </button>
        </li>
        <li class="nav-item">
          <button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-shifts" onclick="loadSubmissions()">
            <i class="fa-solid fa-clock-rotate-left"></i> Shift Audit Logs
          </button>
        </li>
        <li class="nav-item">
          <button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-leaderboard">
            <i class="fa-solid fa-trophy"></i> Leaderboards
          </button>
        </li>
        <li class="nav-item">
          <button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-settings" onclick="loadSettings()">
            <i class="fa-solid fa-sliders"></i> Bank Rates & Config
          </button>
        </li>
      </ul>

      <div class="d-flex align-items-center gap-2 mt-3 mt-lg-0">
        <button class="btn btn-outline-light btn-sm px-3 py-2 rounded-3" onclick="loadData()">
          <i class="fa-solid fa-arrows-rotate me-1"></i> Refresh
        </button>
      </div>
    </div>
  </nav>

  <!-- Main Container -->
  <div class="container-fluid px-4 py-4">

    <!-- KPI Metric Cards -->
    <div class="row g-3 mb-4">
      <div class="col-sm-6 col-xl-3">
        <div class="glass-card p-3 d-flex align-items-center justify-content-between">
          <div>
            <div class="text-secondary small fw-semibold text-uppercase">Active Staff</div>
            <h3 class="fw-bold mb-0 mt-1" id="stat-active">0</h3>
            <small class="text-success"><i class="fa-solid fa-check me-1"></i>Currently Hired</small>
          </div>
          <div class="stat-badge bg-primary bg-opacity-10 text-primary border border-primary border-opacity-25">
            <i class="fa-solid fa-user-check"></i>
          </div>
        </div>
      </div>
      <div class="col-sm-6 col-xl-3">
        <div class="glass-card p-3 d-flex align-items-center justify-content-between">
          <div>
            <div class="text-secondary small fw-semibold text-uppercase">Total Shift Hours</div>
            <h3 class="fw-bold mb-0 mt-1" id="stat-hours">0h</h3>
            <small class="text-info" id="stat-mins-detail">0 logged minutes</small>
          </div>
          <div class="stat-badge bg-info bg-opacity-10 text-info border border-info border-opacity-25">
            <i class="fa-solid fa-business-time"></i>
          </div>
        </div>
      </div>
      <div class="col-sm-6 col-xl-3">
        <div class="glass-card p-3 d-flex align-items-center justify-content-between">
          <div>
            <div class="text-secondary small fw-semibold text-uppercase">Weekly Payroll</div>
            <h3 class="fw-bold mb-0 mt-1 text-success" id="stat-weekly-pay">$0</h3>
            <small class="text-secondary">Base pay + extra bonuses</small>
          </div>
          <div class="stat-badge bg-success bg-opacity-10 text-success border border-success border-opacity-25">
            <i class="fa-solid fa-money-bill-wave"></i>
          </div>
        </div>
      </div>
      <div class="col-sm-6 col-xl-3">
        <div class="glass-card p-3 d-flex align-items-center justify-content-between">
          <div>
            <div class="text-secondary small fw-semibold text-uppercase">All-Time Payouts</div>
            <h3 class="fw-bold mb-0 mt-1 text-warning" id="stat-lifetime-pay">$0</h3>
            <small class="text-warning">Career bank payouts</small>
          </div>
          <div class="stat-badge bg-warning bg-opacity-10 text-warning border border-warning border-opacity-25">
            <i class="fa-solid fa-sack-dollar"></i>
          </div>
        </div>
      </div>
    </div>

    <!-- Tab Contents -->
    <div class="tab-content" id="mainTabContent">

      <!-- TAB 1: STAFF MANAGEMENT -->
      <div class="tab-pane fade show active" id="tab-staff">
        <div class="glass-card p-4">
          <div class="d-flex flex-column flex-md-row justify-content-between align-items-md-center gap-3 mb-4">
            <div>
              <h5 class="fw-bold mb-1"><i class="fa-solid fa-id-badge text-primary me-2"></i>Staff Directory & Quotas</h5>
              <p class="text-secondary small mb-0">Track shift quotas, calculate employee payouts, and manage ranks.</p>
            </div>
            <div class="d-flex flex-wrap gap-2">
              <input type="text" id="searchInput" class="form-control" style="width: 240px;" placeholder="Search employee..." onkeyup="renderStaffTable()">
              <select id="statusFilter" class="form-select" style="width: 170px;" onchange="renderStaffTable()">
                <option value="ALL">All Statuses</option>
                <option value="Hired">Hired Only</option>
                <option value="Fired">Fired</option>
                <option value="Retired">Retired</option>
                <option value="On Leave">On Leave</option>
              </select>
            </div>
          </div>

          <div class="table-responsive rounded-3">
            <table class="table table-custom">
              <thead>
                <tr>
                  <th>Employee</th>
                  <th>Rank</th>
                  <th>Status</th>
                  <th>Weekly Quota Progress</th>
                  <th>Weekly Earnings</th>
                  <th>Lifetime Hours</th>
                  <th>All-Time Made</th>
                  <th class="text-end">Actions</th>
                </tr>
              </thead>
              <tbody id="staffTableBody">
                <tr><td colspan="8" class="text-center py-5 text-secondary">Loading records...</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- TAB 2: SHIFT AUDIT LOGS -->
      <div class="tab-pane fade" id="tab-shifts">
        <div class="glass-card p-4">
          <div class="d-flex justify-content-between align-items-center mb-4">
            <div>
              <h5 class="fw-bold mb-1"><i class="fa-solid fa-clipboard-check text-info me-2"></i>Shift Verification Audit</h5>
              <p class="text-secondary small mb-0">Historical log of submitted in-game screenshots and supervisor approvals.</p>
            </div>
            <button class="btn btn-outline-info btn-sm" onclick="loadSubmissions()">
              <i class="fa-solid fa-rotate me-1"></i> Refresh Logs
            </button>
          </div>

          <div class="table-responsive rounded-3">
            <table class="table table-custom">
              <thead>
                <tr>
                  <th>Timestamp</th>
                  <th>Employee</th>
                  <th>Shift Duration</th>
                  <th>Proof Photo</th>
                  <th>Reviewed By</th>
                  <th>Shift Notes</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody id="submissionsTableBody">
                <tr><td colspan="7" class="text-center py-5 text-secondary">Loading audit trail...</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- TAB 3: LEADERBOARD -->
      <div class="tab-pane fade" id="tab-leaderboard">
        <div class="row g-4">
          <div class="col-lg-6">
            <div class="glass-card p-4 h-100">
              <h5 class="fw-bold mb-3"><i class="fa-solid fa-medal text-warning me-2"></i>Top Earners of All-Time</h5>
              <div id="topEarnersList" class="d-flex flex-column gap-3"></div>
            </div>
          </div>
          <div class="col-lg-6">
            <div class="glass-card p-4 h-100">
              <h5 class="fw-bold mb-3"><i class="fa-solid fa-stopwatch-20 text-info me-2"></i>Most Dedicated Bankers (Hours)</h5>
              <div id="topHoursList" class="d-flex flex-column gap-3"></div>
            </div>
          </div>
        </div>
      </div>

      <!-- TAB 4: SETTINGS & CONFIG -->
      <div class="tab-pane fade" id="tab-settings">
        <div class="row g-4">
          <div class="col-lg-6">
            <div class="glass-card p-4">
              <h5 class="fw-bold mb-3"><i class="fa-solid fa-coins text-warning me-2"></i>Bank Rates & Quotas</h5>
              <p class="text-secondary small mb-4">These rates automatically calculate employee weekly pay upon shift approval.</p>

              <div class="mb-3">
                <label class="form-label text-secondary small fw-bold">BASE PAY UPON MEETING QUOTA ($)</label>
                <input type="number" id="settingBasePay" class="form-control" placeholder="1000">
              </div>
              <div class="mb-3">
                <label class="form-label text-secondary small fw-bold">BONUS RATE PER 30 MINS OVER QUOTA ($)</label>
                <input type="number" id="settingBonusRate" class="form-control" placeholder="500">
              </div>
              <div class="mb-4">
                <label class="form-label text-secondary small fw-bold">DEFAULT WEEKLY QUOTA (MINUTES)</label>
                <input type="number" id="settingDefaultQuota" class="form-control" placeholder="120">
              </div>
              <button class="btn btn-primary px-4 fw-bold" onclick="saveSettings()">
                <i class="fa-solid fa-floppy-disk me-1"></i> Save Configuration
              </button>
            </div>
          </div>

          <div class="col-lg-6">
            <div class="glass-card p-4 border-danger border-opacity-25">
              <h5 class="fw-bold text-danger mb-3"><i class="fa-solid fa-triangle-exclamation me-2"></i>Executive Actions</h5>
              <p class="text-secondary small">Actions here impact all staff and reset weekly tracking counters.</p>

              <div class="p-3 rounded-3 bg-danger bg-opacity-10 border border-danger border-opacity-25 mb-4">
                <h6 class="text-danger fw-bold mb-1">Weekly Reset & Cycle Finalization</h6>
                <p class="small text-secondary mb-3">Resets everyone's current weekly minutes and weekly earnings to $0 for the new week. Lifetime hours and lifetime payouts are preserved safely.</p>
                <button class="btn btn-danger btn-sm px-3 fw-bold" onclick="confirmResetWeek()">
                  <i class="fa-solid fa-box-archive me-1"></i> Finalize & Reset Week
                </button>
              </div>

              <div class="p-3 rounded-3 bg-dark border border-secondary border-opacity-25">
                <h6 class="text-white fw-bold mb-1">Generate Discord Payroll Announcement</h6>
                <p class="small text-secondary mb-3">Copies a formatted Discord summary of everyone's payout for quick posting.</p>
                <button class="btn btn-outline-light btn-sm" onclick="copyDiscordPayroll()">
                  <i class="fa-solid fa-copy me-1"></i> Copy Discord Summary
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

    </div>
  </div>

  <!-- Employee Manage Modal -->
  <div class="modal fade" id="editStaffModal" tabindex="-1">
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content p-2">
        <div class="modal-header border-0 pb-0">
          <h5 class="modal-title fw-bold" id="modalUsername">Edit Employee</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
        </div>
        <div class="modal-body">
          <input type="hidden" id="editUsername">
          <div class="mb-3">
            <label class="form-label text-secondary small fw-bold">BANK RANK</label>
            <select id="editRank" class="form-select">
              <option value="Trainee">Trainee</option>
              <option value="Teller">Teller</option>
              <option value="Senior Teller">Senior Teller</option>
              <option value="Vault Guard">Vault Guard</option>
              <option value="Loan Officer">Loan Officer</option>
              <option value="Branch Manager">Branch Manager</option>
              <option value="Executive Board">Executive Board</option>
            </select>
          </div>
          <div class="mb-3">
            <label class="form-label text-secondary small fw-bold">EMPLOYMENT STATUS</label>
            <select id="editStatus" class="form-select">
              <option value="Hired">Hired (Active)</option>
              <option value="On Leave">On Leave</option>
              <option value="Retired">Retired (Honorable)</option>
              <option value="Fired">Fired (Terminated)</option>
            </select>
          </div>
          <div class="mb-3">
            <label class="form-label text-secondary small fw-bold">WEEKLY REQUIRED QUOTA (MINUTES)</label>
            <input type="number" id="editQuota" class="form-control" placeholder="120">
          </div>
        </div>
        <div class="modal-footer border-0 pt-0">
          <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-primary btn-sm fw-bold px-3" onclick="submitEditStaff()">Save Changes</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Lightbox Proof Image Modal -->
  <div class="modal fade" id="imageModal" tabindex="-1">
    <div class="modal-dialog modal-dialog-centered modal-lg">
      <div class="modal-content bg-transparent border-0">
        <div class="modal-body p-0 text-center position-relative">
          <button type="button" class="btn-close btn-close-white position-absolute top-0 end-0 m-3" data-bs-dismiss="modal"></button>
          <img id="lightboxImg" src="" class="img-fluid rounded-4 shadow-lg border border-secondary" alt="Proof">
        </div>
      </div>
    </div>
  </div>

  <!-- Toast Notification -->
  <div class="toast-container">
    <div id="appToast" class="toast align-items-center text-white bg-primary border-0" role="alert">
      <div class="d-flex">
        <div class="toast-body" id="toastMsg">Action completed successfully.</div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
      </div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
  <script>
    let staffData = [];
    let submissionsData = [];

    async function loadData() {
      try {
        const res = await fetch('/api/staff');
        staffData = await res.json();
        updateHeaderStats();
        renderStaffTable();
        renderLeaderboards();
      } catch (err) {
        showToast("Error loading staff data: " + err.message, "danger");
      }
    }

    function updateHeaderStats() {
      let active = 0, mins = 0, weeklyPay = 0, lifePay = 0;
      staffData.forEach(s => {
        if (s.status === "Hired") active++;
        mins += (s.lifetime_mins || 0);
        weeklyPay += (s.current_earnings || 0);
        lifePay += (s.lifetime_made || 0);
      });
      document.getElementById("stat-active").innerText = active;
      document.getElementById("stat-hours").innerText = (mins / 60).toFixed(1) + "h";
      document.getElementById("stat-mins-detail").innerText = mins.toLocaleString() + " total minutes";
      document.getElementById("stat-weekly-pay").innerText = "$" + weeklyPay.toLocaleString();
      document.getElementById("stat-lifetime-pay").innerText = "$" + lifePay.toLocaleString();
    }

    function renderStaffTable() {
      const search = document.getElementById("searchInput").value.toLowerCase();
      const filter = document.getElementById("statusFilter").value;
      const tbody = document.getElementById("staffTableBody");
      tbody.innerHTML = "";

      const filtered = staffData.filter(s => {
        const matchName = s.username.toLowerCase().includes(search);
        const matchStatus = filter === "ALL" || s.status === filter;
        return matchName && matchStatus;
      });

      if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" class="text-center py-5 text-secondary">No bank personnel matching criteria.</td></tr>`;
        return;
      }

      filtered.forEach(s => {
        const pct = s.quota > 0 ? Math.min(100, Math.round((s.current_mins / s.quota) * 100)) : 100;
        const progressColor = pct >= 100 ? "bg-success" : (pct > 40 ? "bg-primary" : "bg-warning");
        const statusClass = "badge-" + (s.status || "Hired").replace(" ", "");

        tbody.innerHTML += `
          <tr>
            <td>
              <div class="d-flex align-items-center gap-2">
                <div class="avatar-circle">${s.username.substring(0, 2).toUpperCase()}</div>
                <div>
                  <div class="fw-bold">${s.username}</div>
                  <small class="text-secondary">${s.discord_id ? 'Discord Linked' : 'Roblox ID'}</small>
                </div>
              </div>
            </td>
            <td><span class="badge bg-dark border border-secondary border-opacity-50">${s.rank || 'Teller'}</span></td>
            <td><span class="badge-status ${statusClass}"><span class="pulse-dot" style="background: currentColor;"></span>${s.status}</span></td>
            <td style="min-width: 180px;">
              <div class="d-flex justify-content-between small mb-1">
                <span>${s.current_mins} / ${s.quota}m</span>
                <span class="fw-bold">${pct}%</span>
              </div>
              <div class="progress progress-custom">
                <div class="progress-bar ${progressColor}" style="width: ${pct}%"></div>
              </div>
            </td>
            <td><span class="fw-bold text-success">$${(s.current_earnings || 0).toLocaleString()}</span></td>
            <td>${(s.lifetime_mins / 60).toFixed(1)} hrs</td>
            <td><span class="fw-bold text-warning">$${(s.lifetime_made || 0).toLocaleString()}</span></td>
            <td class="text-end">
              <button class="btn btn-outline-primary btn-sm px-2 py-1 rounded-2" onclick="openEditModal('${s.username}', '${s.rank}', '${s.status}', ${s.quota})">
                <i class="fa-solid fa-pen-to-square"></i> Manage
              </button>
            </td>
          </tr>
        `;
      });
    }

    function openEditModal(username, rank, status, quota) {
      document.getElementById("modalUsername").innerText = "Manage: " + username;
      document.getElementById("editUsername").value = username;
      document.getElementById("editRank").value = rank || "Teller";
      document.getElementById("editStatus").value = status || "Hired";
      document.getElementById("editQuota").value = quota || 120;
      new bootstrap.Modal(document.getElementById("editStaffModal")).show();
    }

    async function submitEditStaff() {
      const username = document.getElementById("editUsername").value;
      const rank = document.getElementById("editRank").value;
      const status = document.getElementById("editStatus").value;
      const quota = parseFloat(document.getElementById("editQuota").value) || 120;

      const res = await fetch('/api/edit-staff', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, rank, status, quota })
      });
      const data = await res.json();
      if (data.success) {
        bootstrap.Modal.getInstance(document.getElementById("editStaffModal")).hide();
        showToast("Employee details updated successfully!");
        loadData();
      }
    }

    async function loadSubmissions() {
      const res = await fetch('/api/submissions');
      submissionsData = await res.json();
      const tbody = document.getElementById("submissionsTableBody");
      tbody.innerHTML = "";

      if (submissionsData.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-5 text-secondary">No shift submissions recorded yet.</td></tr>`;
        return;
      }

      submissionsData.forEach(sub => {
        tbody.innerHTML += `
          <tr>
            <td><small class="text-secondary">${sub.timestamp}</small></td>
            <td><strong>${sub.username}</strong></td>
            <td><span class="badge bg-primary bg-opacity-25 text-primary border border-primary border-opacity-25">${sub.minutes} mins</span></td>
            <td>
              <button class="btn btn-outline-light btn-sm py-0 px-2" onclick="openLightbox('${sub.proof_url}')">
                <i class="fa-solid fa-image me-1"></i> View Photo
              </button>
            </td>
            <td><small class="text-info">${sub.reviewer || 'Supervisor'}</small></td>
            <td><small class="text-secondary">${sub.notes || 'Normal Shift'}</small></td>
            <td><span class="badge bg-success bg-opacity-15 text-success border border-success border-opacity-25">VERIFIED</span></td>
          </tr>
        `;
      });
    }

    function openLightbox(url) {
      document.getElementById("lightboxImg").src = url;
      new bootstrap.Modal(document.getElementById("imageModal")).show();
    }

    function renderLeaderboards() {
      const topEarners = [...staffData].sort((a,b) => (b.lifetime_made || 0) - (a.lifetime_made || 0)).slice(0, 5);
      const topHours = [...staffData].sort((a,b) => (b.lifetime_mins || 0) - (a.lifetime_mins || 0)).slice(0, 5);

      const earnersDiv = document.getElementById("topEarnersList");
      earnersDiv.innerHTML = "";
      topEarners.forEach((s, idx) => {
        const medal = idx === 0 ? "🥇" : (idx === 1 ? "🥈" : (idx === 2 ? "🥉" : `#${idx+1}`));
        earnersDiv.innerHTML += `
          <div class="d-flex align-items-center justify-content-between p-3 rounded-3 bg-dark border border-secondary border-opacity-25">
            <div class="d-flex align-items-center gap-3">
              <span class="fs-4">${medal}</span>
              <div>
                <div class="fw-bold">${s.username}</div>
                <small class="text-secondary">${s.rank || 'Teller'}</small>
              </div>
            </div>
            <div class="fs-5 fw-bold text-warning">$${(s.lifetime_made || 0).toLocaleString()}</div>
          </div>
        `;
      });

      const hoursDiv = document.getElementById("topHoursList");
      hoursDiv.innerHTML = "";
      topHours.forEach((s, idx) => {
        const medal = idx === 0 ? "🥇" : (idx === 1 ? "🥈" : (idx === 2 ? "🥉" : `#${idx+1}`));
        hoursDiv.innerHTML += `
          <div class="d-flex align-items-center justify-content-between p-3 rounded-3 bg-dark border border-secondary border-opacity-25">
            <div class="d-flex align-items-center gap-3">
              <span class="fs-4">${medal}</span>
              <div>
                <div class="fw-bold">${s.username}</div>
                <small class="text-secondary">${s.current_mins} mins this week</small>
              </div>
            </div>
            <div class="fs-5 fw-bold text-info">${((s.lifetime_mins || 0)/60).toFixed(1)} hrs</div>
          </div>
        `;
      });
    }

    async function loadSettings() {
      const res = await fetch('/api/settings');
      const data = await res.json();
      document.getElementById("settingBasePay").value = data.base_pay || 1000;
      document.getElementById("settingBonusRate").value = data.bonus_rate || 500;
      document.getElementById("settingDefaultQuota").value = data.default_quota || 120;
    }

    async function saveSettings() {
      const base_pay = document.getElementById("settingBasePay").value;
      const bonus_rate = document.getElementById("settingBonusRate").value;
      const default_quota = document.getElementById("settingDefaultQuota").value;

      await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_pay, bonus_rate, default_quota })
      });
      showToast("Bank rate configuration saved!");
    }

    async function confirmResetWeek() {
      if (!confirm("⚠️ Are you sure you want to finalize the weekly cycle? Current weekly minutes & earnings will reset to 0.")) return;
      await fetch('/api/reset-week', { method: 'POST' });
      showToast("Weekly shift counters reset successfully!");
      loadData();
    }

    function copyDiscordPayroll() {
      let msg = "🏦 **Blox RP Bank — Weekly Payroll Report**\\n\\n";
      staffData.filter(s => s.status === "Hired").forEach(s => {
        msg += "• **" + s.username + "** (" + (s.rank || "Teller") + ") - `" + s.current_mins + "/" + s.quota + "m` | Payout: **$" + (s.current_earnings || 0) + "**\\n";
      });
      navigator.clipboard.writeText(msg);
      showToast("Discord payroll announcement copied to clipboard!");
    }

    function showToast(msg, type = "primary") {
      const toastEl = document.getElementById("appToast");
      document.getElementById("toastMsg").innerText = msg;
      toastEl.className = `toast align-items-center text-white bg-${type} border-0`;
      new bootstrap.Toast(toastEl, { delay: 3000 }).show();
    }

    window.onload = loadData;
  </script>
</body>
</html>"""

# ----------------- WEB SERVER ROUTES -----------------
async def handle_index(request):
    return web.Response(text=HTML_DASHBOARD, content_type="text/html")

async def handle_get_staff(request):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM staff ORDER BY lifetime_made DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return web.json_response(rows)

async def handle_get_submissions(request):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM submissions ORDER BY timestamp DESC LIMIT 100")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return web.json_response(rows)

async def handle_edit_staff(request):
    data = await request.json()
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE staff 
        SET rank = ?, status = ?, quota = ?
        WHERE username = ?
    """, (data.get("rank"), data.get("status"), float(data.get("quota", 120)), data.get("username")))
    conn.commit()
    conn.close()
    return web.json_response({"success": True})

async def handle_get_settings(request):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM settings")
    rows = {r["key"]: r["value"] for r in c.fetchall()}
    conn.close()
    return web.json_response(rows)

async def handle_update_settings(request):
    data = await request.json()
    for k, v in data.items():
        set_setting(k, v)
    return web.json_response({"success": True})

async def handle_reset_week(request):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE staff SET current_mins = 0, current_earnings = 0")
    conn.commit()
    conn.close()
    return web.json_response({"success": True})

# ----------------- RUNNER -----------------
async def main():
    # Setup Web GUI
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/staff", handle_get_staff)
    app.router.add_get("/api/submissions", handle_get_submissions)
    app.router.add_post("/api/edit-staff", handle_edit_staff)
    app.router.add_get("/api/settings", handle_get_settings)
    app.router.add_post("/api/settings", handle_update_settings)
    app.router.add_post("/api/reset-week", handle_reset_week)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("SERVER_PORT") or os.environ.get("PORT") or 8080)
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Web Dashboard running on 0.0.0.0:{port} (SERVER_PORT: {os.environ.get('SERVER_PORT')})")

    # Start Discord Bot
    await bot.start(BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

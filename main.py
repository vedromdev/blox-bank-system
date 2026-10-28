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

BASE_PAY = 1000       # Base pay when quota is hit
BONUS_RATE = 500      # Extra bonus per 30 mins over quota
DEFAULT_QUOTA = 120   # 120 minutes weekly quota
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
    conn.commit()
    conn.close()

init_db()

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

        # Update SQLite DB
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (sub_id, datetime.now().strftime("%Y-%m-%d %H:%M"), username, str(self.submitter.id),
              minutes, self.proof_url, "ACCEPTED", interaction.user.display_name, notes))

        # Check existing staff
        c.execute("SELECT * FROM staff WHERE LOWER(username) = LOWER(?)", (username,))
        row = c.fetchone()

        if row:
            cur_mins = row["current_mins"] + minutes
            life_mins = row["lifetime_mins"] + minutes
            quota = row["quota"]

            bonus = 0
            if cur_mins > quota and quota > 0:
                bonus = int((cur_mins - quota) // 30) * BONUS_RATE
            cur_earnings = (BASE_PAY + bonus) if cur_mins >= quota else 0
            life_made = row["lifetime_made"] + (minutes * 10)

            c.execute("""
                UPDATE staff SET current_mins=?, current_earnings=?, lifetime_mins=?, lifetime_made=?
                WHERE LOWER(username)=LOWER(?)
            """, (cur_mins, cur_earnings, life_mins, life_made, username))
        else:
            c.execute("""
                INSERT INTO staff (username, discord_id, rank, status, quota, current_mins, current_earnings, lifetime_mins, lifetime_made)
                VALUES (?, ?, 'Teller', 'Hired', ?, ?, 0, ?, ?)
            """, (username, str(self.submitter.id), DEFAULT_QUOTA, minutes, minutes, minutes * 10))

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
        await interaction.response.send_message("❌ Verification channel not found. Please verify VERIFICATION_CHANNEL_ID setting.", ephemeral=True)
        return

    embed = discord.Embed(title="🏦 New Shift Progress Submission", color=0xf1c40f)
    embed.add_field(name="Submitted By", value=interaction.user.mention, inline=True)
    embed.add_field(name="Account Name", value=f"`{interaction.user.name}`", inline=True)
    embed.set_image(url=photo.url)
    embed.set_footer(text="Click 'Accept & Credit' to verify minutes")

    view = VerificationButtons(submitter=interaction.user, proof_url=photo.url)
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Your screenshot has been submitted to supervisors!", ephemeral=True)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (Slash commands synced)")

# ----------------- WEB GUI PANEL (HTML) -----------------
HTML_DASHBOARD = """
<!DOCTYPE html>
<html>
<head>
  <title>Blox RP Bank - Management Panel</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background-color: #0b132b; color: #f8fafc; font-family: 'Segoe UI', sans-serif; }
    .card { background-color: #1c2541; border: 1px solid #3a506b; border-radius: 12px; }
    .badge-Hired { background-color: #10b981; }
    .badge-Fired { background-color: #ef4444; }
    .badge-Retired { background-color: #64748b; }
    .badge-OnLeave { background-color: #f59e0b; }
  </style>
</head>
<body class="p-4">
  <div class="container-fluid">
    <div class="d-flex justify-content-between align-items-center mb-4">
      <h2>🏦 Blox RP Bank — Staff & Earnings Panel</h2>
      <div>
        <button class="btn btn-outline-danger me-2" onclick="resetWeek()">📦 Reset & Archive Week</button>
        <button class="btn btn-primary" onclick="loadData()">🔄 Refresh</button>
      </div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-md-4">
        <div class="card p-3">
          <small class="text-secondary">ACTIVE STAFF (HIRED)</small>
          <h3 id="stat-active">0</h3>
        </div>
      </div>
      <div class="col-md-4">
        <div class="card p-3">
          <small class="text-secondary">TOTAL SHIFT MINUTES</small>
          <h3 id="stat-mins">0m</h3>
        </div>
      </div>
      <div class="col-md-4">
        <div class="card p-3">
          <small class="text-secondary">TOTAL LIFETIME PAYOUTS</small>
          <h3 id="stat-payouts">$0</h3>
        </div>
      </div>
    </div>

    <div class="card p-3">
      <div class="d-flex justify-content-between mb-3">
        <input type="text" id="search" class="form-control w-25 bg-dark text-white border-secondary" placeholder="Search username..." onkeyup="renderTable()">
        <select id="statusFilter" class="form-select w-25 bg-dark text-white border-secondary" onchange="renderTable()">
          <option value="ALL">All Statuses</option>
          <option value="Hired">Hired</option>
          <option value="Fired">Fired</option>
          <option value="Retired">Retired</option>
          <option value="On Leave">On Leave</option>
        </select>
      </div>

      <div class="table-responsive">
        <table class="table table-dark table-hover align-middle">
          <thead>
            <tr class="text-secondary">
              <th>Roblox Username</th>
              <th>Rank</th>
              <th>Status</th>
              <th>Weekly Quota</th>
              <th>Weekly Earnings</th>
              <th>Lifetime Mins</th>
              <th>Total Lifetime Made</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody id="tableBody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    let staffData = [];

    async function loadData() {
      const res = await fetch('/api/staff');
      staffData = await res.json();
      updateStats();
      renderTable();
    }

    function updateStats() {
      let active = 0, mins = 0, pay = 0;
      staffData.forEach(s => {
        if(s.status === 'Hired') active++;
        mins += s.lifetime_mins;
        pay += s.lifetime_made;
      });
      document.getElementById('stat-active').innerText = active;
      document.getElementById('stat-mins').innerText = mins + 'm';
      document.getElementById('stat-payouts').innerText = '$' + pay.toLocaleString();
    }

    function renderTable() {
      const search = document.getElementById('search').value.toLowerCase();
      const filter = document.getElementById('statusFilter').value;
      const tbody = document.getElementById('tableBody');
      tbody.innerHTML = '';

      const filtered = staffData.filter(s => {
        const matchesName = s.username.toLowerCase().includes(search);
        const matchesStatus = (filter === 'ALL' || s.status === filter);
        return matchesName && matchesStatus;
      });

      if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-3">No staff records found.</td></tr>';
        return;
      }

      filtered.forEach(s => {
        const badgeClass = 'badge-' + (s.status.replace(' ', ''));
        tbody.innerHTML += `
          <tr>
            <td><strong>${s.username}</strong></td>
            <td>${s.rank}</td>
            <td><span class="badge ${badgeClass}">${s.status}</span></td>
            <td>${s.current_mins} / ${s.quota}m</td>
            <td class="text-success fw-bold">$${s.current_earnings}</td>
            <td>${s.lifetime_mins}m</td>
            <td class="text-warning fw-bold">$${s.lifetime_made.toLocaleString()}</td>
            <td>
              <select class="form-select form-select-sm bg-dark text-white border-secondary" onchange="changeStatus('${s.username}', this.value)">
                <option value="">Edit Status...</option>
                <option value="Hired">Hired</option>
                <option value="Fired">Fired</option>
                <option value="Retired">Retired</option>
                <option value="On Leave">On Leave</option>
              </select>
            </td>
          </tr>
        `;
      });
    }

    async function changeStatus(username, newStatus) {
      if(!newStatus) return;
      await fetch('/api/status', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ username, status: newStatus })
      });
      loadData();
    }

    async function resetWeek() {
      if(!confirm("Are you sure you want to reset current weekly minutes for all staff?")) return;
      await fetch('/api/reset-week', { method: 'POST' });
      loadData();
    }

    loadData();
  </script>
</body>
</html>
"""

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

async def handle_update_status(request):
    data = await request.json()
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE staff SET status = ? WHERE username = ?", (data["status"], data["username"]))
    conn.commit()
    conn.close()
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
    app.router.add_post("/api/status", handle_update_status)
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

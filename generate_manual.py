"""Generate the IBKR Trading Bot operations manual as a PDF."""

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable, Preformatted, KeepTogether,
)
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.pdfgen import canvas

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
NAVY       = HexColor("#1a2a3a")
TEAL       = HexColor("#0f7a7a")
LIGHT_TEAL = HexColor("#e8f5f5")
LIGHT_GREY = HexColor("#f4f4f4")
MID_GREY   = HexColor("#cccccc")
CODE_BG    = HexColor("#1e1e2e")
CODE_FG    = HexColor("#cdd6f4")
WARNING_BG = HexColor("#fff8e1")
WARNING_BD = HexColor("#f9a825")

W, H = A4
MARGIN = 20 * mm

# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------
base = getSampleStyleSheet()

def _style(name, **kw):
    return ParagraphStyle(name, **kw)

TITLE_STYLE = _style("DocTitle",
    fontName="Helvetica-Bold", fontSize=28, textColor=white,
    leading=34, alignment=TA_CENTER, spaceAfter=6)

SUBTITLE_STYLE = _style("DocSubtitle",
    fontName="Helvetica", fontSize=13, textColor=HexColor("#c0d8d8"),
    leading=18, alignment=TA_CENTER)

H1 = _style("H1",
    fontName="Helvetica-Bold", fontSize=15, textColor=NAVY,
    leading=19, spaceBefore=14, spaceAfter=6,
    borderPad=4, leftIndent=0)

H2 = _style("H2",
    fontName="Helvetica-Bold", fontSize=11, textColor=TEAL,
    leading=14, spaceBefore=10, spaceAfter=4)

H3 = _style("H3",
    fontName="Helvetica-BoldOblique", fontSize=10, textColor=NAVY,
    leading=13, spaceBefore=7, spaceAfter=3)

BODY = _style("Body",
    fontName="Helvetica", fontSize=9.5, textColor=HexColor("#222222"),
    leading=14, spaceAfter=5)

BULLET = _style("Bullet",
    fontName="Helvetica", fontSize=9.5, textColor=HexColor("#222222"),
    leading=13, leftIndent=12, bulletIndent=0, spaceAfter=2,
    bulletText="•")

CODE = _style("Code",
    fontName="Courier", fontSize=8.5, textColor=CODE_FG,
    leading=12, leftIndent=4, spaceAfter=0, backColor=CODE_BG)

NOTE = _style("Note",
    fontName="Helvetica-Oblique", fontSize=9, textColor=HexColor("#555555"),
    leading=13, leftIndent=8, spaceAfter=4, backColor=WARNING_BG,
    borderColor=WARNING_BD, borderWidth=0, borderPad=4)

TOC_H1 = _style("TOCH1",
    fontName="Helvetica-Bold", fontSize=10, textColor=NAVY, leading=14, spaceAfter=2)

TOC_H2 = _style("TOCH2",
    fontName="Helvetica", fontSize=9, textColor=HexColor("#444444"),
    leading=12, leftIndent=10, spaceAfter=1)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def h1(text):
    return [HRFlowable(width="100%", thickness=0.5, color=TEAL, spaceAfter=2),
            Paragraph(text, H1)]

def h2(text):
    return [Paragraph(text, H2)]

def h3(text):
    return [Paragraph(text, H3)]

def body(text):
    return Paragraph(text, BODY)

def bullet(text):
    return Paragraph(text, BULLET)

def note(text):
    return Paragraph(f"<b>Note:</b> {text}", NOTE)

def warning(text):
    return Paragraph(f"⚠️  {text}", NOTE)

def code_block(text):
    """Render a monospaced code block with dark background."""
    lines = text.strip("\n").split("\n")
    elements = []
    for line in lines:
        elements.append(Preformatted(line if line else " ", CODE))
    # Wrap in a table for background colour (Preformatted backColor is unreliable)
    inner = [[el] for el in elements]
    t = Table(inner, colWidths=[W - 2 * MARGIN - 4])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING",   (0, 0), (-1, 0),  6),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 1),
        ("BOTTOMPADDING",(0, -1),(-1, -1), 6),
        ("ROUNDEDCORNERS", [4]),
    ]))
    return t

def simple_table(headers, rows, col_widths=None):
    data = [headers] + rows
    if col_widths is None:
        n = len(headers)
        col_widths = [(W - 2 * MARGIN) / n] * n
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0),  TEAL),
        ("TEXTCOLOR",    (0, 0), (-1, 0),  white),
        ("FONTNAME",     (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0),  9),
        ("FONTNAME",     (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",     (0, 1), (-1, -1), 8.5),
        ("TEXTCOLOR",    (0, 1), (-1, -1), HexColor("#1a1a1a")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, LIGHT_GREY]),
        ("GRID",         (0, 0), (-1, -1), 0.4, MID_GREY),
        ("LEFTPADDING",  (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING",   (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
    ]))
    return t

def sp(n=4):
    return Spacer(1, n)

# ---------------------------------------------------------------------------
# Page templates — header/footer
# ---------------------------------------------------------------------------

class ManualDoc(SimpleDocTemplate):
    def handle_pageBegin(self):
        super().handle_pageBegin()

    def afterPage(self):
        pass  # footer drawn in onLaterPages


def _draw_header_footer(canvas_obj, doc):
    canvas_obj.saveState()
    page = doc.page

    # Skip cover page
    if page == 1:
        canvas_obj.restoreState()
        return

    # Header bar
    canvas_obj.setFillColor(NAVY)
    canvas_obj.rect(MARGIN, H - 14 * mm, W - 2 * MARGIN, 7 * mm, fill=1, stroke=0)
    canvas_obj.setFont("Helvetica-Bold", 8)
    canvas_obj.setFillColor(white)
    canvas_obj.drawString(MARGIN + 3 * mm, H - 10 * mm, "IBKR Trading Bot — Deployment & Operations Manual")
    canvas_obj.setFont("Helvetica", 8)
    canvas_obj.drawRightString(W - MARGIN - 3 * mm, H - 10 * mm, f"Page {page}")

    # Footer line
    canvas_obj.setStrokeColor(MID_GREY)
    canvas_obj.setLineWidth(0.4)
    canvas_obj.line(MARGIN, 13 * mm, W - MARGIN, 13 * mm)
    canvas_obj.setFont("Helvetica", 7.5)
    canvas_obj.setFillColor(HexColor("#888888"))
    canvas_obj.drawString(MARGIN, 9 * mm, "For paper trading only until fully validated on live account.")
    canvas_obj.drawRightString(W - MARGIN, 9 * mm, "Confidential — do not share")

    canvas_obj.restoreState()


def _draw_cover(canvas_obj, doc):
    """Cover page with a full bleed dark header panel."""
    canvas_obj.saveState()

    # Background panel top half
    canvas_obj.setFillColor(NAVY)
    canvas_obj.rect(0, H * 0.45, W, H * 0.55, fill=1, stroke=0)

    # Accent bar
    canvas_obj.setFillColor(TEAL)
    canvas_obj.rect(0, H * 0.45, W, 4 * mm, fill=1, stroke=0)

    # Title
    canvas_obj.setFont("Helvetica-Bold", 30)
    canvas_obj.setFillColor(white)
    canvas_obj.drawCentredString(W / 2, H * 0.72, "IBKR Trading Bot")

    canvas_obj.setFont("Helvetica-Bold", 16)
    canvas_obj.setFillColor(HexColor("#c0d8d8"))
    canvas_obj.drawCentredString(W / 2, H * 0.66, "Deployment & Operations Manual")

    canvas_obj.setFont("Helvetica", 10)
    canvas_obj.setFillColor(HexColor("#8ab0b0"))
    canvas_obj.drawCentredString(W / 2, H * 0.61, "Options Trading Bot — IB Gateway · Telegram · Three-Strategy System")

    # Subtitle box
    canvas_obj.setFillColor(LIGHT_TEAL)
    canvas_obj.roundRect(MARGIN * 2, H * 0.30, W - MARGIN * 4, H * 0.12, 6, fill=1, stroke=0)
    canvas_obj.setFont("Helvetica", 9.5)
    canvas_obj.setFillColor(NAVY)
    lines = [
        "Covers: VPS Setup  ·  IB Gateway  ·  Daily Operations",
        "Telegram Command Reference  ·  Market-Hours Scheduling",
        "Troubleshooting  ·  Emergency Procedures",
    ]
    y = H * 0.39
    for line in lines:
        canvas_obj.drawCentredString(W / 2, y, line)
        y -= 13

    # Footer note on cover
    canvas_obj.setFont("Helvetica-Oblique", 8)
    canvas_obj.setFillColor(HexColor("#aaaaaa"))
    canvas_obj.drawCentredString(W / 2, 18 * mm, "For paper trading use only until validated. Keep .env and credentials secure.")

    canvas_obj.restoreState()


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

def build_content():
    story = []

    # ---- Cover (placeholder — drawn in onFirstPage) ----
    story.append(Spacer(1, H * 0.55))   # push below the drawn panel
    story.append(PageBreak())

    # ---- TOC ----
    story += h1("Table of Contents")
    toc_rows = [
        ("1", "VPS Requirements", "3"),
        ("2", "One-Time Setup", "3"),
        ("",  "  2.1  System dependencies", "3"),
        ("",  "  2.2  Clone and configure", "3"),
        ("",  "  2.3  IB Gateway", "4"),
        ("",  "  2.4  Bootstrap IV history", "4"),
        ("",  "  2.5  systemd service", "4"),
        ("",  "  2.6  First run", "5"),
        ("3", "Market-Hours Operation", "5"),
        ("4", "Shutdown & Startup Procedures", "6"),
        ("5", "Automating the Schedule", "6"),
        ("",  "  Option A  —  Cloud provider scheduler", "6"),
        ("",  "  Option B  —  systemd timers (keep VM on)", "7"),
        ("",  "  Option C  —  Always-on VPS", "7"),
        ("6", "Daily Trading Operations", "8"),
        ("7", "Scheduled Maintenance", "10"),
        ("8", "Troubleshooting", "10"),
        ("9", "Emergency Procedures", "11"),
        ("10","Quick Reference Card", "12"),
    ]
    toc_data = [[Paragraph(f"<b>{s}</b>  {title}", TOC_H1 if s.strip() and not s.startswith(" ") else TOC_H2),
                 Paragraph(pg, TOC_H2)] for s, title, pg in toc_rows]
    toc_t = Table(toc_data, colWidths=[W - 2 * MARGIN - 20, 20])
    toc_t.setStyle(TableStyle([
        ("ALIGN",  (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, MID_GREY),
    ]))
    story.append(toc_t)
    story.append(PageBreak())

    # ================================================================
    # 1. VPS Requirements
    # ================================================================
    story += h1("1.  VPS Requirements")
    story.append(body("<b>Minimum specification:</b>"))
    for b in ["1 vCPU, 2 GB RAM, 20 GB disk",
              "Ubuntu 22.04 LTS (recommended)",
              "Static IP or fixed hostname"]:
        story.append(bullet(b))
    story.append(sp(8))
    story.append(body("<b>Firewall — inbound ports to open:</b>"))
    story.append(simple_table(
        ["Port", "Protocol", "Source", "Purpose"],
        [["22", "TCP", "Your IP only", "SSH access"],
         ["(all others)", "—", "CLOSED", "IB Gateway port 4002 must never be public"]],
        col_widths=[45, 50, 90, None],
    ))
    story.append(sp(6))
    story.append(warning(
        "IB Gateway has no authentication. Port 4002 is bound to 127.0.0.1 only in "
        "docker-compose.yml — never change this or expose it publicly."
    ))

    # ================================================================
    # 2. One-Time Setup
    # ================================================================
    story.append(sp(10))
    story += h1("2.  One-Time Setup")

    story += h2("2.1  Install system dependencies")
    story.append(code_block(
        "sudo apt update && sudo apt install -y \\\n"
        "  python3.11 python3.11-venv python3-pip \\\n"
        "  docker.io docker-compose-v2 git curl\n"
        "sudo systemctl enable --now docker\n"
        "sudo usermod -aG docker $USER\n"
        "# Log out and back in for the docker group to take effect"
    ))

    story += h2("2.2  Clone, configure, and install")
    story.append(code_block(
        "git clone https://github.com/rahuling/ibkr-trading-bot.git\n"
        "cd ibkr-trading-bot\n"
        "\n"
        "# Python virtual environment\n"
        "python3.11 -m venv venv\n"
        "source venv/bin/activate\n"
        "pip install -r requirements.txt\n"
        "\n"
        "# Directories\n"
        "mkdir -p data logs backups"
    ))

    story.append(sp(6))
    story.append(body("<b>Configure environment variables:</b>"))
    story.append(code_block(
        "cp .env.example .env\n"
        "nano .env"
    ))
    story.append(sp(4))
    story.append(simple_table(
        ["Variable", "Example value", "Notes"],
        [
            ["IBKR_USERNAME", "myibkruser", "IBKR account username"],
            ["IBKR_PASSWORD", "••••••••",   "IBKR account password"],
            ["TRADING_MODE",  "paper",       "Keep 'paper' until validated on live account"],
            ["TELEGRAM_BOT_TOKEN", "123:ABC…", "From @BotFather"],
            ["TELEGRAM_ALLOWED_USER_IDS", "123456789", "Your Telegram user ID (@userinfobot)"],
        ],
        col_widths=[110, 90, None],
    ))

    story.append(sp(6))
    story.append(body("<b>Configure trading parameters:</b>"))
    story.append(code_block(
        "cp config.yaml.example config.yaml\n"
        "nano config.yaml\n"
        "\n"
        "# Key settings to fill in:\n"
        "#   scanner.watchlist.core: [AAPL, MSFT, SPY, QQQ]\n"
        "#   scanner.watchlist.tactical: [AMD, META, TSLA]\n"
        "#   leap.momentum_watchlist: [NVDA, AMD, META]\n"
        "#   automation.level: 2        # human-approved (recommended)"
    ))

    story += h2("2.3  Start IB Gateway")
    story.append(code_block(
        "docker compose up -d\n"
        "\n"
        "# Verify — wait 30-60 seconds for login\n"
        "docker compose ps\n"
        "docker compose logs -f    # look for 'Gateway Started'"
    ))
    story.append(sp(4))
    story.append(note(
        "IB Gateway performs a mandatory restart at <b>11:59pm ET every night</b> (IBKR requirement). "
        "The bot detects the disconnect via heartbeat and auto-reconnects when the gateway comes back (~2 min). "
        "You will receive a Telegram alert on disconnect and a recovery notice on reconnect."
    ))
    story.append(sp(4))
    story.append(note(
        "IBKR permits only one active API session per account. Make sure TWS is not open on the "
        "same account simultaneously, or the gateway will fail to log in."
    ))

    story += h2("2.4  Bootstrap IV history (one-time, ~30 min)")
    story.append(body(
        "The IVR scanner requires 252 trading days of historical IV per ticker before it can "
        "rank candidates. Run this once before your first scan:"
    ))
    story.append(code_block(
        "source venv/bin/activate\n"
        "python scripts/bootstrap_iv_history.py"
    ))
    story.append(body("Safe to re-run — uses INSERT OR IGNORE. After bootstrap, IV history is updated automatically at 4:15pm ET daily."))

    story += h2("2.5  Install the systemd service")
    story.append(code_block(
        "sudo cp systemd/trading-bot.service /etc/systemd/system/\n"
        "sudo nano /etc/systemd/system/trading-bot.service\n"
        "\n"
        "# Update these two lines:\n"
        "User=ubuntu                            # your VPS username\n"
        "WorkingDirectory=/home/ubuntu/ibkr-trading-bot"
    ))
    story.append(sp(4))
    story.append(body("The service file should contain:"))
    story.append(code_block(
        "[Unit]\n"
        "Description=IBKR Trading Bot\n"
        "After=network.target docker.service\n"
        "Requires=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "User=ubuntu\n"
        "WorkingDirectory=/home/ubuntu/ibkr-trading-bot\n"
        "EnvironmentFile=/home/ubuntu/ibkr-trading-bot/.env\n"
        "ExecStart=/home/ubuntu/ibkr-trading-bot/venv/bin/python bot/main.py\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target"
    ))
    story.append(code_block(
        "sudo systemctl daemon-reload\n"
        "sudo systemctl enable trading-bot    # auto-start on boot"
    ))

    story += h2("2.6  First run — verify")
    story.append(code_block(
        "sudo systemctl start trading-bot\n"
        "sudo journalctl -u trading-bot -f    # watch startup"
    ))
    story.append(sp(4))
    story.append(body("You should see in the logs:"))
    story.append(code_block(
        "Config loaded OK. Automation level: L2\n"
        "IB Gateway connected. Net Liquidation: $...\n"
        "Reconciliation: OK — DB and IBKR positions match\n"
        "Scheduler started.\n"
        "Bot started. Waiting for commands."
    ))
    story.append(sp(4))
    story.append(body("Open Telegram and send <b>/status</b> to confirm IB Gateway is connected."))

    # ================================================================
    # 3. Market-Hours Operation
    # ================================================================
    story.append(PageBreak())
    story += h1("3.  Market-Hours Operation")

    story += h2("3.1  What runs automatically")
    story.append(simple_table(
        ["ET Time", "Event"],
        [
            ["9:30am",        "Morning summary — portfolio value, open positions, expiry alerts, yesterday's P&L"],
            ["9:45am",        "Premium-selling scan (Core + Tactical) — proposals pushed to Telegram"],
            ["3:00pm",        "Afternoon premium-selling scan"],
            ["3:30pm",        "EOD momentum scan — LEAP proposals"],
            ["4:15pm",        "IV history update for all watchlist tickers"],
            ["Every 5 min",   "Position monitor — live P&L/Greeks update, profit-target and strike-tested alerts"],
            ["Every 5 min",   "Heartbeat — alerts on 2 consecutive IBKR disconnects; recovery notice on reconnect"],
            ["11:59pm",       "IB Gateway mandatory restart (auto-reconnect, ~2 min downtime)"],
            ["1st of month 8am", "Monthly P&L report pushed to Telegram"],
        ],
        col_widths=[90, None],
    ))

    story += h2("3.2  What requires your action")
    story.append(simple_table(
        ["Event", "Your action"],
        [
            ["Trade proposal arrives",        "/approve <id>  or  /reject <id> [reason]"],
            ["Fill confirmation",              "No action needed — informational"],
            ["Assignment alert",              "CC proposal auto-generated — review + /approve"],
            ["Strike tested alert",           "Your CSP is near its strike — decide: hold, roll, or close"],
            ["Profit target alert",           "Bot suggests closing — use /close <id> to act"],
            ["Loss limit hit / Bot paused",   "Review positions, then /resume when satisfied"],
            ["Heartbeat disconnect alert",    "SSH to VPS, check docker compose ps, restart if needed"],
        ],
        col_widths=[120, None],
    ))

    # ================================================================
    # 4. Shutdown & Startup
    # ================================================================
    story.append(sp(10))
    story += h1("4.  Shutdown & Startup Procedures")

    story += h2("4.1  Manual daily shutdown  (after 4:30pm ET)")
    story.append(code_block(
        "# Stop the bot\n"
        "sudo systemctl stop trading-bot\n"
        "\n"
        "# Stop IB Gateway\n"
        "cd /home/ubuntu/ibkr-trading-bot\n"
        "docker compose down\n"
        "\n"
        "# Power off the VM\n"
        "sudo shutdown -h now"
    ))
    story.append(body("Or as a one-liner:"))
    story.append(code_block(
        "sudo systemctl stop trading-bot && \\\n"
        "docker compose -f /home/ubuntu/ibkr-trading-bot/docker-compose.yml down && \\\n"
        "sudo shutdown -h now"
    ))

    story += h2("4.2  Manual startup  (before 9:20am ET)")
    story.append(body("Start the VM from your cloud provider's console, then SSH in:"))
    story.append(code_block(
        "cd /home/ubuntu/ibkr-trading-bot\n"
        "docker compose up -d\n"
        "sleep 60                             # wait for IB Gateway login\n"
        "sudo systemctl start trading-bot\n"
        "\n"
        "# Confirm in Telegram\n"
        "# Send /status — should show IB Gateway: 🟢 Connected"
    ))

    # ================================================================
    # 5. Automating the Schedule
    # ================================================================
    story.append(PageBreak())
    story += h1("5.  Automating the Schedule")

    story.append(body(
        "Since the VM is powered off, it cannot schedule its own startup. "
        "Choose one of the three options below."
    ))
    story.append(sp(6))

    story += h2("Option A — Cloud provider scheduler  (recommended if using AWS/GCP)")
    story.append(body("Target window: <b>start at 9:15am ET, stop at 4:45pm ET, Mon–Fri.</b>"))
    story.append(sp(4))

    story.append(body("<b>AWS EC2</b> — EventBridge + Systems Manager (or Instance Scheduler):"))
    story.append(code_block(
        "# Start rule: 9:15am ET = 14:15 UTC (EDT) / 13:15 UTC (EST)\n"
        "aws events put-rule \\\n"
        "  --schedule-expression 'cron(15 14 ? * MON-FRI *)' \\\n"
        "  --name ibkr-bot-start --state ENABLED\n"
        "\n"
        "# Stop rule: 4:45pm ET = 21:45 UTC (EDT) / 20:45 UTC (EST)\n"
        "aws events put-rule \\\n"
        "  --schedule-expression 'cron(45 21 ? * MON-FRI *)' \\\n"
        "  --name ibkr-bot-stop --state ENABLED"
    ))
    story.append(warning(
        "US daylight saving time shifts the UTC offset between -4 (EDT, Mar–Nov) and -5 (EST, Nov–Mar). "
        "Adjust your UTC cron times at each DST change, or use AWS Instance Scheduler which handles "
        "DST automatically via the America/New_York timezone."
    ))

    story.append(sp(6))
    story.append(body("<b>GCP Compute Engine:</b> VM Manager → Instance Schedules → create schedule with America/New_York timezone."))
    story.append(sp(4))
    story.append(body("<b>DigitalOcean / Hetzner / Linode:</b> No native scheduler — use Option B or Option C."))

    story += h2("Option B — systemd timers  (keep VM always on, schedule bot only)")
    story.append(body(
        "The VM runs 24/7 but the bot and gateway only run during market hours. "
        "Simpler than cloud scheduling and handles DST automatically."
    ))
    story.append(code_block(
        "# /etc/systemd/system/ibkr-start.service\n"
        "[Unit]\n"
        "Description=Start IBKR bot for market open\n"
        "[Service]\n"
        "Type=oneshot\n"
        "User=root\n"
        "WorkingDirectory=/home/ubuntu/ibkr-trading-bot\n"
        "ExecStart=/usr/bin/docker compose up -d\n"
        "ExecStartPost=/bin/sleep 60\n"
        "ExecStartPost=/bin/systemctl start trading-bot"
    ))
    story.append(code_block(
        "# /etc/systemd/system/ibkr-start.timer\n"
        "[Unit]\n"
        "Description=Start IBKR bot at market open\n"
        "[Timer]\n"
        "OnCalendar=Mon-Fri 09:15:00 America/New_York\n"
        "Persistent=false\n"
        "[Install]\n"
        "WantedBy=timers.target"
    ))
    story.append(code_block(
        "# /etc/systemd/system/ibkr-stop.service\n"
        "[Unit]\n"
        "Description=Stop IBKR bot after market close\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/bin/systemctl stop trading-bot\n"
        "ExecStartPost=/usr/bin/docker compose \\\n"
        "  -f /home/ubuntu/ibkr-trading-bot/docker-compose.yml down"
    ))
    story.append(code_block(
        "# /etc/systemd/system/ibkr-stop.timer\n"
        "[Unit]\n"
        "Description=Stop IBKR bot after market close\n"
        "[Timer]\n"
        "OnCalendar=Mon-Fri 16:30:00 America/New_York\n"
        "Persistent=false\n"
        "[Install]\n"
        "WantedBy=timers.target"
    ))
    story.append(code_block(
        "sudo systemctl daemon-reload\n"
        "sudo systemctl enable --now ibkr-start.timer ibkr-stop.timer\n"
        "\n"
        "# Verify\n"
        "systemctl list-timers ibkr-*"
    ))
    story.append(note("Requires systemd 242+. Ubuntu 22.04 ships systemd 249 — compatible."))

    story += h2("Option C — Always-on VPS  (simplest)")
    story.append(body(
        "For a $5–6/month nano VPS, always-on costs less than the operational overhead of "
        "start/stop scheduling. With <b>systemctl enable trading-bot</b> already set, "
        "the bot starts automatically on every boot. The IB Gateway nightly restart is "
        "handled automatically."
    ))

    # ================================================================
    # 6. Daily Trading Operations
    # ================================================================
    story.append(PageBreak())
    story += h1("6.  Daily Trading Operations")

    story += h2("6.1  Typical morning flow")
    for txt in [
        "<b>9:30am</b> — Morning summary arrives. Check portfolio value, open positions, anything expiring this week.",
        "<b>9:45am</b> — Scan results arrive. 0–5 trade cards depending on IV conditions.",
        "<b>Review each card</b> — check underlying, strike, DTE, delta, IVR. Approve if it meets your criteria.",
        "<b>Fill confirmation</b> — arrives within seconds to a few minutes once submitted.",
    ]:
        story.append(bullet(txt))

    story += h2("6.2  Approving and rejecting proposals")
    story.append(code_block(
        "/approve ABC123          # approve — bot fetches fresh market data first\n"
        "/reject ABC123 IV drop   # reject with optional reason"
    ))
    story.append(sp(4))
    story.append(body(
        "The bot fetches a <b>fresh</b> bid-ask mid at approval time — not the stale price from scan time. "
        "You'll see the actual submission price in the confirmation."
    ))
    story.append(note("Proposals expire at 3:55pm ET even if the TTL hasn't elapsed, so you can't accidentally submit an order after the close blackout window."))

    story += h2("6.3  Checking open positions")
    story.append(code_block("/positions"))
    story.append(body("Shows all open positions with live P&L, delta (Δ), theta (Θ), and trade ID."))

    story += h2("6.4  Rolling a CSP")
    story.append(body("When a CSP gets close to its strike you'll receive a <b>Strike Tested</b> alert. Check roll economics:"))
    story.append(code_block(
        "/roll <trade_id>\n"
        "\n"
        "# Output shows: current strike, roll strike, close debit, new credit, net credit\n"
        "# To execute the roll:\n"
        "/close <trade_id>           # 1. close current position\n"
        "/scan                       # 2. generate fresh proposals\n"
        "/approve <new_proposal_id>  # 3. enter the new position"
    ))

    story += h2("6.5  The Wheel — assignment and covered call")
    story.append(body("When a CSP is assigned:"))
    for txt in [
        "You receive an <b>Assignment</b> alert with net cost basis (strike − premium collected)",
        "A <b>Covered Call proposal</b> is automatically generated and sent to Telegram",
        "Review it — check that the strike is above cost basis (<i>'above cost basis'</i> tag confirms profit if called away)",
        "<b>/approve &lt;id&gt;</b> to sell the covered call",
    ]:
        story.append(bullet(txt))
    story.append(sp(4))
    story.append(body("If the CC proposal doesn't arrive (market was closed at assignment time):"))
    story.append(code_block("/scan     # detects assigned position and generates CC proposal"))

    story += h2("6.6  Closing a position manually")
    story.append(code_block(
        "/close <trade_id>          # limit order at mid (default)\n"
        "/close <trade_id> bid      # 1 tick above mid — more urgent\n"
        "/close <trade_id> market   # emergency only — warning alert sent first"
    ))
    story.append(body("If the close order doesn't fill, the bot reprices once (1 tick) before giving up. You'll be notified either way."))

    story += h2("6.7  Wheel cycle P&L")
    story.append(code_block(
        "/wheel              # list recent cycles\n"
        "/wheel <cycle_id>   # full P&L for one cycle (CSP + CC combined)"
    ))

    story += h2("6.8  Journal and analytics")
    story.append(code_block(
        "/journal 30         # last 30 days of closed trades\n"
        "/journal 7          # just this week\n"
        "\n"
        "/analyze ivr        # win rate by IVR bucket\n"
        "/analyze dte        # win rate by DTE bucket\n"
        "/analyze strategy   # by strategy"
    ))

    story += h2("6.9  Risk dashboard")
    story.append(code_block("/risk"))
    story.append(body("Shows bucket usage (Core/Tactical/Momentum), daily/weekly/monthly P&L vs limits, PDT status."))

    story += h2("6.10  Adjusting settings at runtime")
    story.append(code_block(
        "# View all current settings\n"
        "/config\n"
        "\n"
        "# Adjust a parameter (changes take effect immediately, are audited in DB)\n"
        "/setconfig risk.min_ivr_core 35\n"
        "/setconfig risk.daily_loss_limit_pct 0.02\n"
        "/setconfig automation.level 1     # alerts-only mode\n"
        "\n"
        "# Manage watchlist at runtime\n"
        "/addticker GOOGL core\n"
        "/addticker MARA momentum\n"
        "/removeticker COIN tactical\n"
        "/watchlist"
    ))
    story.append(note("Watchlist changes made via /addticker and /removeticker are in-memory only — they reset on bot restart. To persist them, update config.yaml."))

    # ================================================================
    # 7. Scheduled Maintenance
    # ================================================================
    story.append(PageBreak())
    story += h1("7.  Scheduled Maintenance")

    story += h2("7.1  Log monitoring")
    story.append(code_block(
        "# Live stream\n"
        "sudo journalctl -u trading-bot -f\n"
        "\n"
        "# Last 100 lines\n"
        "sudo journalctl -u trading-bot -n 100\n"
        "\n"
        "# Today only\n"
        "sudo journalctl -u trading-bot --since today\n"
        "\n"
        "# Rotating file log (also written here)\n"
        "tail -f /home/ubuntu/ibkr-trading-bot/logs/bot.log"
    ))

    story += h2("7.2  Database backup")
    story.append(code_block(
        "cd /home/ubuntu/ibkr-trading-bot\n"
        "bash scripts/backup.sh\n"
        "ls backups/\n"
        "\n"
        "# Schedule weekly backup (add to crontab)\n"
        "crontab -e\n"
        "# Add this line:\n"
        "0 20 * * 5 /home/ubuntu/ibkr-trading-bot/scripts/backup.sh"
    ))

    story += h2("7.3  Updating the bot")
    story.append(code_block(
        "sudo systemctl stop trading-bot\n"
        "cd /home/ubuntu/ibkr-trading-bot\n"
        "git pull\n"
        "source venv/bin/activate\n"
        "pip install -r requirements.txt    # only if requirements changed\n"
        "sudo systemctl start trading-bot"
    ))

    # ================================================================
    # 8. Troubleshooting
    # ================================================================
    story += h1("8.  Troubleshooting")

    story += h2("8.1  IB Gateway disconnected / heartbeat alert")
    story.append(code_block(
        "docker compose ps              # check container status\n"
        "docker compose logs --tail 50  # look for login errors\n"
        "docker compose restart         # restart gateway"
    ))
    story.append(body("Common causes:"))
    for txt in [
        "Wrong credentials in <b>.env</b>",
        "TWS or another API session open on the same IBKR account simultaneously",
        "IB Gateway nightly restart at 11:59pm ET got stuck — <code>docker compose restart</code> fixes this",
    ]:
        story.append(bullet(txt))
    story.append(body("The bot reconnects automatically once the gateway is available. Confirm with <b>/status</b>."))

    story += h2("8.2  Reconciliation mismatch on startup")
    story.append(code_block("/reconcile"))
    story.append(simple_table(
        ["Mismatch type", "Cause", "Fix"],
        [
            ["In DB but not IBKR", "Position closed in TWS / expired", "Use /close <id> to mark it closed in the DB"],
            ["In IBKR but not DB", "Position opened manually in TWS",  "Close it in TWS, or add it to DB manually"],
        ],
        col_widths=[100, 120, None],
    ))

    story += h2("8.3  Proposal expired before approval")
    story.append(code_block("/scan     # generates fresh proposals"))

    story += h2("8.4  Order submitted but no fill notification")
    story.append(code_block(
        "/positions   # if it appears here, fill was recorded\n"
        "\n"
        "# If not, check logs:\n"
        "sudo journalctl -u trading-bot -n 200 | grep -E 'fill|order|reprice'"
    ))
    story.append(body(
        "The bot reprices once after 5 minutes and cancels if still unfilled after 3 more minutes. "
        "You'll receive a notification either way."
    ))

    story += h2("8.5  Bot won't start")
    story.append(code_block(
        "sudo journalctl -u trading-bot -n 50   # read the error\n"
        "sudo systemctl status trading-bot"
    ))
    story.append(simple_table(
        ["Error", "Fix"],
        [
            ["Config validation error", "Fix the offending value in config.yaml and restart"],
            ["Database locked",         "fuser data/trading.db — kill the locking process"],
            ["IB Gateway unreachable",  "docker compose up -d first, then start the bot"],
        ],
        col_widths=[130, None],
    ))

    story += h2("8.6  Was my order filled during a crash?")
    story.append(body(
        "On restart, the bot automatically runs <b>recover_orphaned_orders</b>: it checks "
        "<code>pending_submit</code> orders against live IBKR open orders, and "
        "<code>submitted</code> orders against IBKR execution reports. "
        "If a fill happened during the crash it is recovered automatically and you'll receive "
        "a fill notification. Then run <b>/reconcile</b> to confirm."
    ))

    # ================================================================
    # 9. Emergency Procedures
    # ================================================================
    story += h1("9.  Emergency Procedures")

    story += h2("9.1  Immediately stop all trading")
    story.append(code_block(
        "/pause     # blocks all new proposals and order submission\n"
        "           # existing open positions are unaffected\n"
        "\n"
        "/resume    # when ready to trade again"
    ))

    story += h2("9.2  Emergency close a position")
    story.append(code_block(
        "/close <trade_id> market    # market order — warning alert sent first"
    ))
    story.append(warning("Only use market orders if you need out immediately regardless of price. Always try limit first."))

    story += h2("9.3  Close position in TWS (bypass the bot entirely)")
    story.append(body("Close the position directly in TWS, then:"))
    story.append(code_block(
        "/reconcile   # bot detects the mismatch and alerts you"
    ))
    story.append(body("The bot won't silently update the DB from TWS closes. To mark the trade closed manually:"))
    story.append(code_block(
        "sqlite3 /home/ubuntu/ibkr-trading-bot/data/trading.db \\\n"
        "  \"UPDATE trades SET status='closed', exit_reason='manual_tws',\n"
        "    exit_date=datetime('now') WHERE trade_id='<id>';\""
    ))

    story += h2("9.4  Kill the bot without systemd")
    story.append(code_block("pkill -f 'python bot/main.py'"))

    # ================================================================
    # 10. Quick Reference Card
    # ================================================================
    story.append(PageBreak())
    story += h1("10.  Quick Reference Card")

    story += h2("Telegram commands")
    story.append(simple_table(
        ["Command", "Description"],
        [
            ["/status",                    "IB Gateway connection, account, balance, automation level"],
            ["/positions",                 "Open positions with live P&L, delta, theta"],
            ["/risk",                      "Capital allocation, bucket usage, loss limits, PDT status"],
            ["/scan",                      "Trigger manual scan (CSP, Spread, + pending CC proposals)"],
            ["/approve <id>",              "Approve proposal — fetches fresh price, submits order"],
            ["/reject <id> [reason]",      "Reject proposal"],
            ["/close <id>",                "Limit close at mid price"],
            ["/close <id> bid",            "Urgent close — 1 tick above mid"],
            ["/close <id> market",         "Emergency market close"],
            ["/roll <id>",                 "Show roll economics for an open CSP"],
            ["/wheel [id]",                "Wheel cycle P&L — omit id for recent cycle list"],
            ["/journal [days]",            "Closed trade log (default 30 days)"],
            ["/analyze [dim]",             "Win rate by: ivr / dte / strategy / rule tag"],
            ["/config",                    "Show current configuration"],
            ["/setconfig <param> <value>", "Adjust a parameter at runtime"],
            ["/watchlist",                 "Show all watchlists"],
            ["/addticker <T> <bucket>",    "Add ticker to core / tactical / momentum"],
            ["/removeticker <T> <bucket>", "Remove ticker"],
            ["/pause",                     "Halt scanning and order submission"],
            ["/resume",                    "Resume after pause"],
            ["/reconcile",                 "Force DB ↔ IBKR position comparison"],
        ],
        col_widths=[130, None],
    ))

    story.append(sp(10))
    story += h2("VPS / shell commands")
    story.append(simple_table(
        ["Command", "Purpose"],
        [
            ["sudo journalctl -u trading-bot -f",              "Live log stream"],
            ["sudo systemctl restart trading-bot",             "Restart bot"],
            ["sudo systemctl stop/start trading-bot",          "Stop or start bot"],
            ["docker compose ps",                              "Check gateway container status"],
            ["docker compose restart",                         "Restart IB Gateway"],
            ["docker compose logs -f",                         "IB Gateway logs"],
            ["bash scripts/backup.sh",                         "Backup database"],
        ],
        col_widths=[185, None],
    ))

    story.append(sp(10))
    story += h2("Automation level reference")
    story.append(simple_table(
        ["Level", "Behaviour"],
        [
            ["L1 — Alerts only", "Bot scans and notifies; no order submission"],
            ["L2 — Assisted (default)", "Bot proposes, human approves via /approve"],
            ["L3 — Autonomous", "Bot executes without approval (except positions > 20% of portfolio)"],
        ],
        col_widths=[130, None],
    ))

    return story


# ---------------------------------------------------------------------------
# Build PDF
# ---------------------------------------------------------------------------

def main():
    output_path = "IBKR_Trading_Bot_Manual.pdf"

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        title="IBKR Trading Bot — Deployment & Operations Manual",
        author="Rahul Prasad",
        subject="Operations Manual",
    )

    story = build_content()

    doc.build(
        story,
        onFirstPage=_draw_cover,
        onLaterPages=_draw_header_footer,
    )

    print(f"PDF written to: {output_path}")


if __name__ == "__main__":
    main()

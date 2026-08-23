# Running the engine on a schedule

`--daily` is safe to run unattended. It makes no outbound contact, sends
nothing, and respects every budget cap. Three cadences are worth setting up.

## Manual (the default)

Run it when you sit down to work. Nothing is scheduled, nothing happens
behind your back.

```bash
python3 -m wholesale_engine.main --daily
```

## Daily

Once each morning, before you start calling. Ingests new leads, detects what
moved overnight, and writes the ranked list of what needs attention.

```bash
python3 -m wholesale_engine.main --daily --quiet \
    --out-dir ~/wholesale/reports
```

## Weekly

A wider net and a backup, once a week. Raise the caps deliberately — this is
the run that costs money once a live provider is connected.

```bash
python3 -m wholesale_engine.main --daily \
    --max-raw-leads 5000 --max-research 300 --max-comps 100 \
    --backup --quiet
```

---

## macOS / Linux — cron

`crontab -e`, then:

```cron
# Daily at 7:00am. Absolute paths: cron has almost no environment.
0 7 * * * cd /path/to/wholesale-code && /usr/bin/python3 -m wholesale_engine.main --daily --quiet >> ~/wholesale-daily.log 2>&1

# Weekly on Sunday at 6:00am, wider net plus a backup.
0 6 * * 0 cd /path/to/wholesale-code && /usr/bin/python3 -m wholesale_engine.main --daily --backup --max-research 300 --quiet >> ~/wholesale-weekly.log 2>&1
```

Two things cron gets wrong by default:

- **It does not read your shell profile**, so `.env` is the only place
  credentials will come from. The engine loads it from the project root — the
  `cd` above is what makes that work.
- **It has no TTY**, so a prompt cannot be answered. Bulk skip tracing
  therefore *declines* rather than assuming yes. If you want a scheduled run
  to trace, pass `--auto-skip-trace` deliberately and set `MAX_SKIP_TRACES`
  to a number you are happy to pay for every single day.

Check it is working:

```bash
tail -f ~/wholesale-daily.log
```

## macOS — launchd

cron works on macOS, but launchd survives reboots more predictably. Save as
`~/Library/LaunchAgents/com.wholesale.daily.plist` and
`launchctl load` it:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.wholesale.daily</string>
    <key>WorkingDirectory</key><string>/path/to/wholesale-code</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>-m</string>
        <string>wholesale_engine.main</string>
        <string>--daily</string>
        <string>--quiet</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    <key>StandardOutPath</key><string>/tmp/wholesale-daily.log</string>
    <key>StandardErrorPath</key><string>/tmp/wholesale-daily.err</string>
</dict>
</plist>
```

## Windows — Task Scheduler

Create `daily.bat` in the project folder:

```bat
@echo off
cd /d "C:\path\to\wholesale-code"
python -m wholesale_engine.main --daily --quiet >> "%USERPROFILE%\wholesale-daily.log" 2>&1
```

Then either use the Task Scheduler GUI (Create Basic Task → Daily → Start a
program → `daily.bat`), or from an elevated PowerShell:

```powershell
schtasks /create /tn "Wholesale Daily" /tr "C:\path\to\wholesale-code\daily.bat" /sc daily /st 07:00
schtasks /run /tn "Wholesale Daily"     # test it now
schtasks /query /tn "Wholesale Daily"   # check it is registered
```

Under **Conditions**, turn off "Start the task only if the computer is on AC
power" if you want it to run on a laptop.

---

## What a scheduled run will and will not do

**Will:** pull leads, deduplicate, detect changes, score, research within the
caps, calculate deals, rank the work, write the reports, and raise console
notifications.

**Will not:** call, text, email, or skip trace anyone without you asking. A
scheduled run has no TTY, so every confirmation prompt declines by default.

## Keep it manual, too

Nothing about scheduling changes the manual commands. `--dashboard`,
`--contact-queue`, `--deal-room` and the rest work exactly the same whether or
not a schedule exists, and you can always run `--daily` by hand.

## This is deliberately not a cloud service

No server, no hosted queue, no account. The engine is a local program with a
local SQLite database, and a scheduler you control. That means your property
data and your credentials stay on your machine — and that a backup is your
responsibility:

```bash
python3 -m wholesale_engine.main --backup
```

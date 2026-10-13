# Tapo Local Dashboard — Install & Run Guide

This guide gets the H500 recording browser, live view, and the Devices page running from a fresh clone. Follow the Linux / Raspberry Pi blocks or the Windows blocks. Both use the same repository.

This is an unofficial project. It is not affiliated with TP-Link. A Tapo firmware update can change the local API and may require code changes.

## Prerequisites

- Ubuntu 22.04+ or Raspberry Pi OS (64-bit), or Windows 10/11
- Python 3.10+
- git
- ffmpeg. go2rtc uses it to encode each camera preview JPEG. `pip` does not install it.
- Network access to the Tapo H500 hub
- Python packages from `requirements.txt`: FastAPI, uvicorn, Jinja2, python-multipart, and `tapo` 0.10 or newer
- go2rtc, installed separately. `pip` does not install it.

## 1. Clone and set up Python

Linux or Raspberry Pi:

```bash
cd ~
git clone https://github.com/GitRomeoG2020/tapo-local.git
cd tapo-local
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Windows, from the folder where you want the project:

```powershell
git clone https://github.com/GitRomeoG2020/tapo-local.git
cd tapo-local
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Open a new terminal later and activate the same `venv` before you run the dashboard.

## 2. Environment variables

The dashboard and `scripts/h500_poc.py` read three variables. Do not put the password, or these values, in the repository.

| Variable | Required | Purpose |
| --- | --- | --- |
| `TAPO_USERNAME` | yes | Cloud TP-Link account that owns the hub, usually an email address |
| `TAPO_PASSWORD` | yes | Password for that account |
| `TAPO_HUB_IP` | yes | LAN IPv4 address of the H500, such as `192.168.1.10` |

`TAPO_HUB_IP` is the hub's address on your network. Use your hub's address, not the sample `192.168.1.10`. If it is missing or is not an IPv4 address, the app stops with a clear error and does not try to log in.

A rejected password is not retried. A locked hub refuses further logins for a while.

Put all three variables in the place that matches how you start the dashboard. A value stored for one of these does not fill in the others.

| How you start it | Where the three variables go |
| --- | --- |
| Linux or Raspberry Pi, a terminal you open yourself | `export` lines in that terminal, or in `~/.bashrc` |
| Linux or Raspberry Pi, the systemd service in section 7 | `Environment=` lines in the unit file |
| Windows, the PowerShell window you already have open | `$env:` assignments in that window |
| Windows, PowerShell windows you open later | Permanent user environment variables |

### Linux and Raspberry Pi — manual run

Use this when you start uvicorn yourself in a terminal (section 6).

For the current terminal:

```bash
export TAPO_USERNAME="your-email@example.com"
export TAPO_PASSWORD="your-tapo-cloud-password"
export TAPO_HUB_IP="192.168.1.10"
```

To keep them for every new interactive terminal, add those three `export` lines to the end of `~/.bashrc`, then load them:

```bash
source ~/.bashrc
```

`~/.bashrc` is read by interactive bash. It is the right place for a manual run. It is not read by the systemd service.

### Linux and Raspberry Pi — systemd service

Use this when a Raspberry Pi or other Linux machine starts the dashboard on boot. systemd starts the process directly. It does not open a shell and it does not read `~/.bashrc`, even when `User=` is the account that owns that file. Variables that exist in an interactive shell can still be missing from the service.

Put the same three values in the unit with `Environment=` lines. Quotes keep a password that contains a space intact:

```ini
Environment="TAPO_USERNAME=your-email@example.com"
Environment="TAPO_PASSWORD=your-tapo-cloud-password"
Environment="TAPO_HUB_IP=192.168.1.10"
```

`tapo-dashboard.service.example` is that unit with these three lines. Section 7 shows how to install it. If the lines are missing, the service logs `Set TAPO_HUB_IP to the LAN address of the H500 hub.` and does not try to log in.

Use both places when you sometimes start it by hand and sometimes as a service: `~/.bashrc` for the terminal, and the three `Environment=` lines for the service.

### Windows

Windows does not use the systemd unit. The dashboard sees the variables of the PowerShell window that starts it.

For the current PowerShell window:

```powershell
$env:TAPO_USERNAME = "your-email@example.com"
$env:TAPO_PASSWORD = "your-tapo-cloud-password"
$env:TAPO_HUB_IP = "192.168.1.10"
```

To keep them for your Windows user, store them once. This does not change the window you already have open:

```powershell
[System.Environment]::SetEnvironmentVariable("TAPO_USERNAME", "your-email@example.com", "User")
[System.Environment]::SetEnvironmentVariable("TAPO_PASSWORD", "your-tapo-cloud-password", "User")
[System.Environment]::SetEnvironmentVariable("TAPO_HUB_IP", "192.168.1.10", "User")
```

Close PowerShell and open a new window so the saved values load. Activate `venv` again in that window. A window that was already open keeps the old `$env:` values until you set them there too.

## 3. ffmpeg (camera thumbnails)

Previews on the Cameras and Live View pages ask go2rtc for a small JPEG. go2rtc runs `ffmpeg` to encode that frame. Live view playback does not use ffmpeg. When `ffmpeg` is missing, the camera list still loads and every preview stays on the placeholder.

Linux or Raspberry Pi:

```bash
sudo apt install -y ffmpeg
```

Windows. Install a build that puts `ffmpeg.exe` on your user PATH, then open a new PowerShell window:

```powershell
winget install --id Gyan.FFmpeg -e
```

Check either machine with `ffmpeg -version`.

## 4. go2rtc (live view)

Live view needs two files that are not in git:

- `bin/go2rtc` on Linux, or `bin/go2rtc.exe` on Windows. Download the binary from the [go2rtc releases](https://github.com/AlexxIT/go2rtc/releases).
- `bin/go2rtc.yaml`. The dashboard reads stream names from this file. It does not start go2rtc.

Leave both files in `bin/` on each machine. Do not commit them. The yaml contains the camera account secret inside the `tapo://` URLs.

Copy `bin/go2rtc.yaml.example` and edit the placeholders. One stream per camera. Battery cameras on the H500 use the hub address plus `deviceId`.

Linux or Raspberry Pi:

```bash
cp bin/go2rtc.yaml.example bin/go2rtc.yaml
```

Windows:

```powershell
Copy-Item bin\go2rtc.yaml.example bin\go2rtc.yaml
```

The password in each URL is the uppercase SHA256 of `TAPO_PASSWORD`, not the password itself. `deviceId` is the hub child id: 8 to 64 hexadecimal characters. The host is the same address as `TAPO_HUB_IP`. Comments in `bin/go2rtc.yaml.example` describe both fields.

Generate the SHA256 from the cloud password.

Linux or Raspberry Pi:

```bash
echo -n "your-tapo-cloud-password" | sha256sum | awk '{print toupper($1)}'
```

Windows:

```powershell
$hash = [System.BitConverter]::ToString(
  [System.Security.Cryptography.SHA256]::Create().ComputeHash(
    [System.Text.Encoding]::UTF8.GetBytes("your-tapo-cloud-password")
  )
).Replace("-", "")
$hash
```

The Live View page lists each name under `streams` and links to `http://<host>:1984/stream.html?src=<name>`. It also links to the full go2rtc UI on port 1984. Stream URL values stay in the yaml.

Previews load after the list. Each one asks go2rtc for a small JPEG (`width=320`), waits up to 15 seconds, and tries once more. A Raspberry Pi often needs that extra time while a battery camera wakes and the frame is encoded. One preview is encoded at a time, so several images appear in turn. A preview that still does not arrive shows a placeholder, and the rest of the page stays up.

Live View has a Sync cameras to go2rtc button. It uses the same hub camera list as the Cameras page and appends any camera whose device id is not already in `bin/go2rtc.yaml`. Existing stream lines are left as they are. Each new line looks like `tapo://admin:UPPERCASE_SHA256@<hub-ip>/?deviceId=<id>`. The hash is the uppercase SHA256 of `TAPO_PASSWORD`. After the page names what it added, restart go2rtc so the new streams load.

Linux or Raspberry Pi, when go2rtc is a service:

```bash
sudo systemctl restart go2rtc
```

Windows: stop `go2rtc.exe` and start it again.

## 5. Extra devices (optional)

Plugs and lights on another subnet or VLAN go in `extra_devices.yaml` in the project root. When that file is missing, or it only has comments, the Devices page stays on the subnet scan.

`extra_devices.yaml` is git-ignored. Create it by hand on each machine, including this PC and the Raspberry Pi. `git pull` does not download it.

The file looks like this. `model` is optional (`L530`, `P110`, and similar).

```yaml
extra_devices:
  - ip: 192.168.x.x
    name: Living Room Lamp
```

The page logs in with `TAPO_USERNAME` and `TAPO_PASSWORD`, and shows the name, model, and on/off control. Brightness is shown when the device reports it. An address that does not respond is shown as offline.

### Linux or Raspberry Pi

Create `extra_devices.yaml` in the project directory. It is git-ignored, so a later `git pull` will not replace it or add it to a new clone.

```yaml
extra_devices:
  - ip: 192.168.x.x
    name: Living Room Lamp
```

This repository includes `devices.yaml.example`. Copy that file and edit the addresses:

```bash
cp devices.yaml.example extra_devices.yaml
```

`extra_devices.yaml.example` may also be present. When it is, you can copy that instead:

```bash
cp extra_devices.yaml.example extra_devices.yaml
```

### Windows

Create `extra_devices.yaml` in the project directory. It is git-ignored, so a later `git pull` will not replace it or add it to a new clone.

```yaml
extra_devices:
  - ip: 192.168.x.x
    name: Living Room Lamp
```

This repository includes `devices.yaml.example`. Copy that file and edit the addresses:

```powershell
Copy-Item devices.yaml.example extra_devices.yaml
```

`extra_devices.yaml.example` may also be present. When it is, you can copy that instead:

```powershell
Copy-Item extra_devices.yaml.example extra_devices.yaml
```

`devices.txt` is separate. One IPv4 address per line. A broadcast address such as `192.168.1.255` scans that subnet. When that file is missing or only has comments, the page scans the subnet of `TAPO_HUB_IP`. When any address is listed, only those addresses are checked. Entries in `extra_devices.yaml` are added on top of that scan.

## 6. Run it and check the pages

Linux or Raspberry Pi. Use two terminals. Each one needs the three `export` lines from the manual-run steps in section 2 (`~/.bashrc` already covers a new interactive terminal):

```bash
# Terminal 1, from the project directory
source venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Terminal 2
cd ~/tapo-local/bin
./go2rtc -config go2rtc.yaml
```

Windows. Use two PowerShell windows. The dashboard window needs the three variables from section 2, either as `$env:` assignments or from the saved user environment. Activate `venv` in that window:

```powershell
# Window 1, from the project directory
.\venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Window 2
cd bin
.\go2rtc.exe -config go2rtc.yaml
```

On Linux the dashboard listens on all interfaces so other machines on the LAN can open it. On Windows the sample command listens on this PC only. Use `--host 0.0.0.0` on Windows when another device should open the dashboard.

Dashboard: `http://server_name_or_IP:8000`

go2rtc UI: `http://server_name_or_IP:1984`

Check these pages:

- `/` lists paired cameras and recordings
- `/live` lists the stream names from `bin/go2rtc.yaml`
- `/controls` lists plugs and lights from the subnet scan and from `extra_devices.yaml`

## 7. Run the dashboard as a service on Linux (optional)

Create this unit once if a Raspberry Pi or other Linux machine should start the dashboard on boot. go2rtc is still started on its own. The service does not read `~/.bashrc`. All three variables have to be `Environment=` lines in the unit, or the dashboard fails with `Set TAPO_HUB_IP to the LAN address of the H500 hub.`

Copy `tapo-dashboard.service.example` into place and edit it. Replace `pi`, the project path, and the three sample values. `TAPO_HUB_IP` is your hub's address, such as `192.168.1.10` in the example. Keep the quotes.

```bash
sudo cp tapo-dashboard.service.example /etc/systemd/system/tapo-dashboard.service
sudo nano /etc/systemd/system/tapo-dashboard.service
```

The unit looks like this:

```ini
[Unit]
Description=Tapo local dashboard
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/tapo-local
Environment="TAPO_USERNAME=your-email@example.com"
Environment="TAPO_PASSWORD=your-tapo-cloud-password"
Environment="TAPO_HUB_IP=192.168.1.10"
ExecStart=/home/pi/tapo-local/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Load it and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tapo-dashboard.service
```

If the service is already installed and an interactive shell already has `TAPO_HUB_IP`, edit the unit anyway. Add the three `Environment=` lines, then reload and restart. Saving `~/.bashrc` does not change the running service.

```bash
sudo systemctl daemon-reload
sudo systemctl restart tapo-dashboard.service
```

Confirm the service received the names. This prints the three names and not their values:

```bash
sudo systemctl show tapo-dashboard.service -p Environment --value | grep -oE 'TAPO_(USERNAME|PASSWORD|HUB_IP)'
```

The list should include `TAPO_HUB_IP`, `TAPO_PASSWORD`, and `TAPO_USERNAME`. Then check the log:

```bash
journalctl -u tapo-dashboard.service -n 30 --no-pager
```

`Set TAPO_HUB_IP` in that log means the unit still does not have the variable. `systemctl show` without the `cut` command also prints the password, so leave that output on the machine.

This unit stays on that machine. Do not commit a copy that contains the password. `git pull` does not update `/etc/systemd/system/tapo-dashboard.service`.

## 8. Updating

Linux or Raspberry Pi:

```bash
cd ~/tapo-local
git pull
sudo systemctl restart tapo-dashboard.service
```

Skip the `systemctl` line when you start uvicorn yourself, and start that process again after the pull. Restart go2rtc as well when the pull changes how previews are requested. A pull does not change the `Environment=` lines in `/etc/systemd/system/tapo-dashboard.service`. Edit that file, then `daemon-reload` and restart, when the service is missing one of the three variables.

Windows, from the project directory: `git pull`, then stop and start the uvicorn window and `go2rtc.exe`.

When a change adds a program, a package, or a config file the app needs, update this guide in the same commit. Right now, `pip install -r requirements.txt` does not provide:

- ffmpeg, for the camera preview JPEG
- `bin/go2rtc` on Linux, or `bin/go2rtc.exe` on Windows
- `bin/go2rtc.yaml` for the camera streams

## Access

Dashboard on the Pi or Linux host: `http://<hostname-or-IP>:8000`

Dashboard on this Windows PC, with the sample command: `http://127.0.0.1:8000`

Live view links inside the dashboard point at go2rtc on port 1984 of the same host.

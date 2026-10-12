# Tapo Local Dashboard — Install & Run Guide

This guide gets the H500 recording browser, live view, and the Devices page running on a fresh Ubuntu or Raspberry Pi OS machine. The Windows PC uses the same repository. The dashboard steps that differ are called out below.

## Prerequisites

- Ubuntu 22.04+ or Raspberry Pi OS (64-bit), or Windows for the desktop copy
- Python 3.10+
- git
- Network access to the Tapo H500 hub
- Python packages from `requirements.txt`: FastAPI, uvicorn, Jinja2, python-multipart, and `tapo` 0.10 or newer
- go2rtc, installed separately. `pip` does not install it

## 1. Clone and set up the Python environment

```bash
cd ~
git clone https://github.com/GitRomeoG2020/tapo-local.git
cd tapo-local
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

On Windows, from the project directory:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 2. Credentials

Use the cloud TP-Link account that owns the hub. Set these in the shell. Add them to `~/.bashrc` on the Pi so they survive a reboot. Do not put the password in the repository.

```bash
export TAPO_USERNAME="your-email@example.com"
export TAPO_PASSWORD="your-tapo-cloud-password"
```

On Windows:

```powershell
$env:TAPO_USERNAME = "your-email@example.com"
$env:TAPO_PASSWORD = "your-tapo-cloud-password"
```

A rejected password is not retried. A locked hub refuses further logins for a while.

## 3. go2rtc (live view)

Live view needs two files that are not in git:

- `bin/go2rtc` on Linux, or `bin/go2rtc.exe` on Windows. Download the binary from the go2rtc releases.
- `bin/go2rtc.yaml`. The dashboard reads stream names from this file. It does not start go2rtc.

Leave both files in `bin/` on each machine. Do not commit them. The yaml contains the camera account secret inside the `tapo://` URLs.

Example `bin/go2rtc.yaml`. Replace the hash and each device id. One stream per camera. Battery cameras on the H500 use the hub address plus `deviceId`:

```yaml
streams:
  front_door: tapo://admin:YOUR_UPPERCASE_SHA256@192.168.11.223/?deviceId=DEVICE_ID_HERE
  front_doorbell: tapo://admin:YOUR_UPPERCASE_SHA256@192.168.11.223/?deviceId=DEVICE_ID_HERE
```

Generate the SHA256 from the cloud password:

```bash
echo -n "your-tapo-cloud-password" | sha256sum | awk '{print toupper($1)}'
```

On Windows:

```powershell
$hash = [System.BitConverter]::ToString(
  [System.Security.Cryptography.SHA256]::Create().ComputeHash(
    [System.Text.Encoding]::UTF8.GetBytes("your-tapo-cloud-password")
  )
).Replace("-", "")
$hash
```

The Live View page lists each name under `streams` and links to `http://<host>:1984/stream.html?src=<name>`. It also links to the full go2rtc UI on port 1984. Stream URL values stay in the yaml.

Live View has a Sync cameras to go2rtc button. It uses the same hub camera list as the Cameras page and appends any camera whose device id is not already in `bin/go2rtc.yaml`. Existing stream lines are left as they are. Each new line looks like `tapo://admin:UPPERCASE_SHA256@<hub-ip>/?deviceId=<id>`. The hash is the uppercase SHA256 of `TAPO_PASSWORD`. After the page names what it added, restart go2rtc so the new streams load: `sudo systemctl restart go2rtc`. On Windows, stop `go2rtc.exe` and start it again.

## 4. Extra devices (optional)

Plugs and lights on another subnet or VLAN go in `extra_devices.yaml` in the project root. The file is optional. When it is missing, or it only has comments, the Devices page stays on the subnet scan.

```yaml
extra_devices:
  - ip: 192.168.0.24
    name: Music Room Bulb
  - ip: 192.168.0.55
    name: Smart Plug
    model: P110
```

`model` is optional (`L530`, `P110`, and similar). The page logs in with `TAPO_USERNAME` and `TAPO_PASSWORD`, and shows the name, model, and on/off control. Brightness is shown when the device reports it. An address that does not respond is shown as offline.

`devices.txt` is separate. One IPv4 address per line. A broadcast address such as `192.168.11.255` scans that subnet. When that file is missing or only has comments, the page scans the H500 subnet. When any address is listed, only those addresses are checked. Entries in `extra_devices.yaml` are added on top of that scan.

## 5. Run manually (testing)

```bash
# Terminal 1
source venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Terminal 2
cd ~/tapo-local/bin
./go2rtc -config go2rtc.yaml
```

On Windows, bind the dashboard to this computer and start `go2rtc.exe` from `bin`:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Dashboard: `http://server_name_or_IP:8000`

go2rtc UI: `http://server_name_or_IP:1984`

Check these pages:

- `/` lists paired cameras and recordings
- `/live` lists the stream names from `bin/go2rtc.yaml`
- `/controls` lists plugs and lights from the subnet scan and from `extra_devices.yaml`

## 6. Run the dashboard as a service (optional)

The update step restarts `tapo-dashboard.service`. Create that unit once if the Pi should start the dashboard on boot. go2rtc is still started on its own. Replace `pi` with the account that owns the clone.

```ini
[Unit]
Description=Tapo local dashboard
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/tapo-local
Environment=TAPO_USERNAME=your-email@example.com
Environment=TAPO_PASSWORD=your-tapo-cloud-password
ExecStart=/home/pi/tapo-local/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Save it as `/etc/systemd/system/tapo-dashboard.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tapo-dashboard.service
```

This unit stays on the Pi. Do not commit a copy that contains the password.

## 7. Updating

```bash
cd ~/tapo-local
git pull
sudo systemctl restart tapo-dashboard.service
```

When a change adds a program, a package, or a config file the app needs, update this guide in the same commit. Right now, `pip install -r requirements.txt` does not provide:

- `bin/go2rtc` on Linux, or `bin/go2rtc.exe` on Windows
- `bin/go2rtc.yaml` for the camera streams

## Access

Main dashboard: `http://<pi-ip-or-hostname>:8000`

Live view links inside the dashboard point at go2rtc on port 1984.

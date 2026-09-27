# Middle server requirements

The ERPNext server cannot reach the ZK devices. The **middle server** can reach
both: the devices on the office network, and ERPNext on the internet. It runs the
ZK Device Manager **agent** (`agent/`), which asks ERPNext for work, does it on
the devices, and reports back.

```
 Devices (192.168.67.x)  <── TCP 4370 ──  Middle server  ── HTTPS 443 ──>  ERPNext (momc-erp.sy)
                                          (the agent)
```

The middle server only opens connections **outwards**. Nothing needs to connect
to it.

---

## 1. Operating system

| | Users & fingerprints | Faces & photos |
|---|---|---|
| **Windows 10/11 or Windows Server 2016+, 64-bit** | Yes | Yes |
| Linux | Yes | **No** |

Faces and user photos go through ZKTeco's Standalone SDK (`zkemkeeper.dll`),
which exists for Windows only. On these devices people punch in **by face**, so
**use Windows**. Linux would manage users but could not copy the faces that
let them punch in.

## 2. Hardware

Small is enough:

- 2 CPU cores, 4 GB RAM
- 10 GB free disk for logs, working photos, and face-template backups
- Always on: sleep and hibernate disabled, starts again by itself after a power cut

For scale: a full sync of the 7 devices reads every user on every device one at
a time, which takes about 20–40 minutes.

## 3. Network

| From the middle server to | Port | Why |
|---|---|---|
| Every device: 192.168.67.11, .12, .13, .14, .15, .16, .22 | **TCP 4370** (and UDP 4370) | Reading and writing users, faces, photos |
| `momc-erp.sy` | **TCP 443** (HTTPS) | Taking jobs and reporting results |

Also:

- **No inbound ports.** The firewall can block all inbound connections.
- **No VPN that blocks the local network.** Proton VPN, VPN Master and similar
  "kill switches" silently cut off the devices: they stop answering even ping.
  Leave any VPN off on this machine, or make sure it allows LAN traffic.
- **DNS** must resolve `momc-erp.sy`.
- **Correct time.** Keep it synced (Windows Time / NTP): HTTPS fails when
  the clock is off, and job times are recorded from it.
- **A proxy, if the network uses one:** set `HTTPS_PROXY` for the account the
  agent runs as.
- **A fixed address for each device:** reserve them in DHCP or set them on
  the device. If a device's IP changes, update it under ZK Device in ERPNext.

## 4. Software

1. **Python 3.10 or newer, 64-bit**, from python.org. Tick "Add to PATH".
2. **ZKTeco Standalone SDK 6.3.1.37, 64-bit build.** From the SDK zip's
   `SDK-Ver6.3.1.37\64bit` folder, right-click `Register_SDK_x64.bat` and choose
   **Run as administrator**. It copies the DLLs into `C:\Windows\System32` and
   registers `zkemkeeper.dll`.
   - The SDK and Python must be the **same bitness** (both 64-bit). A mismatch
     shows as `Invalid class string`.
   - It is **not** the "ZKFinger SDK" or "ZKBioModule SDK"; those are for USB
     fingerprint readers.
3. **The agent**: copy the app's `agent/` folder to, for example, `C:\zk_agent\`, then:
   ```
   cd C:\zk_agent
   pip install -r requirements.txt
   ```
   This installs `pyzk`, `requests` and `pywin32`.

## 5. ERPNext account and permissions

1. Create one ERPNext user for the agent, for example the existing
   `fingerprint@momc.gov.sy`.
2. Give it the role **ZK Agent**, and nothing an ordinary person would need.
3. **Also give ZK Agent create/write/read on "ZK Sync Log"** in *Role Permission
   Manager*. That doctype belongs to another app, so this app cannot grant it
   itself, and the full sync writes its log there.
4. On that user: *Settings → API Access → Generate Keys*. Copy the key and secret.

The key can add and remove people from every door. It goes **only** into the
middle server's `config.ini`. It never goes in email, chat, or any repository.

## 6. Configuration

In the agent folder, copy `config.example.ini` to `config.ini` and fill it in:

```ini
[erpnext]
url = https://momc-erp.sy
api_key = <from step 5>
api_secret = <from step 5>

[agent]
name = middle-server
bio_workers = 4
sync_timeout_minutes = 240
```

Limit who can read `config.ini`: only the account that runs the agent, and
administrators.

## 7. Running it all the time

Use Task Scheduler, with a dedicated local account (it does not need to be an
administrator once the SDK is registered):

- **Trigger:** At startup
- **Action:** `C:\zk_agent\run_agent.bat`, *Start in* `C:\zk_agent`
- **Run whether the user is logged on or not**
- **Settings:** if the task fails, restart every 1 minute; do not stop it after
  any time limit

`run_agent.bat` restarts the agent 30 seconds after any crash. Only one agent
may run on a machine; a second copy exits on its own.

## 8. Other programs on the same server

The office PC currently also runs `get_fingerprint_data.py` (pulling attendance
into ERPNext). It can move to this server and keep running. But anything that
talks to the devices disables them for a few seconds while it reads or writes,
so:

- schedule attendance pulls and the full sync (*ZK Settings → Every (minutes)*)
  so they do not overlap;
- run **no other tool** that manages device users (ZKAccess, ZKTime, BioTime),
  or the two will undo each other's changes.

## 9. Data kept on the server

`C:\zk_agent\logs\` holds:

| Folder | Contents |
|---|---|
| `agent.log` | What the agent did, rotated at 5 MB × 10 files |
| `faces\` | A backup of every face template copied or removed (**biometric data**) |
| `photos_work\`, `photos\` | Staff photos, working copies |
| `sync_<job>.log` | The output of each full sync |

Face templates and photos are personal biometric data. Keep this folder off
shared drives, include it in the server's own backup, and decide how long
`faces\removed\` is kept.

## 10. Checking that it is ready

Run these on the middle server, in order. Each must pass before the next.

| Check | Command | Expected |
|---|---|---|
| Device reachable | `Test-NetConnection 192.168.67.11 -Port 4370` | `TcpTestSucceeded : True`, for all 7 |
| ERPNext reachable | `curl.exe -H "Authorization: token KEY:SECRET" https://momc-erp.sy/api/method/frappe.auth.get_logged_user` | The agent user's email |
| SDK registered | `python -c "import win32com.client; win32com.client.Dispatch('zkemkeeper.ZKEM.1'); print('ok')"` | `ok` |
| Agent end to end | `python zk_agent.py --refresh` | Every device reported; in ERPNext, ZK Device shows **Online** and ZK User lists all 478 users |

Then start the scheduled task. In ERPNext, **ZK Settings → Agent Last Seen**
should update every 30 seconds, and *Faces/Photos Available* should be ticked.

## 11. When something goes wrong

| Symptom in ERPNext | Look at |
|---|---|
| Banner: "the agent is not running" | Is the server on? Is the task running? `logs\agent.log` |
| Device shows **Offline** | The device's power and network; a VPN on the server; the IP under ZK Device |
| Job **Failed**: "HTTP 403" | The agent user's roles (ZK Agent, and the ZK Sync Log permission) |
| *Faces/Photos Available* unticked | The SDK is not registered for 64-bit Python (section 4.2) |
| Job stuck in **Running** | The agent stopped mid-job. After 4 hours ERPNext marks it Failed; run it again |

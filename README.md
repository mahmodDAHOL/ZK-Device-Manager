# ZK Device Manager

Manage the people on ZKTeco attendance devices from ERPNext: who is on which
device, with a face, fingerprints and photo on each, and put people on, change
them, copy their face to other devices, or take them off.

## Two halves

The devices sit on the office network (192.168.67.x, no gateway). The ERPNext
server cannot reach them, so the app is split:

| Where | What | Does |
|---|---|---|
| ERPNext (this app) | ZK Device, ZK User, ZK Device User, ZK Job, ZK Settings | Holds the records, queues work, shows results |
| An office PC (`agent/`) | `zk_agent.py` | Asks ERPNext for queued jobs every 30 s, does them on the devices, reports back |

The agent only ever connects *out* to ERPNext; nothing connects in to it.
It must be the Windows PC with the ZKTeco Standalone SDK registered: faces and
photos go through the SDK, which exists for Windows only. Without it the agent
still manages users and fingerprints.

## What HR gets

- **ZK Device** — each device with its status, model, firmware and counts
  (users, faces, fingerprints, photos, capacity). *Refresh State*,
  *Sync All Devices*, *Users on Device*.
- **ZK User** — one per person, by the ID they punch in with, linked to their
  Employee (whose *Attendance Device ID* is kept equal to it). Shows how many
  devices they are on and have a face on. Buttons under *Devices*:
  - *Add to Devices* — puts them on the chosen devices, then copies their face,
    photo and fingerprints there from any device that has them.
  - *Copy Face / Photo* — copies only what each chosen device is missing.
  - *Update on Devices* — writes the saved name, privilege and card.
  - *Remove from Devices* — confirmed twice; their face is saved on the agent's
    PC first (`agent/logs/faces/removed/`).
- **ZK Device User** — what each device holds for each person, as last read.
- **ZK Job** — every request and its outcome. Queued → Running → Done/Failed.
  A queued job can be cancelled. The form refreshes itself until it finishes.
- **Employee → Actions → Device User** — opens their ZK User, or makes one.
- **ZK Settings** — the scheduled sync (off by default), and whether the agent
  is alive.

Nothing is ever replaced on a device by a copy: only what it lacks is written.
Every job can be run as a **dry run** first.

## Full sync and ZK Sync Log

*Sync All Devices* runs `agent/zk_union_sync.py` — the same script as before,
unchanged except that its ERPNext key comes from the agent — so it still writes
its **ZK Sync Log** record. The job links to it. With *Refresh Device State
After Sync* on, the agent then re-reads every device so ZK Device User is
current.

## Installing

On the bench:

```sh
bench get-app zk_device_manager /path/to/erpnext_mobile/server/zk_device_manager
bench --site momc-erp.sy install-app zk_device_manager
bench --site momc-erp.sy migrate
```

This creates the roles **ZK Manager** (give it to HR) and **ZK Agent**.

In ERPNext:

1. Make a user for the agent (e.g. the existing `fingerprint@momc.gov.sy`),
   give it **ZK Agent**, and generate its API key and secret.
2. Add each device under **ZK Device** (name, IP, port 4370).

On the office PC:

1. Install Python and the ZKTeco Standalone SDK (`Register_SDK_x64.bat` as
   administrator, same bitness as Python).
2. `pip install -r agent/requirements.txt`
3. Copy `agent/config.example.ini` to `agent/config.ini` and fill in the URL,
   key and secret.
4. `python agent/zk_agent.py --refresh` once: every device and user appears in
   ERPNext, and every person already on a device gets a ZK User, linked to the
   Employee with the same Attendance Device ID.
5. Start `agent/run_agent.bat` at logon (or from Task Scheduler).

Any VPN on that PC must allow local network traffic, or the devices are
unreachable.

## Known limits

- Faces and photos: Windows agent only.
- A face or fingerprint has to be enrolled on one device first; the app copies,
  it cannot enrol.
- Fingerprint copy uses pyzk's template write and has not yet been tried on
  these devices (they hold almost no fingerprints).

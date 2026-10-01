"""EMERGENCY: re-enable every device so people can punch in again.

A device that was disabled for a read and never re-enabled (a crashed sync or
agent) refuses every check-in until it is enabled or restarted. This only
sends "enable" and reads a few counts — it changes nothing else.

    python enable_devices.py                      # the 7 devices
    python enable_devices.py 192.168.67.22 ...    # just these
"""

import sys

from zk import ZK

DEVICES = [
    "192.168.67.22",
    "192.168.67.11",
    "192.168.67.12",
    "192.168.67.13",
    "192.168.67.14",
    "192.168.67.15",
    "192.168.67.16",
]

for ip in DEVICES:
    conn = None
    try:
        conn = ZK(ip, port=4370, timeout=10, password=0, ommit_ping=True).connect()
        conn.enable_device()
        conn.read_sizes()
        print(
            f"{ip}: ENABLED   users={conn.users}  faces={conn.faces}  "
            f"records={conn.records}  time={conn.get_time()}"
        )
    except Exception as e:
        print(f"{ip}: FAILED    {e}")
    finally:
        if conn:
            try:
                conn.disconnect()
            except Exception:
                pass

from zk_ops import SdkConnection
from zk import ZK


def count_faces(ip):
    conn = ZK(ip, port=4370, timeout=15).connect()
    try:
        users = conn.get_users()
    finally:
        conn.disconnect()

    with SdkConnection(ip, 4370) as sdk:
        faces = 0
        for u in users:
            if sdk.read_face(str(u.user_id)):
                faces += 1
    return len(users), faces


for ip in ["192.168.67.22", "192.168.67.11", "192.168.67.12"]:
    total, faces = count_faces(ip)
    print(f"{ip}: {total} users, {faces} faces")

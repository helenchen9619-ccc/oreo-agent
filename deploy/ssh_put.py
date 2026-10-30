# 部署期临时工具：SSH 裸 channel 管道上传（服务器 SFTP 坏 + exec_command 的 stdin file 对象有问题）
# 用法: EA_SSH_HOST=<主机> EA_SSH_USER=<用户> EA_SSH_PASS=<密码> python ssh_put.py <本地文件> <远程路径>
import os
import sys
import time

import paramiko

host = os.environ.get("EA_SSH_HOST", "")
if not host:
    sys.exit("缺少环境变量 EA_SSH_HOST（目标主机）")
user = os.environ.get("EA_SSH_USER", "root")

local, remote = sys.argv[1], sys.argv[2]
size = os.path.getsize(local)

for attempt in range(1, 6):
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=user, password=os.environ["EA_SSH_PASS"], timeout=30, banner_timeout=60)
        chan = ssh.get_transport().open_session()
        chan.exec_command(f"cat > {remote} && echo UPLOAD_OK")
        sent = 0
        with open(local, "rb") as f:
            while chunk := f.read(65536):
                chan.sendall(chunk)
                sent += len(chunk)
                if sent % (20 * 1024 * 1024) < 1024 * 1024:
                    print(f"{sent // 1048576}/{size // 1048576} MB", flush=True)
        chan.shutdown_write()
        out = b""
        while True:
            if chan.recv_ready():
                out += chan.recv(65536)
            if chan.exit_status_ready() and not chan.recv_ready():
                break
        print(out.decode())
        sys.exit(chan.recv_exit_status())
    except OSError as e:
        print(f"attempt {attempt} failed: {e}", flush=True)
        time.sleep(15)
print("all attempts failed")
sys.exit(1)

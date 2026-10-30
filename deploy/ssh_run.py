# 部署期临时工具：密码 SSH 执行远程命令（Windows 无 sshpass，用 paramiko）
# 用法: EA_SSH_HOST=<主机> EA_SSH_USER=<用户> EA_SSH_PASS=<密码> python ssh_run.py "远程命令"
import os
import sys

import paramiko

host = os.environ.get("EA_SSH_HOST", "")
if not host:
    sys.exit("缺少环境变量 EA_SSH_HOST（目标主机）")
user = os.environ.get("EA_SSH_USER", "root")

cmd = sys.argv[1]
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username=user, password=os.environ["EA_SSH_PASS"], timeout=15)
_, out, err = ssh.exec_command(cmd, timeout=300)
code = out.channel.recv_exit_status()
sys.stdout.write(out.read().decode("utf-8", "replace"))
sys.stderr.write(err.read().decode("utf-8", "replace"))
sys.exit(code)

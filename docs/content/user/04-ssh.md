<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# SSH, SFTP and remote tools

You can connect to your **running** desktop with `ssh`, `sftp`, WinSCP or VS Code Remote-SSH.

## Set up

1. On the dashboard, switch on **SSH and SFTP access** in the Software section and press
   **Apply**.
2. Create a key on your own computer if you do not have one. In Windows PowerShell:
   `ssh-keygen -t ed25519` (press Enter for the questions).
3. Open the file `C:\Users\<you>\.ssh\id_ed25519.pub` in Notepad, copy the single line, and paste it
   under **SSH keys** on your dashboard. Never share the file *without* `.pub`.
4. Stop and start your desktop.

## Connect

```
{ssh_cmd}
```

- **WinSCP**: protocol SFTP, host `{host}`, port `2222`, user `{user}`, and your private key under
  *Advanced → SSH → Authentication*.
- **VS Code**: install *Remote - SSH* and add this to your SSH config:

```
Host orbitalwindow
    HostName {host}
    Port 2222
    User {user}
```

You are logged in as `abc`, the desktop's own user, in your home folder. Connections only work
while your desktop is running.

<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Files

Your files are the same everywhere: in your desktop, in VS Code in the browser, in the web file
manager and in Windows Explorer. In VS Code your home folder is called `/home` (see
[VS Code](09-vscode.md)).

| Folder | In the desktop | In Windows Explorer |
|---|---|---|
| Your home folder | your home | `{samba_path}` |
| Group folders | `/groups/<name>` | `\\{host}\<name>` |
| Shared scratch | `/scratch` | `\\{host}\scratch` |
| Shared data (read only) | `/shared` | – |

Your groups: {groups}

## Web file manager

Press **Open files** on the dashboard to upload, download, preview and zip files in your browser. It
works even when your desktop is stopped.

## Windows Explorer

1. Open Windows Explorer and type `{samba_path}` into the address bar.
2. When Windows asks, enter your OrbitalWindow user name **{user}** and password.
3. To keep it, right-click **This PC** → **Map network drive…** and use the same address.

## Group folders

Your administrator can create a folder for a project or lab group. Everyone in the group can open,
edit and delete the files in it, so keep private work in your home folder. If you were just added to a group, stop and start your desktop to see it.

## Scratch

`/scratch` is shared with everyone on the server: handy for passing large files around. Everyone can
read the files, and normally only the owner can delete them. It is **not private**: anyone who uses
`sudo` in their desktop can read, change or delete any file there. Files that are not changed for a
while are deleted automatically, so do not keep anything important there.

## Disk space

The dashboard shows how much space your home folder uses. When it is nearly full, empty the Trash
and remove `~/.conda/pkgs` and `~/proot-apps` if you do not need them.

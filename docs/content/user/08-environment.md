<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Environment variables

Some programs read settings from *environment variables*, for example where to keep downloaded
models (`HF_HOME`), a licence server, or an API key. You can set your own variables once, and
OrbitalWindow sets them in every desktop you start.

## Add, change or remove one

1. On the dashboard, under **My desktop**, click **Environment variables**.
2. Type a **Name** and a **Value** and press **Add**.
3. To change one, press the pencil next to it. To remove one, press the bin.
4. Stop and start your desktop. Changes apply at the next start; the page reminds you while a
   restart is needed.

## Good to know

- Your variables stay when you switch to another desktop.
- They are set in the desktop's programs, in the [terminal](07-terminal.md) and in
  [SSH](04-ssh.md) logins.
- Names use letters, digits and `_`, and may not start with a digit. A few names, such as `PATH`,
  `HOME` and names starting with `OW_`, are used by OrbitalWindow itself and can't be set here.
  To add a folder to `PATH`, add a line such as `export PATH="$HOME/bin:$PATH"` to the file
  `~/.bashrc` in your home folder.
- Values are shown on this page to you only. Administrators can't see them, but anyone who uses
  your desktop can: treat an API key here like a password.

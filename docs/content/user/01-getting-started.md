<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Getting started

OrbitalWindow gives you your own Linux desktop on a powerful server, in your web browser. Nothing
needs to be installed on your computer.

## Log in

1. Open **https://{host}/** in Chrome, Edge or Firefox.
2. Log in with your user name **{user}** and the password your administrator gave you.
3. Change that password straight away: on your dashboard, open **Change my password**.

## Start and open your desktop

1. On your dashboard, press **Start**. The status changes to *Starting…* and then *Running*. This
   takes about half a minute.
2. Press **Open desktop**. Your desktop opens in a new browser tab. You are not asked for a
   password again. After stopping, restarting or switching your desktop, press **Open desktop**
   again: it reloads that tab.
3. When you have finished for the day, save your work and press **Stop** on the dashboard. Your files
   are kept; only the running desktop is switched off, which frees the server for others.

You can close the browser tab at any time; your desktop keeps running until you press **Stop**.

## Good to know

- Everything you keep in your home folder stays when the desktop is stopped or restarted.
  Programs you install yourself with `sudo apt install` are **lost** at the next start. Use the
  **Software** section of the dashboard instead (see [Software](02-software.md)).
- The graphics card (GPU) is shared by everyone who is working. If a program reports that the GPU is
  out of memory, try again a little later or close GPU programs you no longer need.
- Forgotten your password? Ask your administrator to reset it.

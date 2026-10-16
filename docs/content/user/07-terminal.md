<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Terminal

The terminal gives you a command line in your **running** desktop, right in your web browser.
You don't need SSH or any software on your own computer.

## Open it

On the dashboard, press **Open terminal**. It opens in a new tab, in your home folder, as the
desktop's own user `abc` (the same user as in the desktop and over SSH).

## It keeps running

Closing the terminal's tab does **not** stop what runs in it. Start a long calculation, close the
tab or your laptop, and press **Open terminal** later to see how far it has got. The dashboard
shows *Your terminal is running* while it does.

The terminal stops when you:

- press **Stop terminal** (on the terminal page or the dashboard),
- type `exit`, or
- stop your desktop. The dashboard asks first, because programs in the terminal stop too.

Only one terminal runs at a time. Opening it in two tabs shows the same terminal in both.

## Scrolling and copying

- Scroll back with the mouse wheel.
- To select text, hold **Shift** while you drag, then copy with **Ctrl+Shift+C** (or the right-click
  menu). Paste with **Ctrl+Shift+V**.

The terminal runs [tmux](https://github.com/tmux/tmux/wiki). If you know tmux you can use its
keys (Ctrl+B) as usual, and settings in `~/.tmux.conf` are used.

## If the page says the terminal can't keep running

Your desktop was made before this feature existed: closing the page stops what runs in it. Ask your
administrator to rebuild the desktop.

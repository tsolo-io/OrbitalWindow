<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# VS Code

VS Code is a program to edit code and text files. You can use it in your browser without starting
your desktop.

There are two ways to use VS Code. This page is about **VS Code in the browser**, started from the
dashboard. You can also switch on **Visual Studio Code** under *Software* to get it as a program
inside your desktop, where it can run all of your desktop's software.

## Start and open it

1. On the dashboard, find **VS Code** and press **Start VS Code**. The first start takes a minute
   or two.
2. When the state is **running**, press **Open VS Code**. It opens in a new browser tab.
3. When you are done, press **Stop VS Code**. VS Code saves its settings in your home folder, so they
   are kept for the next start.

VS Code keeps running when you close the tab, and also when the server restarts, until you stop it.

## Your files

VS Code opens your home folder. It is the same folder as in your desktop, but here it is called
`/home`:

| Folder | In VS Code | In the desktop |
|---|---|---|
| Your home folder | `/home` | `/config` |
| Group folders | `/groups/<name>` | `/groups/<name>` |
| Shared scratch | `/scratch` | `/scratch` |
| Shared data (read only) | `/shared` | `/shared` |

Changes you save are at once in your desktop, in the web file manager and in Windows Explorer.

## Terminal and programs

**Terminal → New Terminal** opens a terminal. It has Python 3 and Git, but not the software of your
desktop (for example MATLAB or the conda environments). To run those, use your desktop or its
terminal ([Terminal](07-terminal.md)).

You can add extensions (for example *Python*) under **Extensions**. They come from the Open VSX
registry, not from Microsoft's marketplace, so some extensions are missing.

Your own environment variables ([Environment variables](08-environment.md)) are set in the VS Code
terminal too. Changes apply the next time you start VS Code.

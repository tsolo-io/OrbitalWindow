<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0022 Electron apps run without Chromium's sandbox

- Status: accepted
- Date: 2026-10-10

## Context
Ticket #11: RStudio and Visual Studio Code abort at start on the GPU server:

```
Failed to move to new namespace: PID namespaces supported, Network namespace supported,
but failed: errno = Operation not permitted
FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc:207] Check failed: . : Invalid argument (22)
```

Both are Electron (Chromium) apps. Chromium's sandbox puts each renderer in its own PID and network
namespace, either through the setuid `chrome-sandbox` helper or unprivileged user namespaces. Desktops
run with Docker's default seccomp profile and capability set (minus `NET_RAW`), which has no
`CAP_SYS_ADMIN`, so neither way can create the namespaces. The POC's fix (make `chrome-sandbox`
setuid root, see `poc-learnings.md`) only gets past the earlier "SUID sandbox helper not configured"
check; the helper then fails as above.

## Decision
- Electron apps are started with **`--no-sandbox`**: the add-on's own launcher, the menu entries the
  package installs (patched with `sed` in `install.sh`), and a wrapper in `/usr/local/bin` (ahead of
  `/usr/bin` on `PATH`) for starting them from a terminal. The setuid `chrome-sandbox` step is removed.
- Qt WebEngine (Spyder's help pane) gets the same treatment through `QTWEBENGINE_DISABLE_SANDBOX=1`
  in the `python-science` add-on's environment.

## Consequences
- The desktop container stays the security boundary, as for every other program in it. Chromium's
  sandbox mainly protects against hostile web content; these apps show the user's own files and
  trusted pages, and VS Code's extensions run unsandboxed anyway.
- Not chosen: `CAP_SYS_ADMIN` or `seccomp=unconfined` on desktops. That would weaken the container for
  every program to protect a few renderer processes.
- New Electron-based add-ons need the same flag. MATLAB (which embeds CEF) is checked when a real
  install is available.

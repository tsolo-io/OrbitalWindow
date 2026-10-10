<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Software

Your desktop comes with the basics: a web browser, a terminal, Python 3 and Git. Add more in the
**Software** section of your dashboard.

## Switch on optional software

The switches turn on bundles such as MATLAB, Python science (Jupyter, Spyder), R and RStudio,
Visual Studio Code and developer tools. Some bundles, like MATLAB, need permission from your
administrator first; they are greyed out until then.

## Add Ubuntu packages

Type the name of an Ubuntu package (for example `octave` or `gnuplot`) and press **Add**. Matching
names are suggested while you type.

## Apply

Nothing is installed until you press **Apply**. Your software is then prepared in the background;
**Show log** shows the progress. This can take a few minutes, longer for big programs. When it is
done, **stop and start your desktop** to get the new software.

## MATLAB

MATLAB is installed once on the server and shared. The first time you start it, sign in with your
MathWorks account to activate your own licence.

## Python and conda

With *Python science* switched on, every terminal starts in the shared `science` environment.
Create your own environments with `conda create -n myenv python numpy`; they are stored in your home
folder and appear in JupyterLab.

---
description: >-
  The app module consists of a tray (status bar icon from PySide6) and app
  (nicegui native application)
---

# 📦 app (module)

## Background

### Motivation

* A user-interface is necessary for users to feel comfortable using OpenAdapt. The CLI is too intimidating.
* The tray provides notifications, to let the user know the state of the application

### Literature review

* [https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/index.html](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/index.html) (tray)
* [https://nicegui.io/documentation](https://nicegui.io/documentation) (app)

## Goals

* The tray should make interacting with OpenAdapt easier
* The application should never fail on its own
* The tray should always reflect the state of the DB (always up-to-date)
* The app and tray should be responsive and quick.

## Design

### System Overview

![](https://lh6.googleusercontent.com/PS9Nop16PPsB3Y-SVbwtGh\_LNWqkZtjoBzo5mJTLcnLF5-q7ZpY4nj8S7EdM6-3x05agZZ1gzRtmNj7SOdwKaDmWUq3n8z0fzYO99rYB1DCgkGQ12LTy\_tC3p7KNfTn\_ggYMuazJ24i6g4nlPMT6xVM)![](https://lh3.googleusercontent.com/mUQHXTx24RUFhBhojitWm3PYCkdXDHh9\_aZRqU-OmWorcCrvKE3k0piQV85g-2PNMiqUiHH8wxNU3IC3-RZWoOlBGoX4\_GHD7Ol9fOB7sz\_VLGDnGu4MzAT-3AZv0dUQRb0408F1iZRw0vRRLFmoYYQ)

## Analysis

The app window could look better and may be hard to see.

If an error occurs, the tray might get blocked, so the process may have to be killed.

### Performance

System:

* M1 2020 MacBook Pro
  * Memory: 16 GB
  * macOS: 13.4.1

### Benchmarks:

#### App Startup &#x20;

approximately how long it takes for the app to fully display:

*   python -m openadapt.app.main

    7.22s user 2.26s system 203% cpu 4.665 total&#x20;

#### Tray Startup&#x20;

*   python -m openadapt.app.tray:

    4.58s user 1.01s system 225% cpu 2.476 total
* poetry run app
  * 4.18s user 2.12s system 221% cpu 2.845 total


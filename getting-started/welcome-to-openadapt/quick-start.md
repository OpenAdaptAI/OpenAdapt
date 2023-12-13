---
description: Working with OpenAdapt
---

# Quick Start

{% hint style="info" %}
**Experiencing difficulty? Get support on our** [**Discord**](https://discord.gg/QKPuDqhDHF)**.**
{% endhint %}

## Installation

OpenAdapt **requires Python3.10**, you can download it [here](https://www.python.org/downloads/) or try our installer, which will download Python for you.&#x20;

{% tabs %}
{% tab title="Poetry (recommended)" %}
First, clone & navigate to the repository:

```
git clone https://github.com/MLDSAI/OpenAdapt.git
cd OpenAdapt
```

If [poetry](https://python-poetry.org) is not installed, you can use pip to install it:

```
pip install poetry
```

Lastly, run these lines to setup the environment:

```
poetry install
poetry shell
alembic upgrade head
```
{% endtab %}

{% tab title="Git" %}
```batch
git clone https://github.com/MLDSAI/OpenAdapt.git
cd OpenAdapt
python3.10 -m venv .venv
source .venv/bin/activate
pip install wheel
pip install -r requirements.txt
pip install -e .
alembic upgrade head
```
{% endtab %}

{% tab title="pip" %}
```
pip install openadapt
```
{% endtab %}

{% tab title="Installer" %}
Download / Clone OpenAdapt from [here](https://github.com/MLDSAI/OpenAdapt), then run one of the following scripts:\
Windows: [install\_openadapt.ps1](https://github.com/MLDSAI/OpenAdapt/blob/main/install/install\_openadapt.ps1)\
MacOS:  [install\_openadapt.sh](https://github.com/MLDSAI/OpenAdapt/blob/main/install/install\_openadapt.sh)
{% endtab %}
{% endtabs %}

{% hint style="info" %}
**Good to know:** After installing OpenAdapt, run\
`pytest` to verify that the installation was successful.
{% endhint %}

## Make your first recording

{% hint style="info" %}
**Note:** A GUI is currently in development, see [app-module](../../reference/api-reference/openadapt-module/app-module/ "mention")
{% endhint %}

To make your first recording, try the following command:

```sh
python -m openadapt.record "testing openadapt"
```

You should see the following when recording has begun:

```bash
| INFO     | __mp_main__:performance_stats_writer:422 - performance stats writer starting
| INFO     | __mp_main__:write_events:211 - event_type='window' starting
| INFO     | __mp_main__:write_events:211 - event_type='screen' starting
| INFO     | __mp_main__:write_events:211 - event_type='action' starting
```

To stop recording, focus the terminal and send **CTRL+C (SIGINT)**.  The recording is finished saving when you see something like this:

```bash
| INFO     | __main__:record:637 - joining...
| INFO     | __mp_main__:write_events:221 - event_type='window' done
| INFO     | __mp_main__:write_events:221 - event_type='action' done
| INFO     | __main__:read_screen_events:356 - done
| INFO     | __main__:process_events:126 - done
| INFO     | __mp_main__:write_events:221 - event_type='screen' done
| INFO     | __main__:record:652 - saved recording_timestamp=1686243306.393652
| INFO     | __mp_main__:performance_stats_writer:433 - performance stats writer done
```

To check what the recording looks like, run `python -m openadapt.visualize`

Read more on [openadapt.visualize](../../reference/api-reference/openadapt-module/visualize.py.md).

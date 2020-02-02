# getBible Bot Deployment and Operation Guide

This is the detailed setup and operation instructions for the getBible Bot, a Telegram bot designed to deliver Bible verses and manage user interactions effectively.
This bot utilizes Python for its operation, interacting with users through Telegram's bot interface.

## How to use this Bot on Telegram

> These options give you access to this bot, without the need of running your own instance.
- In your browser: https://t.me/getBibleRobot
- In Telegram search for: `getBibleRobot`

## Purpose of the Bible Bot

The Bible Bot is designed to engage users by providing them with instant access to Bible verses and related content through Telegram.

It supports various commands allowing users to retrieve biblical Scriptures, and more, directly within the Telegram platform.

## Prerequisites

- A Linux system with `systemd` for service management.
- Python 3.6 or newer, with `python3-venv` for creating virtual environments.
- `git` for cloning the repository.
- A Telegram account to create and manage the bot.

## Setup Instructions

### Step 1: Cloning the Repository

Clone the repository to your preferred location:

```bash
git clone https://git.vdm.dev/getBible/robot /home/<YourUsername>/getBibleRobot
```

Navigate to the bot directory:

```bash
cd /home/<YourUsername>/getBibleRobot
```

### Step 2: Creating a Virtual Environment

Within the bot directory, create and activate a Python virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Installing Dependencies

Install required Python packages specified in [requirements.txt](https://git.vdm.dev/getBible/robot/src/branch/master/requirements.txt)

```bash
pip install -r requirements.txt
```

### Step 4: Configuring the .env File

Copy the provided `.env.template` to a new file named `.env` and populate it with your specific values:

```bash
cp .env.template .env
nano .env
```

The `.env` file will require the following configurations:

- `TELEGRAM_TOKEN`: Your Telegram bot's API token.
- `TRANSLATION`: (Optional) default: `kjv`
- `DEFAULT_VERSE`: (Optional) default: `1 John 3:16`
- `GETBIBLE_URL`: (Optional) default: `https://getBible.net/` see [Joomla Component](https://getbible.net/joomla) to host your own getBible.
- `WELCOME_MESSAGE`: (Optional) default: `Welcome to the official getBible.net telegram bot.\n\n/help for more info.`
- `HELP_MESSAGE`: (Optional) default: see [config.py](https://git.vdm.dev/getBible/robot/src/branch/master/config.py)

#### Obtaining a Telegram Bot Token

1. Message `@BotFather` on Telegram to create a new bot.
2. Follow the instructions and copy the provided API token.
3. Paste this token into your `.env` file for the `TELEGRAM_TOKEN` value.

For more detailed instructions, refer to [Telegram's official bot documentation](https://core.telegram.org/bots#creating-a-new-bot).

### Step 5: Running the Bot

Ensure your virtual environment is activated and start the bot with:

```bash
python bot.py
```

This command initiates the bot based on the script's logic and Telegram's bot API interaction.

### Step 6: Setting Up Systemd Service

To run the bot as a systemd service, create a unit file:

```bash
sudo systemctl edit --force --full getBibleRobot.service
```

Add the following configuration, adjusting paths as necessary:

```ini
[Unit]
Description=getBible Bot Service
After=network.target

[Service]
User=<YourUsername>
Group=<YourGroup>
WorkingDirectory=/home/<YourUsername>/getBibleRobot
ExecStart=/home/<YourUsername>/getBibleRobot/venv/bin/python /home/<YourUsername>/getBibleRobot/bot.py

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl enable getBibleRobot.service
sudo systemctl start getBibleRobot.service
```

## Bot Commands

The bot supports various commands for user interaction see [HELP_MESSAGE](https://git.vdm.dev/getBible/robot/src/branch/master/config.py).

```
Available commands:

  You can use a reference to get verses like:
- /bible 1 John 3:16
- /bible John 3:16-19;1 John 3:10-17
- /bible Gen 1:1-5 codex
- /bible Ps 1:1-5 aov

- /search: Search the Scriptures (soon..)
- /help: To get this help message again
```

Ensure these [commands are registered](https://core.telegram.org/bots/tutorial#executing-commands) with `@BotFather` to make them visible to your bot's users.

## Licensing Notice

This project is licensed under the GNU GPL v2.0. See the LICENSE file for more details.

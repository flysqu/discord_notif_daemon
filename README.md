# Discord Notification Daemon

A Python-based daemon that connects to the Discord Gateway to listen for messages and sends desktop notifications for "pings" eg. mentions, messages in "trusted" channels, or direct messages.

Trusted channels are configured by taking a bunch of channel ids and putting them into the trusted_channels.txt file within the .config/discord_notification_daemon folder, created at runtime (Folder and file). Channel id's can be added at runtime, you can add comments by placing a "#" at the start of the line. ALL MESSAGES SENT INTO THE TRUSTED CHANNELS WILL TRIGGER A NOTIFICATION!!! The reason this is needed is that this only connects to the discord websocket, and i dont have discord's internal api working so i cant see if the user has permission to ping use @everyone or @here.

## Features

- Connects to the Discord Gateway using WebSocket.
- Sends desktop notifications for:
  - Mentions.
  - Messages in trusted channels.
  - Direct messages (DMs) and group DMs (GDMs).
- Caches user avatars locally for notifications.

## Requirements

- Python 3.8 or higher.
- Desktop environment that supports notifications.
- Wont work on windows without small tweaks to paths in code

## Installation
pip install -r requirements.txt
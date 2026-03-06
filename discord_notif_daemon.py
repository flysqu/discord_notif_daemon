import asyncio
import websockets
import json
import aiohttp
import time
from pathlib import Path
import psutil  # type: ignore
import signal
import sys
import select
import subprocess  # For calling notify-send
import os
import pwd

# Get current username and home directory
USERNAME = pwd.getpwuid(os.getuid())[0]
HOME_DIR = os.path.expanduser('~')
CONFIG_DIR = os.path.join(HOME_DIR, '.config', 'discord_notif_daemon')

def load_env_file():
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    else:
        print("Warning: .env file not found. Set DISCORD_TOKEN environment variable manually.")

load_env_file()

# GLOBAL VARIABLES
USER_ID = None
USER_TOKEN = os.environ.get("DISCORD_TOKEN", "")
GATEWAY_URL = "wss://gateway.discord.gg/?v=9&encoding=json"
DISCORD_CLIENT_PROCESS = os.environ.get("DISCORD_CLIENT_PROCESS", "harbour-saildiscord")
NOTIFICATION_BACKEND = os.environ.get("NOTIFICATION_BACKEND", "gdbus")  # "gdbus" or "notify-send"
DISSENT_RUNNING = False

if not USER_TOKEN:
    print("Error: DISCORD_TOKEN not set. Add it to .env file.")
    sys.exit(1)

# Global variable to track shutdown
SHUTDOWN_EVENT = asyncio.Event()

async def is_discord_client_focused():
    """Returns True if the Discord client is the topmost (focused) window.

    Calls org.nemomobile.compositor.privateTopmostWindowProcessId to get the
    focused window's PID (returns 0 for home screen), then checks if that
    process is our Discord client. Falls back to False (send notifications)
    if the query fails.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            'dbus-send', '--session', '--print-reply',
            '--dest=org.nemomobile.lipstick', '/',
            'org.nemomobile.compositor.privateTopmostWindowProcessId',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1.0)
        output = stdout.decode()
        # Output: "   int32 <pid>"  (0 = home screen / nothing focused)
        for token in output.split():
            try:
                pid = int(token)
                if pid > 0:
                    p = psutil.Process(pid)
                    return DISCORD_CLIENT_PROCESS in p.name()
            except (ValueError, psutil.NoSuchProcess):
                continue
    except Exception as e:
        print(f"[focus check] D-Bus query failed: {e}")
    return False

async def get_avatar(author):
    avatar_hash = author.get("avatar")
    user_id = author["id"]

    cache_dir = Path(os.path.join(CONFIG_DIR, 'cache'))
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_file = cache_dir / f"avatar_{user_id}.png"

    if cached_file.exists():
        return str(cached_file)

    url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png"
    filename = os.path.join(CONFIG_DIR, 'cache', f"avatar_{user_id}.png")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    with open(filename, "wb") as f:
                        f.write(await resp.read())
                    return filename
    except Exception as e:
        print(f"Failed to fetch avatar: {e}")
    return None

async def is_trusted_channel(channel_id):
    try:
        trusted_channels_path = Path(os.path.join(CONFIG_DIR, 'trusted_channels.txt'))
        if not trusted_channels_path.exists():
            print("trusted_channels.txt not found.")
            trusted_channels_path.parent.mkdir(parents=True, exist_ok=True)
            with open(trusted_channels_path, "w") as file:
                file.write("# Add trusted channel IDs here, one per line.\n")

            return False

        with open(trusted_channels_path, "r") as file:
            trusted_channels = {line.strip() for line in file if not line.strip().startswith("#")}
        return str(channel_id) in trusted_channels
    except Exception as e:
        print(f"Error checking trusted channels: {e}")
        return False

async def heartbeat(ws, interval):
    check_interval = 0.5  # Check every 0.5 seconds for quicker responsiveness
    elapsed_time = 0

    while not SHUTDOWN_EVENT.is_set():
        try:
            if elapsed_time >= interval / 1000:
                await ws.send(json.dumps({"op": 1, "d": None}))
                elapsed_time = 0  # Reset elapsed time after sending heartbeat
            else:
                elapsed_time += check_interval

            await asyncio.sleep(check_interval)
        except (websockets.ConnectionClosed, OSError):
            break  # Stop heartbeating if disconnected

async def should_notify(msg, user_id):
    # Exclude messages sent by the user themselves
    if msg["author"]["id"] == user_id:
        return False

    content = msg.get("content", "")
    mentions = msg.get("mentions", [])
    channel_type = msg.get("channel_type", None)

    mentioned_you = any(user["id"] == user_id for user in mentions)
    trusted_channel = await is_trusted_channel(msg["channel_id"])
    is_dm = channel_type == 1
    is_gdm = channel_type == 3

    return mentioned_you or trusted_channel or is_dm or is_gdm

async def handle_message(msg):
    author = msg["author"]["global_name"]
    content = msg["content"]
    channel_id = msg["channel_id"]
    print(f"[{channel_id}] {author}: {content}")

    # Fetch the avatar image
    avatar_path = await get_avatar(msg["author"])

    icon = avatar_path if avatar_path else ""
    try:
        if NOTIFICATION_BACKEND == "notify-send":
            command = [
                "notify-send",
                "-a", "Discord",
                "-i", icon if icon else "dialog-information",
                author,
                content
            ]
        else:  # gdbus
            command = [
                "gdbus", "call", "--session",
                "--dest", "org.freedesktop.Notifications",
                "--object-path", "/org/freedesktop/Notifications",
                "--method", "org.freedesktop.Notifications.Notify",
                "Discord", "uint32 0", icon, author, content,
                "@as []", "@a{sv} {}", "int32 0"
            ]
        subprocess.run(command, check=True)
    except Exception as e:
        print(f"Failed to send notification: {e}")

async def listen():
    global USER_ID
    session = aiohttp.ClientSession()
    backoff = 1
    dissent_running = False

    try:
        while not SHUTDOWN_EVENT.is_set():
            try:
                async with websockets.connect(GATEWAY_URL, ssl=True, max_size=10 * 1024 * 1024) as ws:
                    print("Connected to Discord Gateway")
                    backoff = 1

                    # Receive HELLO
                    hello = json.loads(await ws.recv())
                    hb_interval = hello["d"]["heartbeat_interval"]
                    heartbeat_task = asyncio.create_task(heartbeat(ws, hb_interval))

                    # IDENTIFY
                    await ws.send(json.dumps({
                        "op": 2,
                        "d": {
                            "token": USER_TOKEN,
                            "capabilities": 4093,
                            "properties": {
                                "os": "linux",
                                "browser": "chrome",
                                "device": "",
                            },
                            "presence": {"status": "online", "since": 0, "activities": [], "afk": False},
                            "compress": False,
                            "client_state": {
                                "guild_hashes": {},
                                "highest_last_message_id": "0",
                                "read_state_version": 0,
                                "user_guild_settings_version": -1,
                            },
                        }
                    }))

                    while not SHUTDOWN_EVENT.is_set():
                        # Suppress notifications only while the Discord client is focused
                        is_focused = await is_discord_client_focused()

                        if is_focused and not dissent_running:
                            print(f"{DISCORD_CLIENT_PROCESS} is focused, suppressing notifications.")
                            dissent_running = True
                        elif not is_focused and dissent_running:
                            print(f"{DISCORD_CLIENT_PROCESS} lost focus, resuming notifications.")
                            dissent_running = False

                        # Set a timeout for ws.recv() to allow checking focus status
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                            if not dissent_running:  # Only notify if app is not focused
                                data = json.loads(msg)
                                if data["t"] == "READY":
                                    USER_ID = data["d"]["user"]["id"]
                                    print(f"Logged in as: {data['d']['user']['username']} ({USER_ID})")
                                elif data["op"] == 0 and data["t"] == "MESSAGE_CREATE":
                                    msg = data["d"]
                                    if await should_notify(msg, USER_ID):
                                        await handle_message(msg)
                        except asyncio.TimeoutError:
                            continue

            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                if SHUTDOWN_EVENT.is_set():
                    break
                print(f"Disconnected: {e}. Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

            except asyncio.CancelledError:
                print("Listener task cancelled.")
                break

            except Exception as e:
                if SHUTDOWN_EVENT.is_set():
                    break
                print(f"Unexpected error: {e}")
                await asyncio.sleep(5)

    finally:
        await session.close()
        print("Session closed.")

async def wait_for_shutdown():
    print("Press 'q' to quit.")
    while not SHUTDOWN_EVENT.is_set():
        if select.select([sys.stdin], [], [], 0.1)[0]:
            user_input = sys.stdin.read(1).strip()
            if user_input.lower() == 'q':
                print("Shutdown triggered by 'q' key.")
                SHUTDOWN_EVENT.set()
                break
        await asyncio.sleep(0.1)

def shutdown_handler(signal_received, frame):
    print("Shutdown signal received. Exiting...")
    SHUTDOWN_EVENT.set()

signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

async def main():
    await asyncio.gather(listen(), wait_for_shutdown())

asyncio.run(main())

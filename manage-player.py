#!/usr/bin/env python3
"""Opt a Sonos player in or out of the bridge on this Pi."""

import argparse
import fcntl
import hashlib
import math
import re
import shutil
import socket
import subprocess
from ipaddress import IPv4Address
from pathlib import Path

CONFIG_ROOT = Path("/etc/play3-airplay")
UUID_PATTERN = re.compile(r"RINCON_[A-F0-9]+")


def read_config(path):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, separator, value = line.partition("=")
            if not separator or key in values:
                raise ValueError(f"Invalid configuration line in {path}.")
            values[key] = value.strip()
    return values


def player_settings(path):
    values = read_config(path)
    uuid = values.get("SONOS_UUID", "").upper()
    if not UUID_PATTERN.fullmatch(uuid) or len(uuid) > 32:
        raise ValueError("SONOS_UUID must be a Sonos RINCON identifier.")
    audio_format = values.get("STREAM_FORMAT", "wav")
    if audio_format not in {"wav", "flac"}:
        raise ValueError("STREAM_FORMAT must be wav or flac.")
    volume = int(values.get("MAX_VOLUME", "70"))
    if not 1 <= volume <= 100:
        raise ValueError("MAX_VOLUME must be between 1 and 100.")
    return {
        "BRIDGE_IP": str(IPv4Address(values.get("BRIDGE_IP", ""))),
        "SONOS_IP": str(IPv4Address(values.get("SONOS_IP", ""))),
        "SONOS_UUID": uuid,
        "STREAM_FORMAT": audio_format,
        "MAX_VOLUME": str(volume),
    }


def unused_port(address, candidates, reserved):
    for port in candidates:
        if port in reserved:
            continue
        with socket.socket() as listener:
            try:
                listener.bind((address, port))
            except OSError:
                continue
            return port
    raise ValueError("No free port in the receiver's configured range.")


def receiver_config(player, name, uuid, port, advance):
    # Stable locally administered unicast identity, keyed to the physical speaker.
    digest = hashlib.sha256(uuid.encode("ascii")).hexdigest()[:10]
    fifo = f"/run/play3-airplay-{player}"
    name = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'''general = {{
    name = "{name}";
    output_backend = "pipe";
    ignore_volume_control = "yes";
    service_type = "airplay2";
    port = {port};
    airplay_device_id = 0x02{digest}L;
    nqptp_shared_memory_interface_name = "/nqptp-{uuid.lower()}";
    audio_backend_latency_offset_in_seconds = {advance:.6f};
}};
pipe = {{ name = "{fifo}/audio"; output_rate = 44100; output_format = "S16_LE"; output_channels = 2; }};
metadata = {{ enabled = "yes"; include_cover_art = "no"; pipe_name = "{fifo}/metadata"; }};
'''


def unit_names(player):
    return [f"play3-airplay-{service}@{player}.service" for service in ("receiver", "relay")]


def add_player(root, player, source, name, advance):
    directory = root / "players" / player
    if directory.exists():
        raise ValueError(f"Profile {player} already exists.")
    if not name or len(name.encode("utf-8")) > 63 or not name.isprintable() or "${" in name:
        raise ValueError("Use an AirPlay name of 1–63 bytes with printable characters.")
    if not math.isfinite(advance):
        raise ValueError("The timing offset must be finite.")
    settings = player_settings(source)
    profiles = list((root / "players").glob("*/relay.env"))
    legacy = root / "relay.env"
    if legacy.exists() and any(
        subprocess.run(
            ["systemctl", check, "--quiet", "play3-airplay-receiver.service"]
        ).returncode == 0
        for check in ("is-active", "is-enabled")
    ):
        profiles.append(legacy)
    existing = [read_config(path) for path in profiles]
    for profile in existing:
        if (profile.get("SONOS_UUID", "").upper() == settings["SONOS_UUID"]
                or profile.get("SONOS_IP") == settings["SONOS_IP"]):
            raise ValueError("This Sonos player is already configured.")
    used_airplay = {int(profile.get("AIRPLAY_PORT", 7000)) for profile in existing}
    used_stream = {int(profile.get("STREAM_PORT", 8000)) for profile in existing}
    airplay = unused_port("0.0.0.0", range(7100, 7200), used_airplay)
    stream = unused_port(settings["BRIDGE_IP"], range(8100, 8200), used_stream)
    settings.update(AIRPLAY_PORT=str(airplay), STREAM_PORT=str(stream))

    directory.mkdir()
    receiver, relay = unit_names(player)
    try:
        (directory / "relay.env").write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
        (directory / "shairport-sync.conf").write_text(
            receiver_config(player, name, settings["SONOS_UUID"], airplay, advance)
        )
        subprocess.run(["systemctl", "enable", "--now", receiver], check=True)
    except (OSError, subprocess.CalledProcessError):
        stopped = subprocess.run(["systemctl", "stop", receiver, relay]).returncode == 0
        disabled = subprocess.run(["systemctl", "disable", receiver]).returncode == 0
        if stopped and disabled:
            shutil.rmtree(directory)
        raise


def remove_player(root, player):
    directory = root / "players" / player
    if not (directory / "relay.env").is_file():
        raise ValueError(f"No configured profile named {player}.")
    receiver, relay = unit_names(player)
    subprocess.run(["systemctl", "stop", receiver, relay], check=True)
    subprocess.run(["systemctl", "disable", receiver], check=True)
    shutil.rmtree(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="operation", required=True)
    add = subcommands.add_parser("add", help="enable one selected player")
    add.add_argument("player", help="short profile ID")
    add.add_argument("config", type=Path, help="relay.env generated for the selected speaker")
    add.add_argument("--name", required=True, help="name shown in the AirPlay picker")
    add.add_argument("--offset", default=0.0, type=float, help="Shairport timing offset in seconds")
    remove = subcommands.add_parser("remove", help="stop and remove an opted-in player")
    remove.add_argument("player", help="short profile ID")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", args.player):
        parser.error("Use a profile ID starting with a lowercase letter, containing up to 32 lowercase letters, digits or hyphens.")
    try:
        (CONFIG_ROOT / "players").mkdir(exist_ok=True)
        with (CONFIG_ROOT / ".players.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.operation == "add":
                add_player(CONFIG_ROOT, args.player, args.config, args.name, args.offset)
            else:
                remove_player(CONFIG_ROOT, args.player)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()

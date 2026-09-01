#!/usr/bin/env python3
"""
Adult website blocker.

Run as administrator/root.

Windows:
  1. Open Command Prompt as Administrator
  2. py block_adult.py --install --dns cleanbrowsing

macOS/Linux:
  sudo python3 block_adult.py --install --dns cleanbrowsing

Remove only the hosts-file block:
  py block_adult.py --remove
  sudo python3 block_adult.py --remove
"""

import argparse
import ctypes
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from ipaddress import ip_address
from pathlib import Path

BLOCKLIST_URL = "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/porn/hosts"

BEGIN = "# >>> ADULT_WEBSITE_BLOCKER_START >>>"
END = "# <<< ADULT_WEBSITE_BLOCKER_END <<<"

DNS_PROFILES = {
    "cleanbrowsing": {
        "ipv4": ["185.228.168.168", "185.228.169.168"],
        "ipv6": ["2a0d:2a00:1::", "2a0d:2a00:2::"],
    },
    "cloudflare": {
        "ipv4": ["1.1.1.3", "1.0.0.3"],
        "ipv6": ["2606:4700:4700::1113", "2606:4700:4700::1003"],
    },
}

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$",
    re.IGNORECASE,
)

SKIP_DOMAINS = {
    "localhost",
    "localhost.localdomain",
    "local",
    "ip6-localhost",
    "ip6-loopback",
    "ip6-allnodes",
    "ip6-allrouters",
    "ip6-allhosts",
}


def is_admin() -> bool:
    system = platform.system()
    if system == "Windows":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def require_admin():
    if not is_admin():
        print("ERROR: Run this script as Administrator/root.")
        sys.exit(1)


def hosts_path() -> Path:
    system = platform.system()
    if system == "Windows":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return Path(root) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def write_text(path: Path, text: str):
    path.write_text(text, encoding="utf-8", newline="\n")


def backup_file(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(path.name + f".backup-{stamp}")
    shutil.copy2(path, backup)
    return backup


def strip_managed_block(text: str) -> str:
    output = []
    inside = False

    for line in text.splitlines():
        if line.strip() == BEGIN:
            inside = True
            continue
        if line.strip() == END:
            inside = False
            continue
        if not inside:
            output.append(line)

    return "\n".join(output).rstrip() + "\n"


def download_blocklist() -> str:
    print("Downloading adult-content blocklist...")
    req = urllib.request.Request(
        BLOCKLIST_URL,
        headers={"User-Agent": "adult-website-blocker/1.0"},
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        return response.read().decode("utf-8", errors="ignore")


def parse_domains(hosts_text: str) -> list[str]:
    domains = set()

    for raw_line in hosts_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        try:
            ip_address(parts[0])
        except ValueError:
            continue

        for item in parts[1:]:
            domain = item.strip().lower().rstrip(".")
            if domain in SKIP_DOMAINS:
                continue
            if DOMAIN_RE.match(domain):
                domains.add(domain)

    return sorted(domains)


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def build_hosts_block(domains: list[str]) -> str:
    lines = [
        BEGIN,
        f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "# Source: StevenBlack hosts adult-content list",
        f"# Domains blocked: {len(domains)}",
        "# Do not edit inside this block manually.",
    ]

    for group in chunked(domains, 8):
        lines.append("0.0.0.0 " + " ".join(group))

    lines.append(END)
    lines.append("")
    return "\n".join(lines)


def install_hosts_block():
    path = hosts_path()

    if not path.exists():
        print(f"ERROR: hosts file not found: {path}")
        sys.exit(1)

    original = read_text(path)
    backup = backup_file(path)

    try:
        remote_hosts = download_blocklist()
        domains = parse_domains(remote_hosts)

        if len(domains) < 1000:
            raise RuntimeError("Downloaded blocklist looked too small; refusing to install.")

        cleaned = strip_managed_block(original)
        new_text = cleaned.rstrip() + "\n\n" + build_hosts_block(domains)

        write_text(path, new_text)

        print(f"Installed hosts block with {len(domains):,} domains.")
        print(f"Backup saved at: {backup}")

    except Exception as e:
        shutil.copy2(backup, path)
        print(f"ERROR: Install failed. Restored backup.\nReason: {e}")
        sys.exit(1)


def remove_hosts_block():
    path = hosts_path()

    if not path.exists():
        print(f"ERROR: hosts file not found: {path}")
        sys.exit(1)

    original = read_text(path)
    backup = backup_file(path)
    cleaned = strip_managed_block(original)
    write_text(path, cleaned)

    print("Removed the managed adult-site hosts block.")
    print(f"Backup saved at: {backup}")


def run_command(cmd, check=False):
    try:
        result = subprocess.run(
            cmd,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"Command not found: {cmd[0]}"


def flush_dns():
    system = platform.system()

    print("Flushing DNS cache...")

    if system == "Windows":
        run_command(["ipconfig", "/flushdns"])
    elif system == "Darwin":
        run_command(["dscacheutil", "-flushcache"])
        run_command(["killall", "-HUP", "mDNSResponder"])
    elif system == "Linux":
        run_command(["resolvectl", "flush-caches"])
        run_command(["systemd-resolve", "--flush-caches"])

    print("DNS cache flush attempted.")


def set_dns_windows(ipv4):
    ps = f"""
$servers = @({",".join("'" + x + "'" for x in ipv4)})
Get-NetAdapter | Where-Object {{$_.Status -eq 'Up'}} | ForEach-Object {{
    Set-DnsClientServerAddress -InterfaceIndex $_.InterfaceIndex -ServerAddresses $servers
}}
"""
    code, out, err = run_command([
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ps,
    ])

    if code != 0:
        raise RuntimeError(err or "PowerShell DNS setup failed")


def set_dns_macos(ipv4):
    code, out, err = run_command(["networksetup", "-listallnetworkservices"])

    if code != 0:
        raise RuntimeError(err or "Could not list macOS network services")

    services = [
        line.strip()
        for line in out.splitlines()
        if line.strip()
        and not line.startswith("An asterisk")
        and not line.startswith("*")
    ]

    for service in services:
        run_command(["networksetup", "-setdnsservers", service] + ipv4)

    if not services:
        raise RuntimeError("No macOS network services found")


def set_dns_linux(profile):
    ipv4 = profile["ipv4"]
    ipv6 = profile["ipv6"]

    if shutil.which("nmcli"):
        code, out, err = run_command([
            "nmcli",
            "-t",
            "-f",
            "NAME",
            "connection",
            "show",
            "--active",
        ])

        if code == 0 and out.strip():
            names = [line.replace(r"\:", ":").strip() for line in out.splitlines() if line.strip()]

            for name in names:
                run_command(["nmcli", "connection", "modify", name, "ipv4.ignore-auto-dns", "yes"])
                run_command(["nmcli", "connection", "modify", name, "ipv4.dns", " ".join(ipv4)])
                run_command(["nmcli", "connection", "modify", name, "ipv6.ignore-auto-dns", "yes"])
                run_command(["nmcli", "connection", "modify", name, "ipv6.dns", " ".join(ipv6)])
                run_command(["nmcli", "connection", "up", name])

            return

    conf_dir = Path("/etc/systemd/resolved.conf.d")
    conf_dir.mkdir(parents=True, exist_ok=True)

    conf = conf_dir / "adult-block-dns.conf"
    conf.write_text(
        "[Resolve]\n"
        f"DNS={' '.join(ipv4 + ipv6)}\n"
        "DNSSEC=no\n",
        encoding="utf-8",
    )

    run_command(["systemctl", "restart", "systemd-resolved"])
    print("Used systemd-resolved fallback DNS configuration.")


def set_dns(profile_name: str):
    profile = DNS_PROFILES[profile_name]
    system = platform.system()

    print(f"Setting DNS profile: {profile_name}")

    if system == "Windows":
        set_dns_windows(profile["ipv4"])
    elif system == "Darwin":
        set_dns_macos(profile["ipv4"])
    elif system == "Linux":
        set_dns_linux(profile)
    else:
        raise RuntimeError(f"Unsupported OS for DNS setup: {system}")

    print("DNS setup completed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true", help="Install/update the adult-site block")
    parser.add_argument("--remove", action="store_true", help="Remove the block added by this script")
    parser.add_argument(
        "--dns",
        choices=["cleanbrowsing", "cloudflare"],
        help="Also set family-filter DNS",
    )

    args = parser.parse_args()

    if not args.install and not args.remove and not args.dns:
        parser.print_help()
        sys.exit(0)

    require_admin()

    if args.remove:
        remove_hosts_block()

    if args.install:
        install_hosts_block()

    if args.dns:
        set_dns(args.dns)

    flush_dns()

    print("\nDone.")
    print("Important: for a much stronger lock, create a separate admin account,")
    print("give its password to someone you trust, and make your daily account non-admin.")


if __name__ == "__main__":
    main()
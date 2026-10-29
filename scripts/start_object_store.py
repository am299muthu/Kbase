#!/usr/bin/env python3
"""Start only this project's private, single-node loopback Garage service."""
import os
from pathlib import Path
import shutil
import stat

ROOT = Path(__file__).resolve().parent.parent
config = ROOT / 'var' / 'garage' / 'config.toml'
if not config.is_file() or config.is_symlink() or stat.S_IMODE(config.stat().st_mode) & 0o077:
    raise SystemExit('Provision a private var/garage/config.toml (mode 0600) before starting storage.')
binary = shutil.which('garage')
if not binary:
    raise SystemExit('The authorized native Garage installation is missing.')
os.umask(0o077)
os.execv(binary, [binary, '--config', str(config), 'server', '--single-node'])

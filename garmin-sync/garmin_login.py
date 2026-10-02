#!/usr/bin/env python3
"""
One-time Garmin login. Run this on YOUR OWN computer, not on GitHub.

Garmin blocks password logins from GitHub's servers, so the sync uses a saved
session instead. This script logs in from your home/office connection, saves
the session and copies it to the clipboard, ready to paste into the GitHub
secret GARMIN_TOKENS. Your password is typed here and is not stored anywhere.

    python -m pip install -U garminconnect
    python garmin_login.py

Run it again whenever the sync reports that the Garmin session has expired.
"""
import getpass
import json
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit(
        f"This needs Python 3.12 or newer (you have {sys.version.split()[0]}).\n"
        "Install it with:  winget install Python.Python.3.12   then open a new PowerShell window."
    )

try:
    from garminconnect import Garmin
except ImportError:
    sys.exit("First run:  python -m pip install -U garminconnect")

TOKEN_DIR = Path.home() / ".garminconnect"
SECRET_URL = "https://github.com/frankholck/vtaper/settings/secrets/actions"


def copy_to_clipboard(text):
    for cmd in (["clip"], ["pbcopy"], ["xclip", "-selection", "clipboard"]):
        try:
            subprocess.run(cmd, input=text, text=True, check=True)
            return True
        except Exception:
            continue
    return False


def main():
    print("Garmin Connect login (one-time, from this computer)\n")
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (hidden while typing): ")

    g = Garmin(email, password, prompt_mfa=lambda: input("Code from Garmin (email/SMS): ").strip())
    try:
        g.login(str(TOKEN_DIR))
    except Exception as e:
        sys.exit(f"\nLogin failed: {e}\nCheck the email/password by signing in at connect.garmin.com, then run this again.")

    tokens = g.client.dumps()
    if not json.loads(tokens).get("di_refresh_token"):
        sys.exit("\nLogged in, but Garmin did not issue a reusable session. Wait 15 minutes and run this again.")

    try:
        name = g.get_full_name()
    except Exception:
        name = None
    print(f"\nLogged in{f' as {name}' if name else ''}.")

    if copy_to_clipboard(tokens):
        print("The session is now on your clipboard.")
    else:
        out = Path.cwd() / "garmin_tokens_for_github.txt"
        out.write_text(tokens)
        print(f"Could not use the clipboard. The session was written to:\n  {out}\nCopy its whole contents, then delete the file.")

    print(
        "\nNext:\n"
        f"  1. Open {SECRET_URL}\n"
        "  2. New repository secret (or edit the existing one)\n"
        "       Name:   GARMIN_TOKENS\n"
        "       Secret: paste (Ctrl+V)\n"
        "  3. Add secret, then tap 'Collect Garmin now' in the Coach app.\n"
        "\nTreat the pasted value like a password: it gives access to your Garmin account."
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Apply custom branding and preset settings to a RustDesk source tree.

Run from the root of a RustDesk checkout (the CI workflow does this right
after checkout):

    python3 custom/apply_branding.py            # patch the tree
    python3 custom/apply_branding.py --dry-run  # only validate and print config

Inputs:
    custom/branding.json        app name, server, key, preset settings
    custom/assets/icon.png      optional, square PNG (1024x1024 recommended)
    custom/assets/logo.png      optional, shown in the main window (max 300x60)
    env RD_PRESET_PASSWORD      optional permanent password (from GitHub Secrets)

How it works: RustDesk already contains the full "custom client" logic used by
RustDesk Server Pro (app name, server, hidden settings, preset password). Pro
feeds it a signed custom.txt. We patch src/common.rs so the same logic is fed
from custom_client.json embedded into the binary at compile time instead.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import string
import sys
from pathlib import Path

ROOT = Path.cwd()
CUSTOM_DIR = Path(__file__).resolve().parent
CONFIG_FILE = CUSTOM_DIR / "branding.json"
ASSETS_DIR = CUSTOM_DIR / "assets"
EMBEDDED_CONFIG = "custom_client.json"
PATCH_MARKER = "custom/apply_branding.py"

APP_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,30}$")


def fail(msg):
    print(f"[branding] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def log(msg):
    print(f"[branding] {msg}")


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def write(path, text):
    (ROOT / path).write_text(text, encoding="utf-8", newline="\n")


def replace_once(path, old, new, required=True):
    text = read(path)
    if new in text:
        return  # already applied
    if old not in text:
        if required:
            fail(f"{path}: expected snippet not found, upstream code changed:\n{old}")
        return
    write(path, text.replace(old, new, 1))


# ---------------------------------------------------------------- config

def load_config():
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        fail(f"cannot read {CONFIG_FILE}: {e}")

    app_name = cfg.get("app_name", "RustDesk")
    if not APP_NAME_RE.match(app_name):
        fail("app_name must be 2-31 ASCII letters/digits, starting with a letter "
             "(it is used for the exe/app bundle name, service name and URL scheme)")

    server = cfg.get("server", {})
    id_server = server.get("id_server", "").strip()
    key = server.get("key", "").strip()
    if not id_server:
        fail("server.id_server is required")
    if not key or key.startswith("PASTE_"):
        fail("server.key is required (content of id_ed25519.pub on your hbbs server)")
    try:
        if len(base64.b64decode(key, validate=True)) != 32:
            raise ValueError
    except Exception:
        fail("server.key does not look like an ed25519 public key (44 chars base64)")

    conn_type = cfg.get("connection_type", "both")
    if conn_type not in ("both", "incoming", "outgoing"):
        fail("connection_type must be one of: both, incoming, outgoing")

    return cfg, app_name, server, conn_type


def build_custom_client(cfg, app_name, server, conn_type, password):
    data = json.loads(json.dumps(cfg.get("custom_client", {})))
    data["app-name"] = app_name

    overrides = data.setdefault("override-settings", {})
    overrides["custom-rendezvous-server"] = server["id_server"].strip()
    overrides["key"] = server["key"].strip()
    if server.get("relay_server", "").strip():
        overrides["relay-server"] = server["relay_server"].strip()
    if server.get("api_server", "").strip():
        overrides["api-server"] = server["api_server"].strip()

    if conn_type != "both":
        data["conn-type"] = conn_type

    if password:
        # Same storage format RustDesk Server Pro uses for preset passwords:
        # "00" + base64(sha256(password + salt)); see hbb_common permanent_password.rs
        salt = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        h1 = hashlib.sha256(password.encode("utf-8") + salt.encode("utf-8")).digest()
        data["password"] = "00" + base64.b64encode(h1).decode("ascii")
        data["salt"] = salt

    for section in ("override-settings", "default-settings"):
        for k, v in data.get(section, {}).items():
            if not isinstance(v, str):
                fail(f"custom_client.{section}.{k} must be a string (e.g. \"Y\" / \"N\")")
    return data


# ---------------------------------------------------------------- patches

def patch_rust(data):
    write(EMBEDDED_CONFIG, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    path = "src/common.rs"
    # 1) load the embedded config first
    replace_once(
        path,
        "pub fn load_custom_client() {\n",
        "pub fn load_custom_client() {\n"
        f"    // Custom build: config embedded at compile time by {PATCH_MARKER}.\n"
        "    if let Ok(data) = serde_json::from_str::<std::collections::HashMap<String, serde_json::Value>>(\n"
        f"        include_str!(\"../{EMBEDDED_CONFIG}\"),\n"
        "    ) {\n"
        "        apply_custom_client(data);\n"
        "        return;\n"
        "    }\n",
    )
    # 2) split read_custom_client(): signature check stays, applying moves to apply_custom_client()
    replace_once(
        path,
        "    let Ok(mut data) =\n"
        "        serde_json::from_slice::<std::collections::HashMap<String, serde_json::Value>>(&data)\n"
        "    else {\n"
        "        log::error!(\"Failed to parse custom client config\");\n"
        "        return;\n"
        "    };\n"
        "\n"
        "    if let Some(app_name) = data.remove(\"app-name\") {\n",
        "    let Ok(data) =\n"
        "        serde_json::from_slice::<std::collections::HashMap<String, serde_json::Value>>(&data)\n"
        "    else {\n"
        "        log::error!(\"Failed to parse custom client config\");\n"
        "        return;\n"
        "    };\n"
        "    apply_custom_client(data);\n"
        "}\n"
        "\n"
        "pub fn apply_custom_client(mut data: std::collections::HashMap<String, serde_json::Value>) {\n"
        "    if let Some(app_name) = data.remove(\"app-name\") {\n",
    )
    log("src/common.rs patched, custom_client.json written")


def patch_names(app_name, company, bundle_id):
    lower = app_name.lower()
    copyright = f"Copyright © {company}" if company else ""

    # macOS: bundle and executable name come from PRODUCT_NAME
    path = "flutter/macos/Runner/Configs/AppInfo.xcconfig"
    text = read(path)
    text = re.sub(r"^PRODUCT_NAME = .*$", f"PRODUCT_NAME = {app_name}", text, flags=re.M)
    if company:
        text = re.sub(r"^PRODUCT_COPYRIGHT = .*$", f"PRODUCT_COPYRIGHT = {copyright}", text, flags=re.M)
    if bundle_id:
        text = re.sub(r"^PRODUCT_BUNDLE_IDENTIFIER = .*$", f"PRODUCT_BUNDLE_IDENTIFIER = {bundle_id}", text, flags=re.M)
    write(path, text)

    # The pbxproj value wins over AppInfo.xcconfig. Launchd plists pick the new
    # bundle id up at runtime (correct_app_name() in src/platform/macos.rs).
    path = "flutter/macos/Runner.xcodeproj/project.pbxproj"
    text = read(path).replace("RustDesk.app", f"{app_name}.app")
    if bundle_id:
        text = text.replace("PRODUCT_BUNDLE_IDENTIFIER = com.carriez.rustdesk;",
                            f"PRODUCT_BUNDLE_IDENTIFIER = {bundle_id};")
    write(path, text)

    path = "flutter/macos/Runner/Info.plist"
    text = read(path)
    text = re.sub(
        r"(<key>CFBundleURLSchemes</key>\s*<array>\s*<string>)rustdesk(</string>)",
        rf"\g<1>{lower}\g<2>",
        text,
    )
    if bundle_id:
        text = text.replace("<string>com.carriez.rustdesk</string>", f"<string>{bundle_id}</string>")
    write(path, text)

    if company:
        # About page and librustdesk.dll version resource
        path = "flutter/lib/desktop/pages/desktop_setting_page.dart"
        write(path, read(path).replace("Purslane Tech Pte. Ltd.", company))
        path = "Cargo.toml"
        text = read(path)
        text = re.sub(r'^LegalCopyright = ".*"$', lambda m: f'LegalCopyright = "{copyright}"', text, flags=re.M)
        write(path, text)

    path = "build.py"
    write(path, read(path).replace("Products/Release/RustDesk.app", f"Products/Release/{app_name}.app"))

    # Windows: version resource (the exe itself is renamed in the workflow)
    path = "flutter/windows/runner/Runner.rc"
    text = read(path)
    text = re.sub(r'(VALUE "FileDescription", )"[^"]*"', rf'\1"{app_name} Remote Desktop"', text)
    text = re.sub(r'(VALUE "ProductName", )"[^"]*"', rf'\1"{app_name}"', text)
    text = re.sub(r'(VALUE "OriginalFilename", )"[^"]*"', rf'\1"{app_name}.exe"', text)
    if company:
        text = re.sub(r'(VALUE "CompanyName", )"[^"]*"', lambda m: f'{m.group(1)}"{company}"', text)
        text = re.sub(r'(VALUE "LegalCopyright", )"[^"]*"', lambda m: f'{m.group(1)}"{copyright}"', text)
    write(path, text)
    log(f"app name set to {app_name}")


def patch_links(website, privacy_url):
    if website:
        for path in ("flutter/lib/desktop/pages/desktop_setting_page.dart", "flutter/lib/common.dart"):
            write(path, read(path).replace("'https://rustdesk.com'", f"'{website}'"))
    if privacy_url:
        for path in ("flutter/lib/desktop/pages/desktop_setting_page.dart",
                     "flutter/lib/desktop/pages/install_page.dart"):
            write(path, read(path).replace("https://rustdesk.com/privacy.html", privacy_url))
    if website or privacy_url:
        log("links updated")


def patch_icons():
    icon = ASSETS_DIR / "icon.png"
    logo = ASSETS_DIR / "logo.png"
    if not icon.exists() and not logo.exists():
        log("no custom/assets/icon.png or logo.png, keeping RustDesk icons")
        return
    from PIL import Image  # pip install pillow

    if logo.exists():
        Image.open(logo).convert("RGBA").save(ROOT / "flutter/assets/logo.png")
        log("logo.png installed")
    if not icon.exists():
        return

    src = Image.open(icon).convert("RGBA")
    if src.width != src.height:
        fail("custom/assets/icon.png must be square")
    if src.width < 256:
        fail("custom/assets/icon.png must be at least 256x256 (1024x1024 recommended)")

    def png(path, size):
        src.resize((size, size), Image.LANCZOS).save(ROOT / path)

    def png_like(path):
        # keep the original file's dimensions
        with Image.open(ROOT / path) as old:
            size = old.size[0]
        png(path, size)

    def ico(path):
        sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
        src.resize((256, 256), Image.LANCZOS).save(ROOT / path, format="ICO", sizes=sizes)

    for p in ("res/icon.ico", "res/tray-icon.ico",
              "flutter/windows/runner/resources/app_icon.ico",
              "flutter/assets/icon.ico"):
        ico(p)
    for p in ("res/icon.png", "res/mac-icon.png", "res/32x32.png", "res/64x64.png",
              "res/128x128.png", "res/128x128@2x.png"):
        png_like(p)
    png("flutter/assets/icon.png", 512)

    # macOS menu bar uses template images: only the alpha channel matters
    for p in ("res/mac-tray-dark-x2.png", "res/mac-tray-light-x2.png"):
        with Image.open(ROOT / p) as old:
            size = old.size
        alpha = src.resize(size, Image.LANCZOS).getchannel("A")
        color = (255, 255, 255, 0) if "light" in p else (0, 0, 0, 0)
        tpl = Image.new("RGBA", size, color)
        tpl.putalpha(alpha)
        tpl.save(ROOT / p)

    src.resize((1024, 1024), Image.LANCZOS).save(ROOT / "flutter/macos/Runner/AppIcon.icns", format="ICNS")
    log("icons generated")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="validate config and print it, change nothing")
    args = ap.parse_args()

    cfg, app_name, server, conn_type = load_config()
    password = os.environ.get("RD_PRESET_PASSWORD", "")
    if password and len(password) < 12:
        fail("RD_PRESET_PASSWORD is too short, use at least 12 characters")
    if not password:
        log("RD_PRESET_PASSWORD not set, building without preset permanent password")

    data = build_custom_client(cfg, app_name, server, conn_type, password)

    if args.dry_run:
        shown = dict(data)
        if "password" in shown:
            shown["password"] = "<hash>"
        print(json.dumps(shown, indent=2, ensure_ascii=False))
        return

    if not (ROOT / "src/common.rs").exists() or not (ROOT / "libs/hbb_common").exists():
        fail("run this from the root of a RustDesk checkout (with submodules)")
    patch_rust(data)
    company = cfg.get("company", "").strip()
    if re.search(r"[\"'`$\\]", company):
        fail("company must not contain quotes, backticks, $ or backslashes")
    bundle_id = cfg.get("macos_bundle_id", "").strip()
    if bundle_id and not re.match(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$", bundle_id):
        fail("macos_bundle_id must look like sk.mojafirma.firmadesk")
    url_re = re.compile(r"^https?://[^\s'\"\\$]+$")
    website = cfg.get("website", "").strip()
    privacy_url = cfg.get("privacy_url", "").strip()
    for name, url in (("website", website), ("privacy_url", privacy_url)):
        if url and not url_re.match(url):
            fail(f"{name} must be a plain http(s) URL")

    patch_names(app_name, company, bundle_id)
    patch_links(website, privacy_url)
    patch_icons()
    log("done")


if __name__ == "__main__":
    main()

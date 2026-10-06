#!/usr/bin/env python3
"""
Chrome 2 - a web browser written in Python (PyQt6 + Qt WebEngine / Chromium), formerly called Foxglove.

  * Google Chrome's New Tab page (Google search, shortcuts or most visited sites, Customize Chrome) and logo,
    in a dark toolbar look with a light-purple accent
  * Privacy screen: windows fade to grey while Chrome 2 isn't the active app (Settings > Privacy)
  * Session restore: quit any time - your tabs (with their back/forward history) and your
    cookies come back next launch, so you stay signed in
  * Bookmarks: star button, bookmarks toolbar with folders, bookmark manager, HTML import/export
  * Chrome extensions (Manifest V3): one-click install from the Chrome Web Store, or from a
    .crx/.zip file or an unpacked folder; toolbar popups with badges, options pages, enable/disable/remove.
    Chrome 2 fills in the extension APIs its engine lacks - chrome.action, contextMenus, notifications,
    alarms, tabs, windows, scripting, offscreen, storage.sync/onChanged, i18n, commands - so extensions
    built for Chrome run, including ad blockers' declarativeNetRequest rules
  * VPN / proxy: route the browser through Tor, Cloudflare WARP or your own HTTP/SOCKS5 proxy,
    with WebRTC leak protection, a connection check and a kill switch (shield button in the toolbar)
  * Smart address bar (history + bookmarks), downloads panel, find in page, per-site zoom,
    developer tools, printing, permission prompts, full-screen video and more

Setup (once):   python3 -m pip install --upgrade PyQt6 PyQt6-WebEngine
Run:            python3 foxglove.py            (optionally followed by URLs to open; --verbose shows
                                                extensions' errors)
macOS app:      python3 foxglove.py --install-app   (makes ~/Applications/Chrome 2.app, for the Dock)

Your data (tabs, bookmarks, cookies, extensions) is kept in (the folder keeps the browser's former name)
  macOS:   ~/Library/Application Support/Foxglove
  Windows: %APPDATA%\\Foxglove
  Linux:   ~/.local/share/Foxglove
"""
from __future__ import annotations

import argparse
import atexit
import base64
import errno
import hashlib
import hmac
import html
import io
import itertools
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import weakref
import zipfile
from dataclasses import dataclass, field as dc_field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote

try:
    from PyQt6 import sip
    from PyQt6.QtCore import (
        QT_VERSION_STR, QBuffer, QByteArray, QCoreApplication, QDataStream, QDateTime, QEvent, QIODevice, QItemSelectionModel,
        QLocale, QLockFile, QObject, QPoint, QRect, QRectF, QSize, QStandardPaths, Qt, QTimer, QUrl,
        pyqtSignal,
    )
    from PyQt6.QtGui import (
        QAction, QColor, QDesktopServices, QFont, QFontDatabase, QGuiApplication, QIcon, QIntValidator, QKeySequence, QPainter,
        QPalette, QPen, QPixmap, QStandardItem, QStandardItemModel,
    )
    from PyQt6.QtNetwork import (
        QAuthenticator, QNetworkAccessManager, QNetworkCookie, QNetworkProxy, QNetworkProxyFactory, QNetworkReply,
        QNetworkRequest,
    )
    from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
    from PyQt6.QtWidgets import (
        QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox,
        QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
        QMenu, QMenuBar, QMessageBox, QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSplitter,
        QStackedWidget, QStyle, QStyledItemDelegate, QStyleOption, QTabBar, QToolButton, QTreeWidget,
        QTreeWidgetItem, QVBoxLayout, QWidget, QWidgetAction,
    )
    from PyQt6.QtWebEngineCore import (
        QWebEngineCertificateError, QWebEngineContextMenuRequest, QWebEngineDownloadRequest, QWebEngineLoadingInfo,
        QWebEngineNewWindowRequest, QWebEnginePage, QWebEnginePermission, QWebEngineProfile, QWebEngineScript,
        QWebEngineSettings, QWebEngineUrlRequestInfo, QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestJob,
        QWebEngineUrlScheme, QWebEngineUrlSchemeHandler, qWebEngineChromiumVersion,
    )
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError as exc:  # shown instead of a traceback when the packages are missing
    sys.exit(
        "Chrome 2 needs PyQt6 and PyQt6-WebEngine (6.8 or newer). Install them with:\n\n"
        "    python3 -m pip install --upgrade PyQt6 PyQt6-WebEngine\n\n"
        f"(details: {exc})"
    )

APP_NAME = "Chrome 2"          # the name people see: windows, menus, dialogs, the Dock
DATA_NAME = "Foxglove"         # what things are stored under (data folder, extension IDs): never changes
APP_VERSION = "1.0"
IS_MAC = sys.platform == "darwin"
HAS_EXTENSIONS = hasattr(QWebEngineProfile, "extensionManager")  # Qt WebEngine 6.10+
TRANSLUCENT_POPUPS = sys.platform in ("darwin", "win32")
NEWTAB = "foxglove://newtab"
VERBOSE = False               # --verbose: Chromium's and extensions' messages in the terminal

TAB_MIN_WIDTH, TAB_MAX_WIDTH, TAB_HEIGHT = 80, 240, 40
TAB_CLOSE_AREA = 32           # room for the close button on the right of each tab
MAX_CLOSED_TABS = 25
ZOOM_LEVELS = (0.3, 0.5, 0.67, 0.8, 0.9, 1.0, 1.1, 1.2, 1.33, 1.5, 1.7, 2.0, 2.4, 3.0, 4.0, 5.0)

SEARCH_ENGINES = {
    "Google": "https://www.google.com/search?q={}",
    "DuckDuckGo": "https://duckduckgo.com/?q={}",
    "Bing": "https://www.bing.com/search?q={}",
    "Brave Search": "https://search.brave.com/search?q={}",
    "Startpage": "https://www.startpage.com/do/search?q={}",
    "Ecosia": "https://www.ecosia.org/search?q={}",
    "Wikipedia": "https://en.wikipedia.org/w/index.php?search={}",
}
WEBSTORE_HOME = "https://chromewebstore.google.com/"
WEBSTORE_RE = re.compile(
    r"^https://(?:chromewebstore\.google\.com/detail|chrome\.google\.com/webstore/detail)/"
    r"(?:[^/?#]+/)?([a-p]{32})(?:[/?#]|$)"
)
# The Web Store's update service: answers with a redirect to the extension's signed .crx ({version}: Chromium's).
WEBSTORE_CRX_URL = ("https://clients2.google.com/service/update2/crx?response=redirect&prodversion={version}"
                    "&acceptformat=crx2,crx3&x=id%3D{id}%26uc")
COMPONENT_EXTENSIONS = {"mhjfbmdgcfjbbpaeojofohoefgiehjai", "nkeimhogjdpnpccoofpliimaahmaaome"}
EXT_SCHEME = "foxglove-ext"   # extensions -> Foxglove calls (foxglove-ext://bridge/call) + web-accessible files
EXT_BRIDGE_URL = f"{EXT_SCHEME}://bridge/call"
MAX_UNPACKED_SIZE = 512 * 1024 * 1024

_PT = QWebEnginePermission.PermissionType
PERMISSION_TEXT = {
    _PT.MediaAudioCapture: "use your microphone",
    _PT.MediaVideoCapture: "use your camera",
    _PT.MediaAudioVideoCapture: "use your camera and microphone",
    _PT.DesktopVideoCapture: "share your screen",
    _PT.DesktopAudioVideoCapture: "share your screen and system audio",
    _PT.MouseLock: "hide your mouse pointer",
    _PT.Notifications: "send you notifications",
    _PT.Geolocation: "access your location",
    _PT.ClipboardReadWrite: "read and write your clipboard",
    _PT.LocalFontsAccess: "use the fonts installed on your computer",
}
# Site settings rows. Qt stores decisions for location, notifications, clipboard and fonts itself; camera, microphone
# and pointer lock it asks about every time, so Foxglove remembers those ("site_permissions"). Screen sharing can only
# be blocked: like Chrome, Foxglove always asks before a site sees your screen.
SITE_PERMISSIONS = ((_PT.Geolocation, "Location"), (_PT.MediaVideoCapture, "Camera"), (_PT.MediaAudioCapture, "Microphone"),
                    (_PT.Notifications, "Notifications"), (_PT.ClipboardReadWrite, "Clipboard"),
                    (_PT.DesktopVideoCapture, "Screen sharing"), (_PT.MouseLock, "Pointer lock"),
                    (_PT.LocalFontsAccess, "Fonts on your computer"))
PERMISSION_PARTS = {_PT.MediaAudioVideoCapture: (_PT.MediaAudioCapture, _PT.MediaVideoCapture),
                    _PT.DesktopAudioVideoCapture: (_PT.DesktopVideoCapture,)}  # requests for two at once
ASK_ALWAYS = {_PT.DesktopVideoCapture, _PT.DesktopAudioVideoCapture}


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Look & feel
# ══════════════════════════════════════════════════════════════════════════════════════════
class P:
    """Firefox "Proton" dark palette, with a light-purple accent."""
    FRAME = "#1c1b22"          # tab strip
    TOOLBAR = "#2b2a33"        # navigation + bookmarks toolbars, dialogs
    TAB_SELECTED = "#42414d"
    FIELD = "#1c1b22"          # address bar and other inputs
    FIELD_FOCUS = "#42414d"
    PANEL = "#2b2a33"          # menus and pop-up panels
    PANEL_BORDER = "#52525e"
    BUTTON = "#42414d"
    BUTTON_HOVER = "#52525e"
    BUTTON_PRESSED = "#5b5b66"
    TEXT = "#fbfbfe"
    TEXT_2 = "#cfcfd8"
    TEXT_3 = "#8f8f9d"
    TEXT_DISABLED = "#6b6a74"
    LINE = "#0c0c0d"
    ACCENT = "#b9a3ff"
    ACCENT_HOVER = "#cbb9ff"
    ACCENT_PRESSED = "#a58df7"
    ACCENT_SOFT = "rgba(185, 163, 255, 0.24)"
    ON_ACCENT = "#1c1b22"
    DANGER = "#ff848b"
    WARNING = "#ffbd4f"


# Icon shapes (24x24, stroked) - adapted from the MIT-licensed Feather icon set.
ICONS = {
    "back": '<path d="M19 12H5"/><path d="m11 18-6-6 6-6"/>',
    "forward": '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    "reload": '<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
    "stop": '<path d="M18 6 6 18M6 6l12 12"/>',
    "close": '<path d="M17 7 7 17M7 7l10 10"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "star": '<path d="m12 2.5 2.94 5.96 6.56.95-4.75 4.63 1.12 6.54L12 17.5l-5.87 3.08 1.12-6.54L2.5 9.41l6.56-.95z"/>',
    "star-filled": '<path fill="{c}" d="m12 2.5 2.94 5.96 6.56.95-4.75 4.63 1.12 6.54L12 17.5l-5.87 3.08 1.12-6.54L2.5 9.41l6.56-.95z"/>',
    "menu": '<path d="M4 6.5h16M4 12h16M4 17.5h16"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7.5a4 4 0 0 1 8 0V11"/>',
    "warning": '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4M12 17h.01"/>',
    "info": '<circle cx="12" cy="12" r="9.5"/><path d="M12 16v-4.5M12 8h.01"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4.5-4.5"/>',
    "download": '<path d="M12 3.5v12M7 11l5 5 5-5"/><path d="M4.5 20.5h15"/>',
    "folder": '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.38l2 2.5h8.62A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z"/>',
    "globe": '<circle cx="12" cy="12" r="9.5"/><path d="M2.5 12h19"/><path d="M12 2.5a14.5 14.5 0 0 1 3.8 9.5 14.5 14.5 0 0 1-3.8 9.5 14.5 14.5 0 0 1-3.8-9.5A14.5 14.5 0 0 1 12 2.5z"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-up": '<path d="m18 15-6-6-6 6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "chevrons-right": '<path d="m13 17 5-5-5-5M6 17l5-5-5-5"/>',
    "puzzle": '<path transform="translate(12 12) scale(1.18) translate(-13.25 -10.75)" d="M6 6h3.5V4.5a2 2 0 0 1 4 0V6H17a1 1 0 0 1 1 1v3.5h1.5a2 2 0 0 1 0 4H18V18a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "clock": '<circle cx="12" cy="12" r="9.5"/><path d="M12 7v5l3.5 2"/>',
    "trash": '<path d="M3.5 6h17M8 6V4.5A1.5 1.5 0 0 1 9.5 3h5A1.5 1.5 0 0 1 16 4.5V6m2.5 0-.9 13.1A2 2 0 0 1 15.6 21H8.4a2 2 0 0 1-2-1.9L5.5 6"/>',
    "external": '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6M10 14 21 3"/>',
    "speaker": '<path fill="{c}" d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/>',
    "speaker-muted": '<path fill="{c}" d="M11 5 6 9H2v6h4l5 4z"/><path d="m22 9-6 6M16 9l6 6"/>',
    "fullscreen": '<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>',
    "file": '<path d="M13.5 2.5h-7a2 2 0 0 0-2 2v15a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-11z"/><path d="M13.5 2.5v6h6"/>',
    "code": '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
    "shield": '<path d="M12 2.5 4.5 5.5v6c0 4.6 3.2 8.6 7.5 10 4.3-1.4 7.5-5.4 7.5-10v-6z"/>',
    "shield-on": '<path fill="{c}" d="M12 2.5 4.5 5.5v6c0 4.6 3.2 8.6 7.5 10 4.3-1.4 7.5-5.4 7.5-10v-6z"/>'
                 '<path stroke="#1c1b22" stroke-width="2.2" d="m8.5 12 2.5 2.5 4.5-5"/>',
}

# The Google Chrome logo: three 120° sectors (outer radius 24) whose edges are tangents of the white disc (radius 12),
# each running from the disc to the rim 60° further on - the "pinwheel" - around a blue centre (radius 9.5). The
# sectors are drawn as wedges through the centre (the white disc covers that) with a hairline of their own colour, so
# no background shows through the anti-aliased seams.
LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">
<defs>
<linearGradient id="chrome-r" x1="3.2" y1="15" x2="44.8" y2="15" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#d93025"/><stop offset="1" stop-color="#ea4335"/></linearGradient>
<linearGradient id="chrome-y" x1="20.7" y1="47.7" x2="41.5" y2="11.7" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#fcc934"/><stop offset="1" stop-color="#fbbc04"/></linearGradient>
<linearGradient id="chrome-g" x1="26.6" y1="46.5" x2="5.8" y2="10.5" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#1e8e3e"/><stop offset="1" stop-color="#34a853"/></linearGradient>
</defs>
<path fill="url(#chrome-r)" stroke="url(#chrome-r)" stroke-width=".12" d="M24 12L44.7846 12A24 24 0 0 0 3.2154 12L13.6077 30L24 24Z"/>
<path fill="url(#chrome-y)" stroke="url(#chrome-y)" stroke-width=".12" d="M34.3923 30L24 48A24 24 0 0 0 44.7846 12L24 12L24 24Z"/>
<path fill="url(#chrome-g)" stroke="url(#chrome-g)" stroke-width=".12" d="M13.6077 30L3.2154 12A24 24 0 0 0 24 48L34.3923 30L24 24Z"/>
<circle cx="24" cy="24" r="12" fill="#fff"/>
<circle cx="24" cy="24" r="9.5" fill="#1a73e8"/>
</svg>"""


def logo_image(size: int, margin: float = 0.0, background: str = "") -> "QImage":
    """The logo rendered at *size* px (square), *margin* (a fraction of size) on each side, on *background* or clear."""
    from PyQt6.QtGui import QImage
    from PyQt6.QtSvg import QSvgRenderer
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(background) if background else QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    inset = size * margin
    QSvgRenderer(QByteArray(LOGO_SVG.encode("utf-8"))).render(painter, QRectF(inset, inset, size - 2 * inset,
                                                                               size - 2 * inset))
    painter.end()
    return image


class IconFactory:
    """Writes the SVG icons to a private temp folder (style sheets need real files) and caches QIcons."""

    def __init__(self) -> None:
        for stale in Path(tempfile.gettempdir()).glob("foxglove-icons-*"):  # left behind by a crash
            try:
                if time.time() - stale.stat().st_mtime > 86400:
                    shutil.rmtree(stale, ignore_errors=True)
            except OSError:
                pass
        self.dir = Path(tempfile.mkdtemp(prefix="foxglove-icons-"))
        atexit.register(shutil.rmtree, self.dir, ignore_errors=True)
        self._cache: dict[tuple[str, str], QIcon] = {}

    def path(self, name: str, color: str = P.TEXT, size: int = 24) -> str:
        target = self.dir / f"{name}-{color.lstrip('#')}-{size}.svg"
        if not target.exists():
            body = ICONS[name].replace("{c}", color)
            target.write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" '
                f'fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" '
                f'stroke-linejoin="round">{body}</svg>',
                encoding="utf-8",
            )
        return target.as_posix()

    def get(self, name: str, color: str = P.TEXT) -> QIcon:
        key = (name, color)
        if key not in self._cache:
            icon = QIcon(self.path(name, color))
            icon.addFile(self.path(name, P.TEXT_DISABLED), QSize(), QIcon.Mode.Disabled)
            self._cache[key] = icon
        return self._cache[key]

    def logo(self) -> QIcon:
        target = self.dir / "logo.svg"
        if not target.exists():
            target.write_text(LOGO_SVG, encoding="utf-8")
        return QIcon(target.as_posix())

    def app_icon(self) -> QIcon:
        """The application's (Dock / taskbar) icon: the logo with the margin app icons have around them."""
        result = QIcon()
        for size in (16, 32, 64, 128, 256, 512, 1024):
            result.addPixmap(QPixmap.fromImage(logo_image(size, 0.08 if size >= 64 else 0.0)))
        return result


_icon_factory: IconFactory | None = None


def icons() -> IconFactory:
    global _icon_factory
    if _icon_factory is None:
        _icon_factory = IconFactory()
    return _icon_factory


def icon(name: str, color: str = P.TEXT) -> QIcon:
    return icons().get(name, color)


QSS = """
* { outline: 0; }
QWidget { color: %(text)s; }
QMainWindow, QDialog, #PopupWindow { background: %(toolbar)s; }
QLabel { background: transparent; }
QLabel[tone="dim"] { color: %(text_3)s; }
QLabel[tone="secondary"] { color: %(text_2)s; }
QLabel[tone="title"] { font-weight: 600; font-size: 15px; }
QLabel[tone="heading"] { font-weight: 600; color: %(text)s; }
QLabel[tone="error"] { color: %(danger)s; }
QLabel[tone="accent"] { color: %(accent)s; font-weight: 600; }
QToolTip { background: %(panel)s; color: %(text)s; border: 1px solid %(panel_border)s; padding: 4px 7px; }

/* Tab strip */
#TabStrip { background: %(frame)s; }
QTabBar#Tabs { background: transparent; border: none; }
QTabBar#Tabs::tab { background: transparent; border: none; border-radius: 6px; margin: 4px 2px; padding: 0 6px 0 0; color: %(text)s; }
QTabBar#Tabs::tab:hover:!selected { background: rgba(251, 251, 254, 0.07); }
QTabBar#Tabs::tab:selected { background: %(tab_selected)s; }
QTabBar#Tabs::close-button { image: url("%(close)s"); subcontrol-position: right; border-radius: 4px; padding: 2px; }
QTabBar#Tabs::close-button:hover { background: rgba(251, 251, 254, 0.14); }
QTabBar#Tabs::close-button:pressed { background: rgba(251, 251, 254, 0.24); }
QTabBar#Tabs QToolButton { background: %(frame)s; border: none; border-radius: 4px; margin: 6px 0; }
QTabBar#Tabs QToolButton:hover { background: #2e2d36; }
QTabBar#Tabs QToolButton::left-arrow { image: url("%(chevron_left)s"); }
QTabBar#Tabs QToolButton::right-arrow { image: url("%(chevron_right)s"); }

/* Toolbars */
#NavBar, #BookmarksBar { background: %(toolbar)s; }
#ChromeSeparator { background: %(line)s; }
QToolButton { background: transparent; border: none; border-radius: 4px; color: %(text)s; }
QToolButton:hover { background: rgba(251, 251, 254, 0.10); }
QToolButton:pressed, QToolButton:checked { background: rgba(251, 251, 254, 0.18); }
QToolButton:disabled { background: transparent; }
QToolButton::menu-indicator { image: none; width: 0; }
QToolButton#BookmarkItem { padding: 0 6px; color: %(text)s; }

/* Address bar */
QLineEdit#UrlBar { background: %(field)s; color: %(text)s; border: 2px solid transparent; border-radius: 6px;
    padding: 0 2px; selection-background-color: %(accent)s; selection-color: %(on_accent)s; font-size: 14px; }
QLineEdit#UrlBar:hover { background: #222129; }
QLineEdit#UrlBar:focus { background: %(field_focus)s; border-color: %(accent)s; }
QListView#Suggestions { background: %(panel)s; border: 1px solid %(panel_border)s; padding: 0; }

/* Inputs */
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QSpinBox { background: %(field)s; color: %(text)s;
    border: 1px solid %(panel_border)s; border-radius: 4px; padding: 5px 8px;
    selection-background-color: %(accent)s; selection-color: %(on_accent)s; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus { border-color: %(accent)s; }
QLineEdit:disabled, QComboBox:disabled { color: %(text_3)s; }
QComboBox { padding-right: 26px; }
QComboBox::drop-down { border: none; width: 24px; subcontrol-origin: padding; subcontrol-position: center right; }
QComboBox::down-arrow { image: url("%(chevron_down)s"); }
QComboBox QAbstractItemView { background: %(panel)s; color: %(text)s; border: 1px solid %(panel_border)s; padding: 4px;
    selection-background-color: %(accent_soft)s; selection-color: %(text)s; outline: 0; }

/* Buttons */
QPushButton { background: %(button)s; color: %(text)s; border: 1px solid transparent; border-radius: 4px; padding: 6px 16px; min-width: 56px; }
QPushButton:hover { background: %(button_hover)s; }
QPushButton:pressed { background: %(button_pressed)s; }
QPushButton:focus { border-color: %(accent)s; }
QPushButton:disabled { background: #34333b; color: %(text_disabled)s; }
QPushButton:default, QPushButton[primary="true"] { background: %(accent)s; color: %(on_accent)s; font-weight: 600; }
QPushButton:default:hover, QPushButton[primary="true"]:hover { background: %(accent_hover)s; }
QPushButton:default:pressed, QPushButton[primary="true"]:pressed { background: %(accent_pressed)s; }
QPushButton:default:focus, QPushButton[primary="true"]:focus { border-color: %(text)s; }
QPushButton[danger="true"] { color: %(danger)s; }

QCheckBox, QRadioButton { spacing: 8px; background: transparent; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; background: %(field)s; border: 1px solid %(text_3)s; }
QCheckBox::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 9px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: %(accent)s; }
QCheckBox::indicator:checked { background: %(accent)s; border-color: %(accent)s; image: url("%(check)s"); }
QRadioButton::indicator:checked { background: %(on_accent)s; border: 5px solid %(accent)s; width: 8px; height: 8px; }

/* Menus */
QMenu { background: %(panel)s; border: 1px solid %(panel_border)s; padding: 5px 0; }
QMenu[rounded="true"] { border-radius: 8px; }
QMenu::item { background: transparent; color: %(text)s; padding: 6px 28px 6px 14px; margin: 0 5px; border-radius: 4px; }
QMenu::item:selected { background: rgba(251, 251, 254, 0.10); }
QMenu::item:disabled { color: %(text_disabled)s; }
QMenu::separator { height: 1px; background: rgba(251, 251, 254, 0.12); margin: 5px 10px; }
QMenu::icon { padding-left: 6px; }
QMenu::indicator { width: 14px; height: 14px; padding-left: 6px; }
QMenu::indicator:checked { image: url("%(menu_check)s"); }
QMenu::right-arrow { image: url("%(chevron_right)s"); margin-right: 6px; }
QMenuBar { background: %(toolbar)s; }

/* Scroll bars */
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle { background: rgba(251, 251, 254, 0.22); border-radius: 3px; margin: 2px; }
QScrollBar::handle:hover { background: rgba(251, 251, 254, 0.38); }
QScrollBar::handle:vertical { min-height: 28px; }
QScrollBar::handle:horizontal { min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* Lists and trees */
QTreeView, QListView { background: %(field)s; alternate-background-color: #212027; color: %(text)s;
    border: 1px solid #3a3944; border-radius: 6px; padding: 2px; outline: 0;
    selection-background-color: %(accent_soft)s; selection-color: %(text)s; }
QTreeView::item, QListView::item { padding: 4px 2px; border: none; }
QTreeView::item:hover, QListView::item:hover { background: rgba(251, 251, 254, 0.06); }
QTreeView::item:selected, QListView::item:selected { background: %(accent_soft)s; color: %(text)s; }
QTreeView::branch { background: transparent; }
QTreeView::branch:has-children:closed { image: url("%(chevron_right)s"); }
QTreeView::branch:has-children:open { image: url("%(chevron_down)s"); }
QHeaderView { background: transparent; }
QHeaderView::section { background: %(toolbar)s; color: %(text_2)s; border: none; border-bottom: 1px solid #3a3944; padding: 6px 8px; font-weight: 600; }

QProgressBar { background: rgba(251, 251, 254, 0.12); border: none; border-radius: 2px; max-height: 4px; min-height: 4px; color: transparent; }
QProgressBar::chunk { background: %(accent)s; border-radius: 2px; }
QSplitter::handle { background: %(line)s; }
QSplitter::handle:vertical { height: 1px; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }

/* Panels, bars and bubbles */
#Panel { background: %(panel)s; border: 1px solid %(panel_border)s; border-radius: 8px; }
#Panel[square="true"] { border-radius: 0; }
#PanelSeparator { background: rgba(251, 251, 254, 0.12); }
#InfoBar { background: #3a3448; border-bottom: 1px solid %(line)s; }
#InfoBar[kind="warning"] { background: #4a3b28; }
#FindBar { background: %(toolbar)s; border-top: 1px solid %(line)s; }
#FindBar QLineEdit[notfound="true"] { background: #5c2730; border-color: %(danger)s; }
#StatusBubble { background: %(panel)s; color: %(text_2)s; border: 1px solid %(panel_border)s; border-radius: 4px; padding: 3px 8px; }
#Toast { background: %(tab_selected)s; border: 1px solid %(panel_border)s; border-radius: 8px; }
#Toast[error="true"] { border-color: %(danger)s; }
#ExtensionRow, #DownloadRow { background: transparent; border-radius: 6px; }
#ExtensionRow:hover, #DownloadRow:hover { background: rgba(251, 251, 254, 0.06); }
#PopupUrl { background: %(toolbar)s; color: %(text_2)s; padding: 6px 10px; border-bottom: 1px solid %(line)s; }
"""


def build_stylesheet() -> str:
    values = {name.lower(): value for name, value in vars(P).items() if name.isupper()}
    f = icons()
    values.update(
        check=f.path("check", P.ON_ACCENT, 14),
        menu_check=f.path("check", P.ACCENT, 14),
        close=f.path("close", P.TEXT_2, 16),
        chevron_down=f.path("chevron-down", P.TEXT_2, 12),
        chevron_right=f.path("chevron-right", P.TEXT_2, 12),
        chevron_left=f.path("chevron-left", P.TEXT_2, 12),
    )
    return QSS % values


def apply_dark_palette(app: QApplication) -> None:
    pal = QPalette()
    role, group = QPalette.ColorRole, QPalette.ColorGroup
    for r, color in (
        (role.Window, P.TOOLBAR), (role.WindowText, P.TEXT), (role.Base, P.FIELD),
        (role.AlternateBase, "#212027"), (role.Text, P.TEXT), (role.Button, P.BUTTON),
        (role.ButtonText, P.TEXT), (role.Highlight, P.ACCENT), (role.HighlightedText, P.ON_ACCENT),
        (role.ToolTipBase, P.PANEL), (role.ToolTipText, P.TEXT), (role.PlaceholderText, P.TEXT_3),
        (role.Link, P.ACCENT), (role.LinkVisited, P.ACCENT_PRESSED), (role.BrightText, P.DANGER),
        (role.Light, "#52525e"), (role.Midlight, "#42414d"), (role.Mid, "#2b2a33"),
        (role.Dark, "#151419"), (role.Shadow, "#000000"),
    ):
        pal.setColor(r, QColor(color))
    for r in (role.WindowText, role.Text, role.ButtonText):
        pal.setColor(group.Disabled, r, QColor(P.TEXT_DISABLED))
    app.setPalette(pal)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ══════════════════════════════════════════════════════════════════════════════════════════
def log(*parts) -> None:
    print(f"[{APP_NAME}]", *parts, file=sys.stderr)


def install_error_guard() -> None:
    """PyQt aborts the whole app on an unexpected error inside an event handler; log it and keep browsing."""
    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log("Unexpected error (the browser keeps running):")
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)

    sys.excepthook = hook


def clamp(value, low, high):
    return max(low, min(high, value))


def elide(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def human_size(num: float) -> str:
    if num < 1024:
        return f"{int(num)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num /= 1024.0
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}"
    return f"{num:.1f} TB"


def read_json(path: Path, default):
    for candidate in (path, path.with_name(path.name + ".bak")):
        try:
            with open(candidate, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            log(f"Ignoring unreadable {candidate.name}: {exc}")
    return default


def write_json(path: Path, data, keep_backup: bool = False) -> bool:
    """Atomically replace *path* (write to a temp file, then rename) so a crash never leaves half a file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        if keep_backup and path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        log(f"Could not save {path.name}: {exc}")
        return False


_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
NAVIGABLE_SCHEMES = {"http", "https", "file", "ftp", "about", "chrome", "view-source", "data",
                     "chrome-extension", "foxglove", "blob", "mailto", "tel"}
# Endings that look like a domain's TLD but are really file names ("node.js", "notes.txt") - search for those.
NOT_TLDS = {"js", "mjs", "cjs", "jsx", "ts", "tsx", "json", "txt", "html", "htm", "xhtml", "css", "scss", "less",
            "csv", "tsv", "pdf", "png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico", "exe", "dmg", "pkg", "msi",
            "deb", "rpm", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "rtf", "java", "jar", "rb", "php",
            "cpp", "hpp", "lock", "log", "ini", "cfg", "conf", "yml", "yaml", "toml", "xml", "plist", "sql",
            "sqlite", "bak", "tmp", "tar", "gz", "tgz", "rar", "iso", "mp3", "mp4", "wav", "flac", "mkv", "avi",
            "ipynb", "vue", "kt", "lua", "bat", "dll"}


def url_from_input(text: str, search_template: str) -> QUrl:
    """Turn what the user typed into a URL - or a web search, like Firefox's address bar."""
    text = text.strip()
    if not text:
        return QUrl()
    match = _SCHEME_RE.match(text)
    if match and match.group(1).lower() in NAVIGABLE_SCHEMES:
        url = QUrl(text)
        if url.isValid():
            return url
    expanded = os.path.expanduser(text)
    if (text.startswith(("/", "~")) or re.match(r"^[a-zA-Z]:[\\/]", text)) and os.path.exists(expanded):
        return QUrl.fromLocalFile(os.path.abspath(expanded))
    if not any(ch.isspace() for ch in text):
        host = re.split(r"[/?#]", text, maxsplit=1)[0].rsplit("@", 1)[-1]
        bare = re.sub(r":\d{1,5}$", "", host).lower()
        if bare == "localhost" or _IPV4_RE.match(bare) or bare.startswith("["):
            return QUrl("http://" + text)
        if re.fullmatch(r"[a-z0-9-]+", bare) and re.search(r":\d{1,5}$", host):  # "my-host:8080"
            return QUrl("http://" + text)
        labels = bare.split(".")
        tld = labels[-1]
        if (len(labels) >= 2 and all(labels) and tld not in NOT_TLDS
                and ((tld.isalpha() and len(tld) >= 2) or tld.startswith("xn--"))):
            url = QUrl("https://" + text)
            if url.isValid() and url.host():
                return url
    return QUrl(search_template.format(quote_plus(text)))


def is_newtab(url: QUrl) -> bool:
    return url.scheme() == "foxglove" and url.host() == "newtab"


NEWTAB_OVERRIDES: set[str] = set()  # extension New Tab pages (chrome_url_overrides) - shown with an empty address bar


def display_url(url: QUrl) -> str:
    if url.isEmpty() or is_newtab(url) or url.toString() in ("about:blank", *NEWTAB_OVERRIDES):
        return ""
    return url.toDisplayString()


_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "mil", "nic", "sch", "ltd", "plc", "nhs"}
# Hosting domains where every subdomain is someone else's site (the "private" part of the public suffix list, the
# common ones): alice.github.io and bob.github.io are two sites, as in Chrome.
PUBLIC_SUFFIXES = frozenset("""github.io githubusercontent.com gitlab.io bitbucket.io vercel.app vercel.dev now.sh
netlify.app pages.dev workers.dev r2.dev web.app firebaseapp.com appspot.com run.app cloudfunctions.net
googleusercontent.com translate.goog herokuapp.com blogspot.com azurewebsites.net azurestaticapps.net cloudapp.net
cloudfront.net s3.amazonaws.com elasticbeanstalk.com onrender.com fly.dev glitch.me repl.co replit.app ngrok.io
ngrok.app ngrok-free.app trycloudflare.com myshopify.com wixsite.com webflow.io neocities.org surge.sh codeberg.page
readthedocs.io sourceforge.io deno.dev supabase.co carrd.co notion.site streamlit.app hf.space up.railway.app
ondigitalocean.app digitaloceanspaces.com pythonanywhere.com gitpod.io stackblitz.io csb.app duckdns.org""".split())


def site_of(host: str) -> str:
    """The site a host belongs to, about its registrable domain: accounts.google.com -> google.com, www.bbc.co.uk ->
    bbc.co.uk, alice.github.io -> alice.github.io. IP addresses and localhost are their own site."""
    host = host.lower().strip(".").strip("[]")
    labels = host.split(".")
    if len(labels) <= 2 or re.fullmatch(r"[\d.]+|[0-9a-f:]+", host):
        return host
    for n in range(len(labels) - 1, 1, -1):  # the longest known suffix, and one label more
        if ".".join(labels[-n:]) in PUBLIC_SUFFIXES:
            return ".".join(labels[-n - 1:])
    return ".".join(labels[-3:] if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else labels[-2:])


def origin_of(url: QUrl) -> str:
    """"scheme://host[:port]" of a web address ("" for anything else) - what permissions and storage belong to."""
    if url.scheme() not in ("http", "https") or not url.host():
        return ""
    host = url.host(QUrl.ComponentFormattingOption.FullyEncoded)
    port, default = url.port(), DEFAULT_PORTS[url.scheme()]
    return f"{url.scheme()}://{f'[{host}]' if ':' in host else host}{f':{port}' if port not in (-1, default) else ''}"


# scheme, host (without the port), port ("*", digits or None: any), path - as Chrome's URLPattern splits them
MATCH_PATTERN = re.compile(r"(\*|[a-z][a-z0-9+.-]*)://(\*|\*\.[^/*:]+|[^/*:]*)(?::(\*|\d+))?(/.*)?")
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}


def match_pattern(pattern: str, url: str) -> bool:
    """Chrome's match patterns ("<all_urls>", "*://*.example.com/path*") - also accepts a bare origin pattern. A pattern
    with a port matches that port only (the scheme's default port when the URL names none); without one, any port."""
    if pattern == "<all_urls>":
        return re.match(r"^(https?|wss?|ftp|file|data|urn):", url) is not None
    m = MATCH_PATTERN.fullmatch(pattern)
    target = QUrl(url)
    if m is None or not target.isValid():
        return False
    scheme, host, port, path = m.group(1), m.group(2).lower(), m.group(3), m.group(4) or "/*"
    if not host and scheme != "file":  # "https:///*": malformed - Chrome grants nothing for it
        return False
    if (scheme == "*" and target.scheme() not in ("http", "https", "ws", "wss")) or (scheme != "*" and scheme != target.scheme()):
        return False
    have = target.host().lower()
    if host.startswith("*.") and not (have == host[2:] or have.endswith(host[1:])):
        return False
    if host not in ("*", "") and not host.startswith("*.") and host != have:
        return False
    if port not in (None, "*") and int(port) != target.port(DEFAULT_PORTS.get(target.scheme(), -1)):
        return False
    full = target.path(QUrl.ComponentFormattingOption.FullyEncoded) or "/"
    if target.hasQuery():
        full += "?" + target.query(QUrl.ComponentFormattingOption.FullyEncoded)
    return wildcard_match(path, full)


class PatternSet:
    """Many match patterns tried on a URL at once: only those for its host (or any host) are looked at - a list of
    thousands (uBlock Origin Lite registers such lists) costs no more than a few."""

    def __init__(self, patterns):
        self.anywhere: list[str] = []
        self.hosts: dict[str, list[str]] = {}
        for p in patterns:
            m = MATCH_PATTERN.fullmatch(p) if p != "<all_urls>" else None
            if m is None or m.group(2) in ("*", ""):
                self.anywhere.append(p)
            else:
                self.hosts.setdefault(m.group(2).lower().removeprefix("*."), []).append(p)

    def __bool__(self) -> bool:
        return bool(self.anywhere or self.hosts)

    def matches(self, url: str) -> bool:
        labels = QUrl(url).host().lower().split(".")
        candidates = self.anywhere + [p for i in range(len(labels)) for p in self.hosts.get(".".join(labels[i:]), ())]
        return any(match_pattern(p, url) for p in candidates)


def pattern_contains(outer: str, inner: str) -> bool:
    """Whether match pattern *outer* matches every URL *inner* does (Chrome's URLPattern::Contains): a pattern for
    any subdomain is only in one for any host, or for any subdomain of the same domain or a parent."""
    if outer == inner:
        return True
    parse = MATCH_PATTERN.fullmatch
    if outer == "<all_urls>":
        m = parse(inner)
        return inner != "<all_urls>" and m is not None and m.group(1) in ("*", "http", "https", "ws", "wss", "ftp", "file", "urn")
    o, i = parse(outer), parse(inner)
    if o is None or i is None:
        return False
    if o.group(1) != i.group(1) and not (o.group(1) == "*" and i.group(1) in ("http", "https", "ws", "wss")):
        return False
    oh, ih = o.group(2).lower(), i.group(2).lower()
    if oh != "*":
        if ih == "*":
            return False
        domain = oh[2:] if oh.startswith("*.") else None
        bare = ih[2:] if ih.startswith("*.") else ih
        if domain is None and (ih.startswith("*.") or ih != oh):
            return False
        if domain is not None and not (bare == domain or bare.endswith("." + domain)):
            return False
    if o.group(3) not in (None, "*") and o.group(3) != i.group(3):
        return False
    return wildcard_match(o.group(4) or "/*", i.group(4) or "/*")


def wildcard_match(pattern: str, text: str) -> bool:
    """Whether all of *text* matches *pattern*, where "*" is any run of characters - in linear time (a regular expression
    with a ".*" per "*" can take minutes on a long URL a web page makes up)."""
    pieces = pattern.split("*")
    if len(pieces) == 1:
        return text == pattern
    first, last = pieces[0], pieces[-1]
    if len(text) < len(first) + len(last) or not text.startswith(first) or not text.endswith(last):
        return False
    pos, end = len(first), len(text) - len(last)
    for piece in pieces[1:-1]:  # each as far left as it goes: with only "*", that finds a match if there is one
        at = text.find(piece, pos, end)
        if at < 0:
            return False
        pos = at + len(piece)
    return True


def plain_tip(text: str) -> str:
    """Tool tips guess rich text; keep HTML-looking names (from extensions, web pages) literal."""
    return f"<span>{html.escape(text)}</span>" if "<" in text or "&lt;" in text else text


def ask_question(parent: QWidget | None, title: str, text: str, yes: str = "") -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, title, text,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)  # names come from extensions and web pages: never render them
    if yes:
        box.button(QMessageBox.StandardButton.Yes).setText(yes)
    try:
        answer = box.exec()
    finally:
        if not sip.isdeleted(box):  # (gone with its window: the browser quit while it was open)
            box.deleteLater()
    return not sip.isdeleted(box) and answer == QMessageBox.StandardButton.Yes


def safe_filename(name: str, fallback: str = "download") -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")
    return name[:180] or fallback


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def reveal_in_file_manager(path: Path) -> None:
    """Show a downloaded file selected in Finder / Explorer (or open its folder elsewhere)."""
    try:
        if IS_MAC and path.exists():
            subprocess.Popen(["open", "-R", str(path)])
            return
        if sys.platform == "win32" and path.exists():
            subprocess.Popen(["explorer", "/select,", str(path)])
            return
    except OSError:
        pass
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


def make_button(text: str, primary: bool = False, danger: bool = False) -> QPushButton:
    button = QPushButton(text)
    if primary:
        button.setProperty("primary", True)
    if danger:
        button.setProperty("danger", True)
    button.setAutoDefault(False)
    return button


def tone_label(text: str = "", tone: str = "", wrap: bool = False, rich: bool = False) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)  # never guess
    if tone:
        label.setProperty("tone", tone)
    label.setWordWrap(wrap)
    return label


def run_dialog(dialog: QDialog) -> bool:
    """Show a modal dialog and free it afterwards (dialogs parented to the window would otherwise pile up)."""
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()


def menu_text(text: str) -> str:
    """Menus treat "&" as a keyboard-mnemonic marker; double it so titles like "Q&A" show correctly."""
    return text.replace("&", "&&")


def tool_button(icon_: QIcon, tip: str, size: int = 32) -> QToolButton:
    button = QToolButton()
    button.setIcon(icon_)
    button.setIconSize(QSize(16, 16))
    button.setToolTip(tip)
    button.setAutoRaise(True)
    button.setFixedSize(size, size)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return button


def shortcut_text(seq: QKeySequence | str) -> str:
    return QKeySequence(seq).toString(QKeySequence.SequenceFormat.NativeText)


class Menu(QMenu):
    """A QMenu with rounded, Firefox-like corners where the platform supports translucent pop-ups."""

    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(title, parent)
        if TRANSLUCENT_POPUPS:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
            if sys.platform == "win32":
                self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint)
            self.setProperty("rounded", True)
        self.setToolTipsVisible(True)

    def submenu(self, title: str, icon_: QIcon | None = None) -> "Menu":
        sub = Menu(title, self)
        if icon_ is not None:
            sub.setIcon(icon_)
        self.addMenu(sub)
        return sub


def reset_menu(menu: QMenu) -> None:
    """Empty a menu that is rebuilt every time it opens, including the sub-menus created last time."""
    menu.clear()
    for child in menu.findChildren(QMenu, options=Qt.FindChildOption.FindDirectChildrenOnly):
        child.deleteLater()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Chrome extension packages (.crx) and manifests
# ══════════════════════════════════════════════════════════════════════════════════════════
class InstallError(Exception):
    pass


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise InstallError("The extension file is damaged (truncated header).")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise InstallError("The extension file is damaged (bad header).")


def _proto_fields(buf: bytes):
    """Minimal protobuf reader - enough to read the CRX3 header."""
    pos = 0
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire == 1:
            value, pos = buf[pos:pos + 8], pos + 8
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            if pos + length > len(buf):
                raise InstallError("The extension file is damaged (truncated field).")
            value, pos = buf[pos:pos + length], pos + length
        elif wire == 5:
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            raise InstallError("The extension file is damaged (unknown field).")
        yield number, wire, value


def extension_id_from_key(public_key: bytes) -> str:
    digest = hashlib.sha256(public_key).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


def manifest_key_bytes(value) -> bytes:
    """A manifest's "key" as Chrome reads it (Extension::ParsePEMKeyBytes): base64, optionally wrapped in
    "-----BEGIN ... KEY-----" / "-----END ... KEY-----" lines. Raises ValueError for anything else - such a key never
    gets an ID of its own choosing."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("empty key")
    text = value.strip()
    if text.startswith("-----BEGIN"):
        m = re.fullmatch(r"-----BEGIN[^\n]*?KEY-----(.*?)-----END[^\n]*", text, re.S)
        if m is None:
            raise ValueError("malformed PEM key")
        text = m.group(1)
    text = re.sub(r"\s+", "", text)
    if not text:
        raise ValueError("empty key")
    try:
        key = base64.b64decode(text, validate=True)
    except ValueError as exc:  # (binascii.Error is one)
        raise ValueError(f"not base64: {exc}") from exc
    if not key:
        raise ValueError("empty key")
    return key


def _der(buf: bytes, pos: int = 0) -> tuple[int, bytes, int]:
    """One DER element: (tag, contents, position after it)."""
    if pos + 2 > len(buf):
        raise ValueError("truncated DER")
    tag, length, pos = buf[pos], buf[pos + 1], pos + 2
    if length & 0x80:
        count = length & 0x7F
        if not 0 < count <= 4 or pos + count > len(buf):
            raise ValueError("bad DER length")
        length, pos = int.from_bytes(buf[pos:pos + count], "big"), pos + count
    if pos + length > len(buf):
        raise ValueError("truncated DER")
    return tag, buf[pos:pos + length], pos + length


def _spki(spki: bytes) -> tuple[bytes, bytes, bytes]:
    """SubjectPublicKeyInfo -> (algorithm OID, parameters, key bits)."""
    tag, body, _ = _der(spki)
    tag_alg, alg, pos = _der(body)
    tag_bits, bits, _ = _der(body, pos)
    tag_oid, oid, pos = _der(alg)
    params = _der(alg, pos)[1] if pos < len(alg) else b""
    if (tag, tag_alg, tag_bits, tag_oid) != (0x30, 0x30, 0x03, 0x06) or not bits or bits[0] != 0:
        raise ValueError("not a public key")
    return oid, params, bits[1:]


_DIGEST_INFO = {"sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
                "sha1": bytes.fromhex("3021300906052b0e03021a05000414")}
_RSA_OID, _EC_OID, _P256_OID = (bytes.fromhex("2a864886f70d010101"), bytes.fromhex("2a8648ce3d0201"),
                                bytes.fromhex("2a8648ce3d030107"))
_P256 = (0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff,       # p
         0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551,       # n
         0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b,       # b
         (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
          0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5))      # G


def _ec_add(a, b):
    p = _P256[0]
    if a is None or b is None:
        return a or b
    if a[0] == b[0] and (a[1] + b[1]) % p == 0:
        return None
    if a == b:
        lam = 3 * (a[0] * a[0] - 1) * pow(2 * a[1], -1, p) % p   # curve a = -3
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, p) % p
    x = (lam * lam - a[0] - b[0]) % p
    return x, (lam * (a[0] - x) - a[1]) % p


def _ec_mul(k: int, point):
    result = None
    while k:
        if k & 1:
            result = _ec_add(result, point)
        point, k = _ec_add(point, point), k >> 1
    return result


def verify_signature(spki: bytes, signature: bytes, message: bytes, hash_name: str = "sha256") -> bool:
    """RSA PKCS#1 v1.5 or ECDSA P-256 signature check (all a .crx needs), without extra packages."""
    try:
        oid, params, key = _spki(spki)
        digest = hashlib.new(hash_name, message).digest()
        if oid == _RSA_OID:
            tag, body, _ = _der(key)
            tag_n, n_raw, pos = _der(body)
            tag_e, e_raw, _ = _der(body, pos)
            n, e = int.from_bytes(n_raw, "big"), int.from_bytes(e_raw, "big")
            size = (n.bit_length() + 7) // 8
            info = _DIGEST_INFO[hash_name] + digest
            if (tag, tag_n, tag_e) != (0x30, 2, 2) or len(signature) != size or size < len(info) + 11:
                return False
            decoded = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
            return hmac.compare_digest(decoded, b"\x00\x01" + b"\xff" * (size - len(info) - 3) + b"\x00" + info)
        if oid == _EC_OID and params == _P256_OID and len(key) == 65 and key[0] == 4:
            p, n, b, g = _P256
            q = (int.from_bytes(key[1:33], "big"), int.from_bytes(key[33:], "big"))
            if (q[1] * q[1] - q[0] ** 3 + 3 * q[0] - b) % p:
                return False
            tag, body, _ = _der(signature)
            tag_r, r, pos = _der(body)
            tag_s, s, _ = _der(body, pos)
            r, s = int.from_bytes(r, "big"), int.from_bytes(s, "big")
            if (tag, tag_r, tag_s) != (0x30, 2, 2) or not (0 < r < n and 0 < s < n):
                return False
            w = pow(s, -1, n)
            point = _ec_add(_ec_mul(int.from_bytes(digest, "big") * w % n, g), _ec_mul(r * w % n, q))
            return point is not None and point[0] % n == r
    except (ValueError, KeyError, ZeroDivisionError):
        pass
    return False


def parse_crx(data: bytes) -> tuple[bytes, bytes | None]:
    """Return (zip archive, developer public key) for a signed .crx (v2/v3) - or (data, None) for a plain zip.

    The key decides the extension's ID, so - like Chrome - only a package whose signature by that key verifies
    is accepted (otherwise any file could claim to be, and silently replace, an installed extension)."""
    if data[:4] != b"Cr24":
        if data[:2] == b"PK":
            return data, None
        raise InstallError("This file isn't a Chrome extension (.crx or .zip).")
    version = int.from_bytes(data[4:8], "little")
    if version == 2:
        key_len = int.from_bytes(data[8:12], "little")
        sig_len = int.from_bytes(data[12:16], "little")
        key, signature = data[16:16 + key_len], data[16 + key_len:16 + key_len + sig_len]
        archive = data[16 + key_len + sig_len:]
        if len(data) < 16 + key_len + sig_len or not verify_signature(key, signature, archive, "sha1"):
            raise InstallError("The extension file's signature doesn't verify, so it can't be installed.")
        return archive, key
    if version == 3:
        header_len = int.from_bytes(data[8:12], "little")
        header = data[12:12 + header_len]
        proofs: list[tuple[bytes, bytes]] = []
        signed = crx_id = None
        for number, wire, value in _proto_fields(header):
            if wire != 2:
                continue
            if number in (2, 3):  # sha256_with_rsa / sha256_with_ecdsa: AsymmetricKeyProof {public_key, signature}
                fields = {n: v for n, w, v in _proto_fields(value) if w == 2}
                proofs.append((fields.get(1, b""), fields.get(2, b"")))
            elif number == 10000:  # signed header data -> crx_id
                signed = value
                crx_id = next((v for n, w, v in _proto_fields(value) if n == 1 and w == 2), None)
        archive = data[12 + header_len:]
        message = b"CRX3 SignedData\x00" + len(signed or b"").to_bytes(4, "little") + (signed or b"") + archive
        key = next((k for k, sig in proofs if crx_id is not None and hashlib.sha256(k).digest()[:16] == crx_id
                    and verify_signature(k, sig, message)), None)
        if key is None:
            raise InstallError("The extension file's signature doesn't verify, so it can't be installed.")
        return archive, key
    raise InstallError(f"Unsupported .crx version ({version}).")


def _strip_json_comments(text: str) -> str:
    out: list[str] = []
    i, n, in_string = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def load_manifest(directory: Path) -> dict:
    raw = (directory / "manifest.json").read_bytes().decode("utf-8-sig")
    try:
        data = json.loads(raw)
    except ValueError:  # Chrome tolerates comments in manifest.json (not trailing commas)
        data = json.loads(_strip_json_comments(raw))
    if not isinstance(data, dict):
        raise ValueError("manifest.json does not contain an object")
    return data


def localized(directory: Path, manifest: dict, value) -> str:
    value = value if isinstance(value, str) else ""
    match = re.fullmatch(r"__MSG_(\w+)__", value)
    if not match:
        return value
    key = match.group(1).lower()
    for locale in dict.fromkeys([manifest.get("default_locale") or "en", "en", "en_US"]):
        try:
            messages = json.loads((directory / "_locales" / str(locale) / "messages.json").read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError):
            continue
        for name, entry in messages.items() if isinstance(messages, dict) else ():
            if name.lower() == key and isinstance(entry, dict) and entry.get("message"):
                return str(entry["message"])
    return value


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    root, total = target.resolve(), 0
    members = archive.infolist()
    for member in members:
        member.filename = member.filename.replace("\\", "/")  # zips made on Windows
        destination = (root / member.filename).resolve()
        if destination != root and root not in destination.parents:
            raise InstallError("The extension archive contains unsafe file paths.")
        total += member.file_size
        if total > MAX_UNPACKED_SIZE or (member.file_size > 10 * 1024 * 1024 and member.file_size > 200 * max(1, member.compress_size)):
            raise InstallError("The extension package is too large to install.")
    for member in members:
        archive.extract(member, root)


# ── Foxglove's chrome.* polyfill, wired into every extension at install time ───────────────────────
# Qt WebEngine gives extensions only runtime, storage, i18n, management and tabs.update, and several calls it does
# have crash or hang the browser. The shim below runs first in each extension context and fills the gaps; calls
# that need the browser go to Foxglove over the foxglove-ext:// scheme (ExtensionBridge).
SHIM_VERSION = 1
SHIM_FILE, SHIM_WORKER, SHIM_BRIDGE, SHIM_ORIGINAL = (
    "foxglove-shim.js", "foxglove-worker.js", "foxglove-bridge.html", "foxglove-manifest.json")
SHIM_TAG = f'<script src="/{SHIM_FILE}"></script>'
SHIM_CFG_MARK = "/*FOXGLOVE_CFG*/"
EXTENSION_SHIM_JS = r"""/* foxglove-shim %(stamp)s
   Foxglove fills in the chrome.* APIs that Qt WebEngine lacks or gets wrong. Written at install time; runs first in
   the service worker, in every extension page and in front of every content script. */
(() => {
  "use strict";
  const g = globalThis;
  if (g.__foxgloveShim || typeof chrome !== "object" || !chrome || !chrome.runtime || !chrome.runtime.id) return;
  Object.defineProperty(g, "__foxgloveShim", { value: true });
  const CFG = /*FOXGLOVE_CFG*/{}/*END*/;
  const rt = chrome.runtime, ID = rt.id, ORIGIN = "chrome-extension://" + ID;
  const isSW = typeof ServiceWorkerGlobalScope === "function" && g instanceof ServiceWorkerGlobalScope;
  const isPage = !isSW && typeof location === "object" && location.origin === ORIGIN;
  const isCS = !isSW && !isPage;
  const isBridge = isPage && location.pathname === "/foxglove-bridge.html";
  const inTab = isCS || (isPage && !isBridge);  // may be in a tab (an extension page in a pop-up or offscreen isn't)
  const FRAME = inTab ? (g === g.top ? 0 : -1) : undefined;
  const rtSend = rt.sendMessage.bind(rt), getURL = rt.getURL.bind(rt);
  // Foxglove's own messages among the extension's: marked with a value only the extension's polyfill knows (an object
  // the extension passes on - from a server, another extension - is never taken for one)
  const EV = "event:" + (CFG.mark || ""), CSM = "cs-msg:" + (CFG.mark || ""), PORT = "__fg" + (CFG.mark || "");
  const nop = () => undefined;
  const put = (obj, members) => {
    for (const [k, v] of Object.entries(members)) {
      try { obj[k] = v; } catch (err) { /* read-only */ }
      if (obj[k] !== v) { try { Object.defineProperty(obj, k, { value: v, configurable: true, writable: true, enumerable: true }); } catch (err) { /* keep it */ } }
    }
    return obj;
  };
  const ns = (name) => (chrome[name] && typeof chrome[name] === "object" ? chrome[name] : (chrome[name] = {}));
  const has = (perm) => (CFG.permissions || []).includes(perm) || (CFG.optional || []).includes(perm);
  const url = (p) => (typeof p === "string" && p && !/^[a-z][a-z0-9+.-]*:/i.test(p) ? getURL(p.replace(/^\//, "")) : p);

  // ── which tab this is: Foxglove's script in the tab answers a DOM event named for this extension alone - and
  //    only an answer that arrives while we ask counts (DOM events reach every world of a page) ──
  let myTab, asking = false;
  const tabId = () => {
    if (myTab === undefined && inTab && CFG.tabQuery && typeof document === "object") {
      asking = true;
      try { document.dispatchEvent(new CustomEvent(CFG.tabQuery)); } finally { asking = false; }
    }
    return myTab;
  };
  if (inTab && CFG.tabAnswer && typeof document === "object") {
    document.addEventListener(CFG.tabAnswer, (e) => { if (asking && myTab === undefined) myTab = Number(e.detail); });
  }

  // ── calls to Foxglove: fetch() to its scheme (pages and the worker are known by their origin, content scripts by a token) ──
  const unwrap = (r) => { if (r && r.ok) return r.value; throw new Error((r && r.error) || "Chrome 2 couldn't complete the request."); };
  const post = g.fetch.bind(g), json = JSON.stringify;  // the originals: page code may replace them later
  const PART = 12000;  // Qt garbles request bodies over 16 KiB: bigger calls go in parts, as ASCII so a part is never cut mid-character
  const ascii = (s) => s.replace(/[\u007f-\uffff]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
  const send = (body, part) => post(CFG.bridge + (part ? "?part=" + part : ""), { method: "POST", body }).then((r) => r.json()).then(unwrap);
  const call = async (api, args) => {
    const body = ascii(json(isCS ? { api, args, token: CFG.token, tab: tabId(), frame: FRAME } : { api, args, from: isSW ? "" : location.href }));
    if (body.length <= PART) return send(body);
    const id = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2), n = Math.ceil(body.length / PART);
    const auth = (isCS ? CFG.token : "") + "\n";  // Foxglove only keeps parts for callers it knows
    for (let i = 0; i < n - 1; i++) await send(auth + body.slice(i * PART, (i + 1) * PART), `${id}.${i}.${n}`);
    return send(auth + body.slice((n - 1) * PART), `${id}.${n - 1}.${n}`);
  };
  const chains = {};  // calls whose order matters (a menu item before its children, the last badge text wins)
  const ordered = (api, args) => {
    const key = api.split(".")[0], next = (chains[key] || Promise.resolve()).then(() => call(api, args));
    chains[key] = next.catch(nop);
    return next;
  };

  // ── callbacks or promises, with chrome.runtime.lastError for callbacks ──
  const withError = (message, cb, ...args) => {
    const d = Object.getOwnPropertyDescriptor(rt, "lastError");
    try { Object.defineProperty(rt, "lastError", { configurable: true, enumerable: true, get: () => ({ message }) }); } catch (err) { /* keep */ }
    try { cb(...args); } finally {
      try { if (d) Object.defineProperty(rt, "lastError", d); else delete rt.lastError; } catch (err) { /* keep */ }
    }
  };
  const fn = (impl) => function (...args) {
    const cb = typeof args[args.length - 1] === "function" ? args.pop() : null;
    let p;  // started right away: a page that closes next (a pop-up saving its settings) must not lose the call
    try { p = Promise.resolve(impl(...args)); } catch (err) { p = Promise.reject(err); }
    if (!cb) return p;
    p.then((v) => cb(v), (err) => withError(String((err && err.message) || err), cb));
  };

  // ── events: Foxglove's (delivered through its bridge page) and the ones emulated here ──
  const events = new Map(), told = new Set();
  const listen = (name) => {  // storage: only the worker's onChanged (a change in a page must wake it, as in Chrome)
    if (isCS || isBridge || told.has(name) || (name.startsWith("storage.") && !(isSW && /^storage\.(\w+\.)?onChanged$/.test(name)))) return;
    told.add(name);
    call("events.listen", { name, worker: isSW }).catch(nop);
  };
  const event = (name) => {
    if (!events.has(name)) {
      const set = new Set();
      events.set(name, { set, api: {
        addListener(f) { if (typeof f === "function") { set.add(f); listen(name); } },
        removeListener(f) { set.delete(f); },
        hasListener(f) { return set.has(f); },
        hasListeners() { return set.size > 0; },
        addRules: fn(() => []), getRules: fn(() => []), removeRules: fn(nop),
      } });
    }
    return events.get(name).api;
  };
  const emit = (name, args) => {
    const e = events.get(name);
    for (const f of e ? [...e.set] : []) { try { f(...(args || [])); } catch (err) { console.error(err); } }
  };
  const seen = new Map();  // eid -> when: an event can come twice (the channel and Foxglove) - but not a minute apart
  const deliver = (m) => {
    if (!m || m.__fg !== EV) return;
    if (m.eid) {
      if (seen.has(m.eid)) return;
      const now = Date.now();
      seen.set(m.eid, now);
      if (seen.size > 256) for (const [k, t] of seen) { if (now - t < 60000 && seen.size <= 8192) break; seen.delete(k); }
    }
    emit(m.name, m.args);
  };
  const channel = !isCS && typeof BroadcastChannel === "function" ? new BroadcastChannel("__foxglove") : null;
  if (channel && !isBridge) channel.onmessage = (e) => deliver(e.data);
  if (isSW) g.addEventListener("message", (e) => { if (e.data && e.data.__fg === EV) { e.stopImmediatePropagation(); deliver(e.data); } });
  let restored = null;  // the bridge page putting back storage.session after a reload of Foxglove's: events wait for it
  if (isBridge) {  // Foxglove -> the extension: other pages hear the channel, the worker gets (and is woken by) a message
    g.__foxgloveEmit = async (name, args, eid, workerOnly) => {
      if (restored) await restored;
      const m = { __fg: EV, name, args, eid: eid || Math.random().toString(36).slice(2) };
      if (!workerOnly) channel.postMessage(m);
      for (let i = 0; CFG.worker && i < 40; i++) {
        const reg = await navigator.serviceWorker.getRegistration();
        const w = reg && (reg.active || reg.waiting || reg.installing);
        if (w) { w.postMessage(m); return true; }
        await new Promise((r) => setTimeout(r, 250));
      }
      return false;
    };
  }

  // ── runtime.onMessage / onConnect: hide Foxglove's own traffic, give content-script senders their tab ──
  const tabOf = (sender, m) => (sender.tab || typeof m.tab !== "number" || m.tab < 0 ? sender : { ...sender, frameId: m.frame,
    tab: { id: m.tab, index: -1, windowId: 1, url: sender.url, title: m.title, active: true, highlighted: true, selected: true,
      pinned: false, incognito: false, status: "complete", audible: false, discarded: false, autoDiscardable: true, groupId: -1,
      mutedInfo: { muted: false } } });
  // A listener may also answer by returning a promise (newer Chrome). One that settles without a value only closes
  // the channel if no other listener still means to answer (returned true or a promise of its own).
  const replies = new WeakMap();  // sendResponse (one per message) -> {open, kept, done, send}
  const invoke = (f, msg, sender, respond) => {
    let st = replies.get(respond);
    if (!st) {
      st = { open: 0, kept: false, done: false };
      st.send = (v) => { if (!st.done) { st.done = true; respond(v); } };
      replies.set(respond, st);
    }
    const r = f(msg, sender, st.send);
    if (r === true) { st.kept = true; return true; }
    if (!r || typeof r.then !== "function") return r;
    st.open++;
    const settle = (v) => { st.open--; if (v !== undefined) st.send(v); else if (!st.open && !st.kept) st.send(undefined); };
    r.then(settle, () => settle(undefined));
    return true;
  };
  const onMsg = rt.onMessage, nmAdd = onMsg.addListener.bind(onMsg), nmRemove = onMsg.removeListener.bind(onMsg), nmHas = onMsg.hasListener.bind(onMsg);
  const wrapped = new WeakMap(), csListeners = new Set();
  put(onMsg, {
    addListener(f) {
      if (typeof f !== "function" || wrapped.has(f)) return;
      const w = (m, sender, respond) => (m && typeof m === "object" && (m.__fg === CSM || m.__fg === EV)
        ? (m.__fg === CSM ? invoke(f, m.msg, tabOf(sender, m), respond) : undefined) : invoke(f, m, sender, respond));
      wrapped.set(f, w);
      if (isCS) csListeners.add(f);
      nmAdd(w);
    },
    removeListener(f) { nmRemove(wrapped.get(f) || f); wrapped.delete(f); csListeners.delete(f); },
    hasListener(f) { return nmHas(wrapped.get(f) || f); },
  });
  if (!isCS) nmAdd((m) => { if (m && m.__fg === EV) deliver(m); return undefined; });
  const onConnect = rt.onConnect;
  if (onConnect && !isCS) {
    const ocAdd = onConnect.addListener.bind(onConnect), ocRemove = onConnect.removeListener.bind(onConnect), ocWrapped = new WeakMap();
    put(onConnect, {
      addListener(f) {
        if (typeof f !== "function" || ocWrapped.has(f)) return;
        const w = (port) => {
          const m = (port.name || "").startsWith(PORT) ? /^([^|]*)\|/.exec(port.name.slice(PORT.length)) : null;
          if (m) {
            try {
              const [tab, frame, title] = JSON.parse(decodeURIComponent(m[1]));
              Object.defineProperty(port, "name", { value: port.name.slice(PORT.length + m[0].length), configurable: true });
              if (port.sender) Object.defineProperty(port, "sender", { value: tabOf(port.sender, { tab, frame, title }), configurable: true });
            } catch (err) { /* leave the port as it is */ }
          }
          return f(port);
        };
        ocWrapped.set(f, w);
        ocAdd(w);
      },
      removeListener(f) { ocRemove(ocWrapped.get(f) || f); ocWrapped.delete(f); },
    });
  }

  // ── chrome.i18n (Qt: always "", and the first call can stall the extension for a minute) ──
  const ui = CFG.uiLocale || "en_US", messages = CFG.messages || {};
  const predefined = { "@@extension_id": ID, "@@ui_locale": ui, "@@bidi_dir": "ltr", "@@bidi_reversed_dir": "rtl",
    "@@bidi_start_edge": "left", "@@bidi_end_edge": "right" };
  if (chrome.i18n) {
    put(chrome.i18n, {
      getMessage(name, subs) {
        const key = String(name).toLowerCase(), msg = key in predefined ? predefined[key] : messages[key];
        if (msg === undefined) return "";
        const list = subs === undefined || subs === null ? [] : Array.isArray(subs) ? subs : [subs];
        return msg.replace(/\$(\$|[1-9])/g, (_, d) => (d === "$" ? "$" : list[d - 1] === undefined ? "" : String(list[d - 1])));
      },
      getUILanguage: () => ui.replace("_", "-"),
      getAcceptLanguages: fn(() => [...new Set([ui.replace("_", "-"), ui.split("_")[0]])]),
      detectLanguage: fn(() => ({ isReliable: false, languages: [] })),
    });
  }
  put(rt, { getManifest: ((native) => () => Object.assign(native(), CFG.manifest || {}))(rt.getManifest.bind(rt)) });

  // ── chrome.storage: sync/managed (Qt: "not available") and onChanged (Qt: never fires) ──
  const S = chrome.storage, SRC = Math.random().toString(36).slice(2);
  let seq = 0;
  if (S && S.local) {
    const L = S.local, PFX = "__foxglove_sync__:";
    const isSync = (k) => k.startsWith(PFX);
    const empty = (o) => Object.keys(o).length === 0;
    const dispatch = (area, changes) => {
      if (empty(changes)) return;
      emit("storage.onChanged", [changes, area]);
      emit(`storage.${area}.onChanged`, [changes]);
    };
    // Foxglove passes changes on to content scripts in tabs, and to a stopped worker that listens (a page's change wakes it)
    const tells = (area) => ((CFG.cs || isCS) && area !== "session") || (isPage && CFG.worker);
    const early = (area, what) => {  // before the write: a page that closes right after it (a pop-up) never gets to announce()
      const eid = SRC + ++seq, sent = (isCS || (isPage && !isBridge)) && tells(area);
      if (sent) call("storage.pending", { area, src: SRC, eid, ...what }).catch(nop);
      return [eid, sent];
    };
    const announce = (area, changes, [eid, sent]) => {
      if (!empty(changes)) {
        dispatch(area, changes);
        const m = { __fg: EV, name: "storage.changed", args: [area, changes, SRC], eid };
        if (channel) channel.postMessage(m);  // open pages, a running worker
        if (isCS) rtSend(m).catch(nop);  // wakes the worker
      }
      // (the same eid: a worker that has it already drops it; an empty change settles what early() said)
      if (tells(area) && (sent || !empty(changes))) call("storage.changed", { area, changes, src: SRC, eid }).catch(nop);
    };
    // a write that failed changed nothing: what early() said is taken back (an empty change)
    const settled = (area, told, p) => p.catch((err) => { announce(area, {}, told); throw err; });
    event("storage.changed").addListener((area, changes, src) => { if (src !== SRC) dispatch(area, changes); });
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const diff = (old, items) => {
      const out = {};
      for (const [k, v] of Object.entries(items)) {
        if (v === undefined) continue;
        if (!(k in old)) out[k] = { newValue: v }; else if (!same(old[k], v)) out[k] = { oldValue: old[k], newValue: v };
      }
      return out;
    };
    const gone = (old) => Object.fromEntries(Object.entries(old).map(([k, v]) => [k, { oldValue: v }]));
    const area = (name, n, wrap, unwrapKeys, mine) => {
      const get = async (keys) => {
        if (keys === null || keys === undefined) return unwrapKeys(await n.get(null));
        if (typeof keys === "string") keys = [keys];
        if (Array.isArray(keys)) return unwrapKeys(await n.get(keys.map(wrap)));
        return Object.assign({}, keys, unwrapKeys(await n.get(Object.keys(keys).map(wrap))));
      };
      const all = async () => Object.keys(await n.get(null)).filter(mine);
      return {
        get: fn(get),
        set: fn(async (items) => {  // the write goes out at once, beside the read of the old values (a page may close next)
          items = items || {};
          const told = early(name, { set: items });
          const [old] = await settled(name, told, Promise.all([get(Object.keys(items)), n.set(Object.fromEntries(Object.entries(items).map(([k, v]) => [wrap(k), v])))]));
          announce(name, diff(old, items), told);
        }),
        remove: fn(async (keys) => {
          keys = [].concat(keys);
          const told = early(name, { remove: keys });
          const [old] = await settled(name, told, Promise.all([get(keys), n.remove(keys.map(wrap))]));
          announce(name, gone(old), told);
        }),
        clear: fn(async () => { const old = await get(null); await n.remove(await all()); announce(name, gone(old), [SRC + ++seq, false]); }),
        getKeys: fn(async () => Object.keys(await get(null))),
        getBytesInUse: fn(async (keys) => new Blob([JSON.stringify(await get(keys === undefined ? null : keys))]).size),
        setAccessLevel: fn(nop),
        onChanged: event(`storage.${name}.onChanged`),
      };
    };
    const native = (o) => ({ get: o.get.bind(o), set: o.set.bind(o), remove: o.remove.bind(o) });
    const access = S.session && typeof S.session.setAccessLevel === "function" ? S.session.setAccessLevel.bind(S.session) : null;
    const local = native(L);
    put(L, { ...area("local", local, (k) => k, (o) => Object.fromEntries(Object.entries(o).filter(([k]) => !isSync(k))), (k) => !isSync(k)),
      QUOTA_BYTES: 10485760 });
    put(S, {
      sync: { ...area("sync", local, (k) => PFX + k, (o) => Object.fromEntries(Object.entries(o).filter(([k]) => isSync(k)).map(([k, v]) => [k.slice(PFX.length), v])), isSync),
        QUOTA_BYTES: 102400, QUOTA_BYTES_PER_ITEM: 8192, MAX_ITEMS: 512, MAX_WRITE_OPERATIONS_PER_HOUR: 1800, MAX_WRITE_OPERATIONS_PER_MINUTE: 120 },
      managed: { get: fn(async (keys) => (keys && typeof keys === "object" && !Array.isArray(keys) ? { ...keys } : {})),
        getKeys: fn(() => []), getBytesInUse: fn(() => 0), onChanged: event("storage.managed.onChanged") },
      onChanged: event("storage.onChanged"),
    });
    if (S.session && isBridge) {  // Foxglove keeps it across its own reloads of the extension (no onChanged: nothing changed)
      const raw = native(S.session);
      g.__foxgloveSession = { get: () => raw.get(null), restore: (data) => (restored = raw.set(data).catch(nop)) };
    }
    if (isBridge) {  // what is stored: a write announced early is only passed on by Foxglove once it is really there
      const stores = { local: [local, (k) => k], sync: [local, (k) => PFX + k], ...(S.session ? { session: [native(S.session), (k) => k] } : {}) };
      g.__foxgloveRead = async (name, keys) => {
        const [store, key] = stores[name] || [];
        if (!store) return null;
        const got = await store.get(keys.map(key));
        return Object.fromEntries(keys.filter((k) => key(k) in got).map((k) => [k, got[key(k)]]));
      };
    }
    if (S.session && !isCS) {  // setAccessLevel stays Qt's: it lets content scripts use storage.session
      put(S.session, { ...area("session", native(S.session), (k) => k, (o) => o, () => true), QUOTA_BYTES: 10485760,
        ...(access ? { setAccessLevel: access } : {}) });
    }
  }

  // ── messages from a tab (content scripts; extension pages opened in one) carry it: Qt leaves out sender.tab ──
  if (inTab) {
    const info = () => ({ tab: tabId(), frame: FRAME, title: typeof document === "object" ? document.title : "" });
    const sendMessage = function (...args) {
      const cb = typeof args[args.length - 1] === "function" ? args.pop() : null;
      if (args.length >= 2 && typeof args[0] === "string" && /^[a-p]{32}$/.test(args[0])) {
        if (args[0] !== ID) return rtSend(...args, ...(cb ? [cb] : []));  // another extension: untouched
        args.shift();
      }
      const i = info();
      if (!isCS && typeof i.tab !== "number") return rtSend(...args, ...(cb ? [cb] : []));
      return rtSend({ __fg: CSM, msg: args[0], ...i }, ...(cb ? [cb] : []));
    };
    const connect = rt.connect.bind(rt);
    put(rt, { sendMessage, connect: function (...args) {
      if (typeof args[0] === "string" && args[0] !== ID) return connect(...args);
      const opts = (typeof args[0] === "string" ? args[1] : args[0]) || {}, name = opts.name || "", i = info();
      if (!isCS && typeof i.tab !== "number") return connect(...args);
      const port = connect({ ...opts, name: PORT + encodeURIComponent(JSON.stringify([i.tab, i.frame, i.title])) + "|" + name });
      try { Object.defineProperty(port, "name", { value: name, configurable: true }); } catch (err) { /* keep it */ }
      return port;
    } });
  }

  // ── content scripts: tabs.sendMessage() from the extension, web-accessible files ──
  if (isCS) {
    if (!chrome.extension) put(chrome, { extension: { inIncognitoContext: false, getURL: (p) => rt.getURL(p) } });
    if (CFG.war && CFG.war.length) {  // Qt refuses chrome-extension:// files in web pages: Foxglove serves them itself
      const pats = CFG.war.map((w) => new RegExp("^" + w.replace(/^\//, "").replace(/[.+^${}()|[\]\\?]/g, "\\$&").replace(/\*/g, ".*") + "$"));
      put(rt, { getURL: (p) => {  // "": the origin of the extension's frames in the page (postMessage's targetOrigin)
        const path = String(p).replace(/^\//, "");
        return !path || pats.some((r) => r.test(path.split(/[?#]/)[0])) ? `${CFG.scheme}://${ID}/${path}` : getURL(p);
      } });
    }
    if (CFG.relay && typeof document === "object") {
      document.addEventListener(CFG.relay, (e) => {
        let m;
        try { m = JSON.parse(e.detail); } catch (err) { return; }
        if (!m) return;
        if (m.event) { deliver({ __fg: EV, name: m.event, args: m.args, eid: m.eid }); return; }
        if ((m.frameId !== undefined && m.frameId !== null && m.frameId !== FRAME) || !csListeners.size) return;
        e.preventDefault();  // tells Foxglove there is a receiving end
        let done = false, keep = false;
        const respond = (value) => { if (!done) { done = true; call("tabs.reply", { callId: m.callId, value }).catch(nop); } };
        const sender = { id: ID, origin: ORIGIN, url: m.from || ORIGIN + "/" };
        for (const f of [...csListeners]) {
          try { if (invoke(f, m.msg, sender, respond) === true) keep = true; } catch (err) { console.error(err); }
        }
        if (!keep) respond(undefined);
      });
    }
    return;
  }
  if (isBridge) return;

  // ── everything below: the service worker and extension pages ──
  if (!chrome.extension) {
    chrome.extension = { inIncognitoContext: false, getURL: (p) => getURL(p), getViews: () => [], getBackgroundPage: () => null,
      isAllowedIncognitoAccess: fn(() => false), isAllowedFileSchemeAccess: fn(() => false), setUpdateUrlData: nop };
  }
  put(rt, {
    openOptionsPage: fn(() => call("runtime.openOptionsPage")),
    onInstalled: event("runtime.onInstalled"),  // Qt never fires these (its extensions start disabled)
    onStartup: event("runtime.onStartup"),
    OnInstalledReason: { INSTALL: "install", UPDATE: "update", CHROME_UPDATE: "chrome_update", SHARED_MODULE_UPDATE: "shared_module_update" },
  });
  if (rt.getContexts) {
    const getContexts = rt.getContexts.bind(rt);
    rt.getContexts = fn(async (filter) => {
      const list = await getContexts(filter || {});
      const types = filter && filter.contextTypes;
      const doc = (!types || types.includes("OFFSCREEN_DOCUMENT")) ? await call("offscreen.hasDocument").catch(() => "") : "";
      if (doc) list.push({ contextType: "OFFSCREEN_DOCUMENT", contextId: "foxglove-offscreen", tabId: -1, windowId: -1, frameId: 0,
        documentUrl: doc, documentOrigin: ORIGIN, incognito: false });
      return list;
    });
  }

  // ── chrome.action (Qt: missing) - the toolbar button is Foxglove's ──
  const toPng = async (img) => {
    if (!img || typeof img !== "object" || typeof OffscreenCanvas !== "function") return undefined;
    if (typeof ImageData === "function" && img instanceof ImageData) img = { [img.width]: img };
    const out = {};
    for (const [size, data] of Object.entries(img)) {
      const c = new OffscreenCanvas(data.width, data.height);
      c.getContext("2d").putImageData(data, 0, 0);
      const bytes = new Uint8Array(await (await c.convertToBlob({ type: "image/png" })).arrayBuffer());
      let bin = "";
      for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      out[size] = "data:image/png;base64," + btoa(bin);
    }
    return out;
  };
  const paths = (p) => (typeof p === "string" ? { 0: url(p) } : p && typeof p === "object"
    ? Object.fromEntries(Object.entries(p).map(([k, v]) => [k, url(v)])) : undefined);
  const color = (c) => (Array.isArray(c) ? `rgba(${c[0]},${c[1]},${c[2]},${(c[3] === undefined ? 255 : c[3]) / 255})` : c);
  const tabArg = (d) => (d && typeof d.tabId === "number" ? d.tabId : null);
  const aset = (key, d, value) => ordered("action.set", { key, tabId: tabArg(d), value });
  const getter = (key) => fn((d) => call("action.get", { key, tabId: tabArg(d) }));
  const action = put(ns("action"), {
    setBadgeText: fn((d) => aset("badgeText", d, String((d && d.text) || ""))),
    getBadgeText: getter("badgeText"),
    setBadgeBackgroundColor: fn((d) => aset("badgeBackground", d, color(d && d.color))),
    getBadgeBackgroundColor: getter("badgeBackground"),
    setBadgeTextColor: fn((d) => aset("badgeTextColor", d, color(d && d.color))),
    getBadgeTextColor: getter("badgeTextColor"),
    setTitle: fn((d) => aset("title", d, d && d.title !== undefined && d.title !== null ? String(d.title) : null)),
    getTitle: getter("title"),
    setIcon: fn(async (d) => aset("icon", d, (await toPng(d && d.imageData)) || paths(d && d.path))),
    setPopup: fn((d) => aset("popup", d, d && d.popup ? url(d.popup) : "")),
    getPopup: getter("popup"),
    enable: fn((tabId) => ordered("action.set", { key: "enabled", tabId: typeof tabId === "number" ? tabId : null, value: true })),
    disable: fn((tabId) => ordered("action.set", { key: "enabled", tabId: typeof tabId === "number" ? tabId : null, value: false })),
    isEnabled: fn((tabId) => call("action.get", { key: "enabled", tabId: typeof tabId === "number" ? tabId : null })),
    openPopup: fn(() => call("action.openPopup", {})),
    getUserSettings: fn(() => ({ isOnToolbar: true })),
    onClicked: event("action.onClicked"),
    onUserSettingsChanged: event("action.onUserSettingsChanged"),
  });
  if (!chrome.browserAction) chrome.browserAction = action;

  // ── chrome.contextMenus: items go into Foxglove's page menu ──
  if (has("contextMenus") || has("menus")) {
    let menuSeq = 0;
    const menus = put(ns("contextMenus"), {
      create(props, cb) {
        const { onclick, ...rest } = props || {};
        const id = rest.id !== undefined ? rest.id : `foxglove-${++menuSeq}`;
        if (typeof onclick === "function") event("contextMenus.onClicked").addListener((info, tab) => { if (info.menuItemId === id) onclick(info, tab); });
        ordered("contextMenus.create", { ...rest, id }).then(() => cb && cb(), (err) => (cb ? withError(err.message, cb) : console.warn(err.message)));
        return id;
      },
      update: fn((id, props) => { const { onclick, ...rest } = props || {}; return ordered("contextMenus.update", { id, props: rest }); }),
      remove: fn((id) => ordered("contextMenus.remove", { id })),
      removeAll: fn(() => ordered("contextMenus.removeAll", {})),
      refresh: fn(nop),
      onClicked: event("contextMenus.onClicked"), onShown: event("contextMenus.onShown"), onHidden: event("contextMenus.onHidden"),
      ACTION_MENU_TOP_LEVEL_LIMIT: 6,
      ContextType: { ALL: "all", PAGE: "page", FRAME: "frame", SELECTION: "selection", LINK: "link", EDITABLE: "editable", IMAGE: "image",
        VIDEO: "video", AUDIO: "audio", LAUNCHER: "launcher", BROWSER_ACTION: "browser_action", PAGE_ACTION: "page_action", ACTION: "action" },
      ItemType: { NORMAL: "normal", CHECKBOX: "checkbox", RADIO: "radio", SEPARATOR: "separator" },
    });
    if (!chrome.menus) chrome.menus = menus;
  }

  // ── chrome.notifications: shown as Foxglove notices ──
  if (has("notifications")) {
    const fix = (o) => ({ ...(o || {}), iconUrl: url(o && o.iconUrl), imageUrl: url(o && o.imageUrl) });
    put(ns("notifications"), {
      create: fn(async (id, options) => {
        if (id && typeof id === "object") { options = id; id = ""; }
        id = id || (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));
        await call("notifications.create", { id, options: fix(options) });
        return id;
      }),
      update: fn((id, options) => call("notifications.update", { id, options: fix(options) })),
      clear: fn((id) => call("notifications.clear", { id })),
      getAll: fn(() => call("notifications.getAll", {})),
      getPermissionLevel: fn(() => "granted"),
      onClicked: event("notifications.onClicked"), onClosed: event("notifications.onClosed"),
      onButtonClicked: event("notifications.onButtonClicked"), onPermissionLevelChanged: event("notifications.onPermissionLevelChanged"),
      onShowSettings: event("notifications.onShowSettings"),
      TemplateType: { BASIC: "basic", IMAGE: "image", LIST: "list", PROGRESS: "progress" },
      PermissionLevel: { GRANTED: "granted", DENIED: "denied" },
    });
  }

  // ── chrome.alarms (Qt: present, but alarms never fire): Foxglove's timers ──
  if (has("alarms")) {
    put(ns("alarms"), {
      create: fn((name, info) => { if (name && typeof name === "object") { info = name; name = ""; } return call("alarms.create", { ...(info || {}), name: name || "" }); }),
      get: fn((name) => call("alarms.get", { name: name || "" })),
      getAll: fn(() => call("alarms.getAll", {})),
      clear: fn((name) => call("alarms.clear", { name: name || "" })),
      clearAll: fn(() => call("alarms.clearAll", {})),
      onAlarm: event("alarms.onAlarm"),
    });
  }

  // ── chrome.tabs / chrome.windows (Qt: only tabs.update, which navigates the caller itself) ──
  const port = (name) => {  // tabs.connect(): no connection to content scripts - report it like Chrome does
    const gone = new Set(), stub = { addListener: nop, removeListener: nop, hasListener: () => false, hasListeners: () => false };
    const p = { name: name || "", postMessage: nop, disconnect: nop, onMessage: stub,
      onDisconnect: { ...stub, addListener: (f) => gone.add(f), removeListener: (f) => gone.delete(f), hasListener: (f) => gone.has(f) } };
    setTimeout(() => { for (const f of gone) withError("Could not establish connection. Receiving end does not exist.", f, p); }, 0);
    return p;
  };
  put(ns("tabs"), {
    query: fn((q) => call("tabs.query", q || {})),
    get: fn((tabId) => call("tabs.get", { tabId })),
    getCurrent: fn(() => { const t = tabId(); return typeof t === "number" ? call("tabs.get", { tabId: t }) : call("tabs.getCurrent", {}); }),
    create: fn((p) => call("tabs.create", { ...(p || {}), url: url(p && p.url) })),
    update: fn((tabId, p) => { if (tabId && typeof tabId === "object") { p = tabId; tabId = undefined; } return call("tabs.update", { ...(p || {}), tabId, url: url(p && p.url) }); }),
    remove: fn((ids) => call("tabs.remove", { tabIds: [].concat(ids) })),
    reload: fn((tabId, p) => { if (tabId && typeof tabId === "object") { p = tabId; tabId = undefined; } return call("tabs.reload", { tabId, ...(p || {}) }); }),
    duplicate: fn((tabId) => call("tabs.duplicate", { tabId })),
    move: fn((ids, p) => call("tabs.move", { tabIds: [].concat(ids), ...(p || {}) })),
    highlight: fn((p) => call("tabs.highlight", p || {})),
    discard: fn((tabId) => call("tabs.get", { tabId })),
    goBack: fn((tabId) => call("tabs.navigate", { tabId, step: -1 })),
    goForward: fn((tabId) => call("tabs.navigate", { tabId, step: 1 })),
    captureVisibleTab: fn((windowId, o) => call("tabs.captureVisibleTab", { options: windowId && typeof windowId === "object" ? windowId : o || {} })),
    detectLanguage: fn(() => "und"),
    getZoom: fn((tabId) => call("tabs.getZoom", { tabId })),
    setZoom: fn((tabId, zoomFactor) => { if (zoomFactor === undefined) { zoomFactor = tabId; tabId = undefined; } return call("tabs.setZoom", { tabId, zoomFactor }); }),
    getZoomSettings: fn(() => ({ mode: "automatic", scope: "per-origin", defaultZoomFactor: 1 })),
    setZoomSettings: fn(nop),
    sendMessage: fn((tabId, msg, options) => call("tabs.sendMessage", { tabId, msg, frameId: options && options.frameId })),
    connect: (tabId, info) => port(info && info.name),
    group: fn(() => -1), ungroup: fn(nop),
    onCreated: event("tabs.onCreated"), onUpdated: event("tabs.onUpdated"), onActivated: event("tabs.onActivated"),
    onRemoved: event("tabs.onRemoved"), onReplaced: event("tabs.onReplaced"), onMoved: event("tabs.onMoved"),
    onHighlighted: event("tabs.onHighlighted"), onAttached: event("tabs.onAttached"), onDetached: event("tabs.onDetached"),
    onZoomChange: event("tabs.onZoomChange"), onSelectionChanged: event("tabs.onSelectionChanged"),
    onActiveChanged: event("tabs.onActiveChanged"),
    TAB_ID_NONE: -1, TAB_INDEX_NONE: -1, MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND: 2,
  });
  put(ns("windows"), {
    get: fn((windowId, q) => call("windows.get", { windowId, ...(q || {}) })),
    getCurrent: fn((q) => call("windows.get", { windowId: -2, ...(q || {}) })),
    getLastFocused: fn((q) => call("windows.get", { windowId: -2, ...(q || {}) })),
    getAll: fn((q) => call("windows.getAll", q || {})),
    create: fn((d) => call("windows.create", { ...(d || {}), url: d && d.url ? [].concat(d.url).map(url) : undefined })),
    update: fn((windowId, info) => call("windows.update", { windowId, ...(info || {}) })),
    remove: fn((windowId) => call("windows.remove", { windowId })),
    onCreated: event("windows.onCreated"), onRemoved: event("windows.onRemoved"), onFocusChanged: event("windows.onFocusChanged"),
    onBoundsChanged: event("windows.onBoundsChanged"),
    WINDOW_ID_NONE: -1, WINDOW_ID_CURRENT: -2,
  });

  // ── chrome.scripting: runs in a Foxglove world of the page (DOM access, no chrome.* there) ──
  if (has("scripting")) {
    const css = (inj) => ({ target: inj.target, css: inj.css, files: inj.files });
    put(ns("scripting"), {
      executeScript: fn((inj) => call("scripting.executeScript", { target: inj.target, world: inj.world, files: inj.files,
        func: inj.func ? String(inj.func) : undefined, args: inj.args || [] })),
      insertCSS: fn((inj) => call("scripting.insertCSS", css(inj || {}))),
      removeCSS: fn((inj) => call("scripting.removeCSS", css(inj || {}))),
      registerContentScripts: fn((scripts) => call("scripting.register", { scripts })),
      updateContentScripts: fn((scripts) => call("scripting.update", { scripts })),
      getRegisteredContentScripts: fn((filter) => call("scripting.registered", { filter })),
      unregisterContentScripts: fn((filter) => call("scripting.unregister", { filter })),
      ExecutionWorld: { ISOLATED: "ISOLATED", MAIN: "MAIN" }, StyleOrigin: { AUTHOR: "AUTHOR", USER: "USER" },
    });
  }

  // ── chrome.offscreen (Qt: createDocument crashes the browser): a hidden Foxglove page ──
  if (has("offscreen")) {
    put(ns("offscreen"), {
      createDocument: fn((p) => call("offscreen.createDocument", { url: url(p && p.url) })),
      closeDocument: fn(() => call("offscreen.closeDocument", {})),
      hasDocument: fn(async () => Boolean(await call("offscreen.hasDocument", {}))),
      Reason: new Proxy({}, { get: (t, k) => (typeof k === "string" ? k : undefined) }),
    });
  }

  // ── chrome.declarativeNetRequest (Qt: update* hang, updateSessionRules crashes, rules aren't enforced): Foxglove's ──
  if (chrome.declarativeNetRequest || has("declarativeNetRequest") || has("declarativeNetRequestWithHostAccess")) {
    put(ns("declarativeNetRequest"), {
      updateDynamicRules: fn((o) => call("dnr.update", { ...(o || {}), kind: "dynamic" })),
      updateSessionRules: fn((o) => call("dnr.update", { ...(o || {}), kind: "session" })),
      getDynamicRules: fn((f) => call("dnr.get", { ...(f || {}), kind: "dynamic" })),
      getSessionRules: fn((f) => call("dnr.get", { ...(f || {}), kind: "session" })),
      updateEnabledRulesets: fn((o) => call("dnr.rulesets", o || {})),
      getEnabledRulesets: fn(() => call("dnr.rulesets", {})),
      updateStaticRules: fn(nop), getDisabledRuleIds: fn(() => []),
      setExtensionActionOptions: fn(nop), testMatchOutcome: fn(() => ({ matchedRules: [] })),
      getMatchedRules: fn(() => ({ rulesMatchedInfo: [] })),
      onRuleMatchedDebug: event("declarativeNetRequest.onRuleMatchedDebug"),
    });
  }

  // ── manifest-based answers: permissions, sidePanel, commands; downloads through Foxglove ──
  put(ns("permissions"), {
    getAll: fn(() => call("permissions.getAll", {})),
    contains: fn((p) => call("permissions.contains", p || {})),
    request: fn((p) => (typeof navigator === "object" && navigator.userActivation && navigator.userActivation.isActive
      ? call("permissions.request", p || {}) : Promise.reject(new Error("This function must be called during a user gesture")))),
    remove: fn((p) => call("permissions.remove", p || {})),
    addHostAccessRequest: fn(nop), removeHostAccessRequest: fn(nop),
    onAdded: event("permissions.onAdded"), onRemoved: event("permissions.onRemoved"),
  });
  if (has("sidePanel")) {
    put(ns("sidePanel"), {
      setOptions: fn(nop), getOptions: fn(() => ({ enabled: true, path: CFG.sidePanel || undefined })),
      setPanelBehavior: fn(nop), getPanelBehavior: fn(() => ({ openPanelOnActionClick: false })),
      open: fn(() => call("sidePanel.open", { path: CFG.sidePanel })),
    });
  }
  put(ns("commands"), { getAll: fn(() => CFG.commands || []), onCommand: event("commands.onCommand") });
  if (has("downloads")) {
    put(ns("downloads"), {
      download: fn((o) => call("downloads.download", o || {})),
      search: fn(() => []), pause: fn(nop), resume: fn(nop), cancel: fn(nop), erase: fn(() => []), removeFile: fn(nop),
      open: fn(nop), show: fn(nop), showDefaultFolder: fn(nop), getFileIcon: fn(nop), acceptDanger: fn(nop),
      setShelfEnabled: nop, setUiOptions: fn(nop),
      onCreated: event("downloads.onCreated"), onChanged: event("downloads.onChanged"), onErased: event("downloads.onErased"),
      onDeterminingFilename: event("downloads.onDeterminingFilename"),
    });
  }

  // ── anything else the manifest asks for: harmless stand-ins, so top-level calls don't throw (some calls are real) ──
  const NOT_APIS = ["activeTab", "background", "unlimitedStorage", "nativeMessaging", "webRequestBlocking", "clipboardRead",
    "clipboardWrite", "geolocation", "declarativeNetRequestFeedback", "declarativeNetRequestWithHostAccess", "webRequestAuthProvider"];
  const emptyish = /^(getAll|query|search|getTree|getRecent|getChildren|getSubTree|getAllFrames|getVisits|getDevices|getAllCookieStores)|^get\w*List$/;
  const REAL = {
    webNavigation: { getAllFrames: fn((d) => call("webNavigation.getAllFrames", d || {})), getFrame: fn((d) => call("webNavigation.getFrame", d || {})) },
    fontSettings: { getFontList: fn(() => call("fontSettings.getFontList", {})) },
    cookies: { get: fn((d) => call("cookies.get", d || {})), getAll: fn((d) => call("cookies.getAll", d || {})),
      set: fn((d) => call("cookies.set", d || {})), remove: fn((d) => call("cookies.remove", d || {})),
      getAllCookieStores: fn(() => call("cookies.getAllCookieStores", {})),
      OnChangedCause: { EVICTED: "evicted", EXPIRED: "expired", EXPLICIT: "explicit", EXPIRED_OVERWRITE: "expired_overwrite", OVERWRITE: "overwrite" },
      SameSiteStatus: { NO_RESTRICTION: "no_restriction", LAX: "lax", STRICT: "strict", UNSPECIFIED: "unspecified" } },
  };
  for (const perm of [...(CFG.permissions || []), ...(CFG.optional || [])]) {
    const name = String(perm).split(".")[0];
    if (chrome[name] !== undefined || !/^[a-z][A-Za-z]+$/.test(name) || NOT_APIS.includes(name)) continue;
    chrome[name] = new Proxy({ ...(REAL[name] || {}) }, {
      get(t, k) {
        if (typeof k !== "string") return undefined;
        if (!(k in t)) t[k] = /^on[A-Z]/.test(k) ? event(name + "." + k) : /^[A-Z]/.test(k) ? {} : fn(() => (emptyish.test(k) ? [] : undefined));
        return t[k];
      },
    });
  }
})();
"""


def ui_locale() -> str:
    name = QLocale.system().name()
    return name if re.fullmatch(r"[a-z]{2,3}(_[A-Z]{2})?", name) else "en_US"


def _read_json_file(path: Path):
    raw = path.read_bytes().decode("utf-8-sig")
    try:
        return json.loads(raw)
    except ValueError:
        return json.loads(_strip_json_comments(raw))


def resolve_messages(directory: Path, manifest: dict, locale: str) -> dict[str, str]:
    """chrome.i18n's messages as Chrome picks them (default locale < language < full locale), placeholders filled in."""
    default = manifest.get("default_locale")
    merged: dict[str, str] = {}
    if not isinstance(default, str) or not default:
        return merged
    for loc in dict.fromkeys([default, locale.split("_")[0], locale]):
        try:
            data = _read_json_file(directory / "_locales" / loc / "messages.json")
        except (OSError, ValueError):
            continue
        for name, entry in data.items() if isinstance(data, dict) else ():
            if not isinstance(entry, dict) or not isinstance(entry.get("message"), str):
                continue
            holders = {str(k).lower(): str(v.get("content", "")) for k, v in (entry.get("placeholders") or {}).items()
                       if isinstance(v, dict)} if isinstance(entry.get("placeholders"), dict) else {}
            merged[name.lower()] = re.sub(r"\$([A-Za-z0-9_@]+)\$", lambda m: holders.get(m.group(1).lower(), m.group(0)), entry["message"])
    return merged


def message_text(value, messages: dict[str, str]) -> str:
    """A manifest string with its "__MSG_name__" filled in from resolve_messages()."""
    value = value if isinstance(value, str) else ""
    m = re.fullmatch(r"__MSG_(\w+)__", value)
    return messages.get(m.group(1).lower(), value) if m else value


def allow_scheme_in_csp(csp: str, scheme: str) -> str:
    """An extension CSP with connect-src (or default-src) would block fetch() to Foxglove's scheme."""
    parts = [p.strip() for p in csp.split(";") if p.strip()]
    names = [p.split()[0].lower() for p in parts]
    source = scheme + ":"
    for directive in ("connect-src", "default-src"):
        if directive in names:
            values = [v for v in parts[names.index(directive)].split()[1:] if v != "'none'"]
            if source in values:
                return csp
            if directive == "connect-src":
                parts[names.index(directive)] = " ".join(["connect-src", *values, source])
            else:
                parts.append(" ".join(["connect-src", *values, source]))
            break
    return "; ".join(parts)


def command_shortcut(command: dict) -> str:
    """A manifest command's suggested key for this platform, in Qt's spelling ("" if none)."""
    keys = command.get("suggested_key") if isinstance(command, dict) else None
    if isinstance(keys, str):
        keys = {"default": keys}
    if not isinstance(keys, dict):
        return ""
    platform = "mac" if IS_MAC else "windows" if sys.platform == "win32" else "linux"
    key = keys.get(platform) or keys.get("default")
    if not isinstance(key, str):
        return ""
    names = {"Command": "Ctrl", "MacCtrl": "Meta", "Comma": ",", "Period": ".", "PageUp": "PgUp", "PageDown": "PgDown",
             "Insert": "Ins", "Delete": "Del"}
    return "+".join(names.get(part.strip(), part.strip()) for part in key.split("+"))


def shim_stamp(secret: str, locale: str) -> str:
    """Identifies the polyfill an extension got: a newer Foxglove (or another UI language) re-wires it at start-up."""
    return f"v{SHIM_VERSION}-{hashlib.sha256((secret + locale + EXTENSION_SHIM_JS).encode()).hexdigest()[:10]}"


def installed_shim_config(directory: Path) -> dict:
    """The configuration written into an installed extension's shim ({} if it has none)."""
    try:
        text = (directory / SHIM_FILE).read_text(encoding="utf-8")
        start = text.index(SHIM_CFG_MARK) + len(SHIM_CFG_MARK)
        data = json.loads(text[start:text.index("/*END*/", start)])
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", "surrogateescape")


def _write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8", "surrogateescape"))


def _replace_file(path: Path, data: bytes) -> None:
    """Write a file all at once: a crash midway leaves the old one (an extension with half a manifest wouldn't load)."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def valid_match_pattern(pattern) -> bool:
    """A match pattern Chrome (and Qt's manifest parser) accepts - a bad one would stop the extension from loading."""
    if not isinstance(pattern, str):
        return False
    m = re.fullmatch(r"(?:(?:\*|https?|wss?|ftp|urn)://(?:\*|(?:\*\.)?[^/*:\s]+)(?::(\*|\d{1,5}))?|file://[^/*:\s]*)/\S*", pattern)
    return pattern == "<all_urls>" or (m is not None and (m.group(1) in (None, "*") or int(m.group(1)) <= 65535))


# Unicode noncharacters: valid UTF-8 to Python, not to Chromium (IsStringUTF8) - a content script with one doesn't load
NONCHARACTERS = re.compile("[\ufdd0-\ufdef" + "".join(chr(p | 0xFFFE) + chr(p | 0xFFFF) for p in range(0, 0x110000, 0x10000)) + "]")


def wire_content_scripts(entries) -> list[dict]:
    """Manifest content_scripts with the polyfill in front of the extension's own scripts (not in the page's world)."""
    out = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            entry = dict(entry)
            js = [j for j in entry.get("js") or [] if isinstance(j, str)] if isinstance(entry.get("js"), list) else []
            if js and entry.get("world") != "MAIN":
                entry["js"] = [SHIM_FILE, *[j for j in js if j.lstrip("/") != SHIM_FILE]]
            out.append(entry)
    return out


def registered_as_manifest(scripts) -> list[dict]:
    """chrome.scripting.registerContentScripts() entries in manifest form: Qt applies them when it loads the extension."""
    keys = {"matches": "matches", "excludeMatches": "exclude_matches", "js": "js", "css": "css", "allFrames": "all_frames",
            "runAt": "run_at", "matchOriginAsFallback": "match_origin_as_fallback", "world": "world"}
    return [{keys[k]: v for k, v in s.items() if k in keys and v not in (None, [], False, "ISOLATED")}
            for s in scripts if isinstance(s, dict)] if isinstance(scripts, list) else []


def inject_shim(directory: Path, manifest: dict, secret: str, locale: str, registered: list | None = None) -> dict:
    """Wire the polyfill into an extension folder: shim file, worker wrapper, <script> in every page, first content
    script, CSP. Rewrites manifest.json (the untouched one is kept beside it) and returns the new manifest.
    *registered*: the extension's chrome.scripting content scripts, as manifest entries."""
    sidecar = directory / SHIM_ORIGINAL
    try:  # wired once already (an installed copy getting a newer shim): start from the original...
        kept = _read_json_file(sidecar) if sidecar.is_file() else None
    except (OSError, ValueError):
        kept = None
    if isinstance(kept, dict):  # ... but the ID stays the one checked on *manifest* (a package may bring its own sidecar)
        kept.pop("key", None)
        if "key" in manifest:
            kept["key"] = manifest["key"]
        manifest = kept
    original, manifest = json.loads(json.dumps(manifest)), json.loads(json.dumps(manifest))
    strings = lambda value: [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
    messages = resolve_messages(directory, manifest, locale)
    cfg: dict = {
        "bridge": EXT_BRIDGE_URL, "scheme": EXT_SCHEME, "uiLocale": locale, "messages": messages,
        "permissions": strings(manifest.get("permissions")),
        "optional": strings(manifest.get("optional_permissions")) + strings(manifest.get("optional_host_permissions")),
        "commands": [{"name": k, "description": message_text(v.get("description"), messages), "shortcut": command_shortcut(v)}
                     for k, v in (manifest.get("commands") or {}).items() if isinstance(v, dict)]
                    if isinstance(manifest.get("commands"), dict) else [],
        "sidePanel": (manifest.get("side_panel") or {}).get("default_path") if isinstance(manifest.get("side_panel"), dict) else None,
        "relay": "__fg" + secrets.token_hex(10), "token": secrets.token_hex(16),  # all three known to this extension alone
        "tabQuery": "__fg" + secrets.token_hex(10), "tabAnswer": "__fg" + secrets.token_hex(10), "mark": secrets.token_hex(8),
        "war": [r for w in manifest.get("web_accessible_resources") or [] if isinstance(w, dict) for r in strings(w.get("resources"))]
               if isinstance(manifest.get("web_accessible_resources"), list) else [],
        "worker": False, "cs": False,
        "manifest": {k: original[k] for k in ("background", "content_scripts") if k in original},
    }
    background = manifest.get("background") if isinstance(manifest.get("background"), dict) else None
    worker = background.get("service_worker") if background else None
    if isinstance(worker, str) and worker.strip("/"):
        rel, root = Path(worker.lstrip("/")), directory.resolve()
        wrapper = (root / rel.parent / SHIM_WORKER).resolve()
        if root != wrapper.parent and root not in wrapper.parent.parents:  # "../" or a drive: never write outside it
            raise InstallError(f"The extension's service worker ({worker}) is outside the extension.")
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        _write_text(wrapper, f'import "/{SHIM_FILE}";\nimport {json.dumps("./" + rel.name)};\n' if background.get("type") == "module"
                    else f'importScripts("/{SHIM_FILE}", {json.dumps(rel.name)});\n')
        background["service_worker"] = (rel.parent / SHIM_WORKER).as_posix()
        cfg["worker"] = True
    scripts = wire_content_scripts(manifest.get("content_scripts")) + wire_content_scripts(registered)
    if scripts:
        manifest["content_scripts"] = scripts
    cfg["cs"] = any(SHIM_FILE in (e.get("js") or []) for e in scripts) or "scripting" in cfg["permissions"]
    csp = manifest.get("content_security_policy")
    if isinstance(csp, dict) and isinstance(csp.get("extension_pages"), str):
        csp["extension_pages"] = allow_scheme_in_csp(csp["extension_pages"], EXT_SCHEME)
    sandbox = manifest.get("sandbox") if isinstance(manifest.get("sandbox"), dict) else {}
    sandboxed = ["/" + p.lstrip("/") for p in strings(sandbox.get("pages"))]  # Chrome gives them no extension APIs
    for page in sorted([*directory.rglob("*.html"), *directory.rglob("*.htm")]):
        if page.name == SHIM_BRIDGE or not page.is_file():
            continue
        text = _text(page)
        if any(wildcard_match(p, "/" + page.relative_to(directory).as_posix()) for p in sandboxed):
            if SHIM_TAG in text:  # (wired by an older Foxglove)
                _write_text(page, text.replace(SHIM_TAG, ""))
            continue
        if SHIM_TAG in text:
            continue
        m = re.search(r"<head\b[^>]*>", text, re.I) or re.search(r"<html\b[^>]*>", text, re.I) or re.search(r"<!doctype[^>]*>", text, re.I)
        _write_text(page, text[:m.end()] + SHIM_TAG + text[m.end():] if m else SHIM_TAG + text)
    body = json.dumps(cfg, ensure_ascii=False).replace("/", "\\/")  # "\/" keeps "*/" out of the comment markers
    shim = EXTENSION_SHIM_JS.replace("%(stamp)s", shim_stamp(secret, locale), 1).replace(SHIM_CFG_MARK + "{}", SHIM_CFG_MARK + body, 1)
    (directory / SHIM_FILE).write_text(shim, encoding="utf-8")
    (directory / SHIM_BRIDGE).write_text(f'<!doctype html><meta charset="utf-8"><title>{APP_NAME}</title>'
                                         f'<script src="{SHIM_FILE}"></script>\n', encoding="utf-8")
    sidecar.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def substitute_css(directory: Path, ext_id: str, locale: str) -> None:
    """Qt doesn't fill in __MSG_...__ in extension CSS; content-script CSS also needs web-accessible files from Foxglove."""
    try:
        manifest = _read_json_file(directory / SHIM_ORIGINAL)
    except (OSError, ValueError):
        return
    messages = {**resolve_messages(directory, manifest, locale), "@@extension_id": ext_id, "@@ui_locale": locale,
                "@@bidi_dir": "ltr", "@@bidi_reversed_dir": "rtl", "@@bidi_start_edge": "left", "@@bidi_end_edge": "right"}
    in_pages = {c.lstrip("/") for e in manifest.get("content_scripts") or [] if isinstance(e, dict)
                for c in e.get("css") or [] if isinstance(c, str)} if isinstance(manifest.get("content_scripts"), list) else set()
    root = directory.resolve()
    for sheet in root.rglob("*.css"):
        try:
            text = _text(sheet)
        except OSError:
            continue
        new = re.sub(r"__MSG_(@?@?\w+)__", lambda m: messages.get(m.group(1).lower(), m.group(0)), text)
        if sheet.relative_to(root).as_posix() in in_pages:
            new = new.replace(f"chrome-extension://{ext_id}/", f"{EXT_SCHEME}://{ext_id}/")
        if new != text:
            _write_text(sheet, new)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Persistent stores: settings, bookmarks, history, favicons
# ══════════════════════════════════════════════════════════════════════════════════════════
class Settings(QObject):
    changed = pyqtSignal(str)
    DEFAULTS = {
        "search_engine": "Google",
        "homepage": "",
        "restore_session": True,
        "show_bookmarks_bar": True,
        "download_dir": "",
        "ask_download_location": False,
        "website_appearance": "dark",
        "force_dark_pages": False,
        "site_zoom": {},
        "site_permissions": {},  # origin -> {permission type name: "allow"/"block"} for the ones Qt doesn't remember
        "vpn": {"mode": "off", "type": "socks5", "host": "", "port": 1080, "username": "", "password": ""},
        # New Tab page (Chrome's): "My shortcuts" show the most visited sites until first edited, like Chrome's
        "ntp_shortcut_mode": "custom",     # "custom" (My shortcuts) or "most_visited"
        "ntp_show_shortcuts": True,
        "ntp_shortcuts": [],               # [{"title", "url"}] once edited
        "ntp_shortcuts_edited": False,
        "ntp_hidden": [],                  # most visited pages removed from the page
        "ntp_theme": "",                   # "" (default) or a NTP_COLORS key
        "privacy_screen": True,            # grey out the windows while Chrome 2 isn't the active app
    }

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        data = read_json(path, {})
        self._data = data if isinstance(data, dict) else {}

    def get(self, key: str):
        default = self.DEFAULTS[key]
        value = self._data.get(key, default)
        if type(value) is not type(default):
            return json.loads(json.dumps(default))
        return value

    def set(self, key: str, value) -> None:
        if key in self._data and self._data[key] == value:
            return
        self._data[key] = value
        self.save()
        self.changed.emit(key)

    def save(self) -> None:
        if write_json(self.path, self._data) and os.name == "posix":
            try:
                os.chmod(self.path, 0o600)  # may hold a proxy password: keep it private to your user
            except OSError:
                pass

    def search_template(self) -> str:
        return SEARCH_ENGINES.get(self.get("search_engine"), SEARCH_ENGINES["Google"])

    def downloads_dir(self) -> Path:
        configured = self.get("download_dir")
        if configured and Path(configured).is_dir():
            return Path(configured)
        system = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        return Path(system or Path.home() / "Downloads")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class _NetscapeBookmarkParser(HTMLParser):
    """Reads the "NETSCAPE-Bookmark-file-1" HTML that Firefox, Chrome, Safari and Edge export."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = {"type": "folder", "title": "", "children": [], "toolbar": False}
        self.stack = [self.root]
        self.pending_folder: dict | None = None
        self.root_opened = False
        self.current: dict | None = None
        self.text: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag == "h3":
            self.current = {"type": "folder", "title": "", "children": [],
                            "toolbar": attrs.get("personal_toolbar_folder", "").lower() == "true"}
            self.text = []
        elif tag == "a":
            self.current = {"type": "url", "url": attrs.get("href", "").strip(), "title": "",
                            "icon": attrs.get("icon", "")}
            self.text = []
        elif tag == "dl":
            if self.pending_folder is not None:
                self.stack.append(self.pending_folder)
                self.pending_folder = None
            elif not self.root_opened:
                self.root_opened = True
            else:
                self.stack.append(self.stack[-1])

    def handle_endtag(self, tag):
        if tag in ("h3", "a") and self.current is not None and self.text is not None:
            self.current["title"] = " ".join("".join(self.text).split())
            self.stack[-1]["children"].append(self.current)
            if tag == "h3":
                self.pending_folder = self.current
            self.current, self.text = None, None
        elif tag == "dl" and len(self.stack) > 1:
            self.stack.pop()

    def handle_data(self, data):
        if self.text is not None:
            self.text.append(data)


class BookmarkStore(QObject):
    """Bookmarks as a JSON tree with two roots: the Bookmarks Toolbar and Other Bookmarks."""

    changed = pyqtSignal()
    ROOTS = (("toolbar", "Bookmarks Toolbar"), ("other", "Other Bookmarks"))

    def __init__(self, path: Path, favicons: "FaviconCache | None" = None):
        super().__init__()
        self.path = path
        self.favicons = favicons
        self._backed_up = False
        raw = read_json(path, {})
        roots = raw.get("roots") if isinstance(raw, dict) else None
        seen: set[str] = set()
        self.roots: dict[str, dict] = {}
        for key, title in self.ROOTS:
            node = roots.get(key) if isinstance(roots, dict) else None
            children = node.get("children") if isinstance(node, dict) else None
            self.roots[key] = {"id": key, "type": "folder", "title": title, "added": time.time(),
                               "children": self._clean(children, seen)}
        self._index: dict[str, tuple[dict, dict | None]] = {}
        self._by_url: dict[str, list[dict]] = {}
        self._reindex()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.save)

    def _clean(self, children, seen: set[str]) -> list[dict]:
        out: list[dict] = []
        for node in children if isinstance(children, list) else ():
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "")
            if not node_id or node_id in seen or node_id in ("toolbar", "other"):
                node_id = _new_id()
            seen.add(node_id)
            added = node.get("added") if isinstance(node.get("added"), (int, float)) else time.time()
            title = str(node.get("title") or "")
            if node.get("type") == "folder":
                out.append({"id": node_id, "type": "folder", "title": title, "added": added,
                            "children": self._clean(node.get("children"), seen)})
            elif node.get("type") == "url" and isinstance(node.get("url"), str) and node["url"].strip():
                out.append({"id": node_id, "type": "url", "title": title, "url": node["url"].strip(), "added": added})
        return out

    def _reindex(self) -> None:
        self._index, self._by_url = {}, {}

        def walk(folder: dict, parent: dict | None) -> None:
            self._index[folder["id"]] = (folder, parent)
            for child in folder["children"]:
                if child["type"] == "folder":
                    walk(child, folder)
                else:
                    self._index[child["id"]] = (child, folder)
                    self._by_url.setdefault(child["url"], []).append(child)

        for root in self.roots.values():
            walk(root, None)

    def _commit(self) -> None:
        self._reindex()
        self._save_timer.start()
        self.changed.emit()

    # Queries
    def node(self, node_id: str) -> dict | None:
        entry = self._index.get(node_id)
        return entry[0] if entry else None

    def parent(self, node_id: str) -> dict | None:
        entry = self._index.get(node_id)
        return entry[1] if entry else None

    def children(self, folder_id: str) -> list[dict]:
        folder = self.node(folder_id)
        return list(folder["children"]) if folder and folder["type"] == "folder" else []

    def for_url(self, url: str) -> list[dict]:
        return list(self._by_url.get(url, ()))

    def is_bookmarked(self, url: str) -> bool:
        return bool(self._by_url.get(url))

    def folders(self) -> list[tuple[str, str, int]]:
        out: list[tuple[str, str, int]] = []

        def walk(folder: dict, depth: int) -> None:
            out.append((folder["id"], folder["title"] or "Untitled folder", depth))
            for child in folder["children"]:
                if child["type"] == "folder":
                    walk(child, depth + 1)

        for root in self.roots.values():
            walk(root, 0)
        return out

    def iter_bookmarks(self):
        for node, _parent in list(self._index.values()):
            if node["type"] == "url":
                yield node

    def search(self, text: str, limit: int = 50) -> list[dict]:
        words = text.lower().split()
        out = []
        for node in self.iter_bookmarks():
            haystack = f"{node['title']} {node['url']}".lower()
            if all(word in haystack for word in words):
                out.append(node)
                if len(out) >= limit:
                    break
        return out

    # Mutations
    @staticmethod
    def _insert(folder: dict, node: dict, index: int | None) -> None:
        kids = folder["children"]
        if index is None or index < 0 or index > len(kids):
            kids.append(node)
        else:
            kids.insert(index, node)

    def _folder_or_default(self, folder_id: str) -> dict:
        folder = self.node(folder_id)
        return folder if folder is not None and folder["type"] == "folder" else self.roots["toolbar"]

    def add_bookmark(self, title: str, url: str, folder_id: str = "toolbar", index: int | None = None) -> dict:
        node = {"id": _new_id(), "type": "url", "title": title.strip() or url, "url": url, "added": time.time()}
        self._insert(self._folder_or_default(folder_id), node, index)
        self._commit()
        return node

    def add_folder(self, title: str, folder_id: str = "toolbar", index: int | None = None) -> dict:
        node = {"id": _new_id(), "type": "folder", "title": title.strip() or "New Folder", "added": time.time(), "children": []}
        self._insert(self._folder_or_default(folder_id), node, index)
        self._commit()
        return node

    def update(self, node_id: str, title: str | None = None, url: str | None = None) -> None:
        node = self.node(node_id)
        if node is None or node_id in self.roots:
            return
        if title is not None:
            node["title"] = title.strip() or (node.get("url") or "New Folder")
        if url is not None and node["type"] == "url" and url.strip():
            node["url"] = url.strip()
        self._commit()

    def move(self, node_id: str, folder_id: str, index: int | None = None) -> bool:
        node, parent = self._index.get(node_id, (None, None))
        target = self.node(folder_id)
        if node is None or parent is None or target is None or target["type"] != "folder":
            return False
        probe: dict | None = target
        while probe is not None:  # a folder can't be moved into itself or its own sub-folders
            if probe is node:
                return False
            probe = self.parent(probe["id"])
        kids = parent["children"]
        old_index = next(i for i, child in enumerate(kids) if child is node)
        del kids[old_index]
        if parent is target and index is not None and index > old_index:
            index -= 1
        self._insert(target, node, index)
        self._commit()
        return True

    def remove(self, node_id: str) -> None:
        node, parent = self._index.get(node_id, (None, None))
        if node is None or parent is None:
            return
        kids = parent["children"]
        del kids[next(i for i, child in enumerate(kids) if child is node)]
        self._commit()

    def remove_url(self, url: str) -> None:
        for node in self.for_url(url):
            node_parent = self.parent(node["id"])
            if node_parent is not None:
                node_parent["children"] = [c for c in node_parent["children"] if c is not node]
        self._commit()

    def apply_structure(self, structure: dict[str, list[str]]) -> bool:
        """Re-parent nodes after a drag & drop in the manager: {folder id: [child ids in order]}."""
        nodes = {node_id: node for node_id, (node, _p) in self._index.items()}
        folders = {node_id for node_id, node in nodes.items() if node["type"] == "folder"}
        if set(structure) != folders:
            return False
        placed: set[str] = set()
        for child_ids in structure.values():
            for child_id in child_ids:
                if child_id in placed or child_id not in nodes or child_id in self.roots:
                    return False
                placed.add(child_id)
        if placed != set(nodes) - set(self.roots):
            return False
        for folder_id, child_ids in structure.items():
            nodes[folder_id]["children"] = [nodes[c] for c in child_ids]
        self._commit()
        return True

    def save(self) -> None:
        if write_json(self.path, {"version": 1, "roots": self.roots}, keep_backup=not self._backed_up):
            self._backed_up = True

    def flush(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
            self.save()

    # Import / export (Netscape bookmark HTML, the format every browser exports)
    def import_html(self, path: str) -> int:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8", errors="replace")
        parser = _NetscapeBookmarkParser()
        parser.feed(text)
        parser.close()
        folder = {"id": _new_id(), "type": "folder", "title": f"Imported {time.strftime('%Y-%m-%d')}",
                  "added": time.time(), "children": []}
        count = 0

        def convert(source: dict, target: dict) -> None:
            nonlocal count
            for child in source["children"]:
                if child["type"] == "folder":
                    if child.get("toolbar"):
                        convert(child, self.roots["toolbar"])
                        continue
                    sub = {"id": _new_id(), "type": "folder", "title": child["title"] or "Folder",
                           "added": time.time(), "children": []}
                    target["children"].append(sub)
                    convert(child, sub)
                else:
                    url = child["url"]
                    if not re.match(r"^(https?|file|ftp|chrome-extension):", url, re.I):
                        continue
                    target["children"].append({"id": _new_id(), "type": "url", "title": child["title"] or url,
                                               "url": url, "added": time.time()})
                    count += 1
                    if self.favicons is not None and child.get("icon", "").startswith("data:image/"):
                        self.favicons.store_data_uri(url, child["icon"])

        convert(parser.root, folder)
        if folder["children"]:
            self.roots["other"]["children"].append(folder)
        self._commit()
        return count

    def export_html(self, path: str) -> None:
        lines = [
            "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
            "<!-- This is an automatically generated file. It will be read and overwritten. DO NOT EDIT! -->",
            '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
            "<TITLE>Bookmarks</TITLE>",
            "<H1>Bookmarks</H1>",
            "<DL><p>",
        ]

        def emit(folder: dict, depth: int) -> None:
            pad = "    " * depth
            for child in folder["children"]:
                added = int(child.get("added") or 0)
                if child["type"] == "folder":
                    lines.append(f'{pad}<DT><H3 ADD_DATE="{added}">{html.escape(child["title"])}</H3>')
                    lines.append(f"{pad}<DL><p>")
                    emit(child, depth + 1)
                    lines.append(f"{pad}</DL><p>")
                else:
                    lines.append(f'{pad}<DT><A HREF="{html.escape(child["url"], quote=True)}" ADD_DATE="{added}">'
                                 f'{html.escape(child["title"] or child["url"])}</A>')

        for key, _title in self.ROOTS:
            root = self.roots[key]
            extra = ' PERSONAL_TOOLBAR_FOLDER="true"' if key == "toolbar" else ""
            lines.append(f'    <DT><H3{extra}>{html.escape(root["title"])}</H3>')
            lines.append("    <DL><p>")
            emit(root, 2)
            lines.append("    </DL><p>")
        lines.append("</DL><p>")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


class HistoryStore:
    """Browsing history in SQLite, used by the address bar, the History window and the New Tab page."""

    def __init__(self, path: Path):
        self.db: sqlite3.Connection | None = None
        try:
            self.db = sqlite3.connect(str(path))
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS places (url TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', "
                "visit_count INTEGER NOT NULL DEFAULT 0, last_visit REAL NOT NULL DEFAULT 0)")
            self.db.execute("CREATE INDEX IF NOT EXISTS places_last_visit ON places(last_visit)")
            self.db.commit()
        except sqlite3.Error as exc:
            log(f"History is unavailable: {exc}")
            self.db = None

    @staticmethod
    def recordable(url: QUrl) -> bool:
        return url.scheme() in ("http", "https", "file", "ftp") and bool(url.toString())

    def _run(self, sql: str, params: tuple = (), fetch: bool = False):
        if self.db is None:
            return [] if fetch else None
        try:
            cursor = self.db.execute(sql, params)
            if fetch:
                return cursor.fetchall()
            self.db.commit()
        except sqlite3.Error as exc:
            log(f"History error: {exc}")
        return [] if fetch else None

    def add_visit(self, url: str, title: str) -> None:
        self._run(
            "INSERT INTO places (url, title, visit_count, last_visit) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(url) DO UPDATE SET visit_count = visit_count + 1, last_visit = excluded.last_visit, "
            "title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE places.title END",
            (url, title or "", time.time()))

    def set_title(self, url: str, title: str) -> None:
        if title:
            self._run("UPDATE places SET title = ? WHERE url = ?", (title, url))

    @staticmethod
    def _like(word: str) -> str:
        return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def search(self, text: str, limit: int = 8) -> list[tuple[str, str]]:
        words = text.split()[:6]
        if not words:
            return []
        where = " AND ".join("(url LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')" for _ in words)
        params: list = []
        for word in words:
            params += [self._like(word), self._like(word)]
        rows = self._run(
            f"SELECT url, title FROM places WHERE {where} "
            "ORDER BY visit_count * 1.0 / (1.0 + (? - last_visit) / 604800.0) DESC LIMIT ?",
            tuple(params) + (time.time(), limit), fetch=True)
        return [(r[0], r[1]) for r in rows]

    def recent(self, text: str = "", limit: int = 2000) -> list[tuple[str, str, float, int]]:
        words = text.split()[:6]
        where = " AND ".join("(url LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')" for _ in words) or "1"
        params: list = []
        for word in words:
            params += [self._like(word), self._like(word)]
        rows = self._run(f"SELECT url, title, last_visit, visit_count FROM places WHERE {where} "
                         "ORDER BY last_visit DESC LIMIT ?", tuple(params) + (limit,), fetch=True)
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    def top_sites(self, limit: int = 8, exclude=()) -> list[tuple[str, str]]:
        """The New Tab page's most visited sites: web pages ranked as the address bar ranks them (visits, fading over
        weeks), one per address (trailing "/" and #fragment aside), minus *exclude* (the ones removed from the page)."""
        skip, found, seen = set(exclude), [], set()
        rows = self._run("SELECT url, title FROM places WHERE url LIKE 'http%' "
                         "ORDER BY visit_count * 1.0 / (1.0 + (? - last_visit) / 604800.0) DESC LIMIT ?",
                         (time.time(), limit * 4 + len(skip)), fetch=True)
        for url, title in rows:
            key = url.split("#", 1)[0].rstrip("/")
            if url in skip or key in seen or not url.startswith(("http://", "https://")):
                continue
            seen.add(key)
            found.append((url, title))
            if len(found) >= limit:
                break
        return found

    def delete(self, urls: list[str]) -> None:
        for url in urls:
            self._run("DELETE FROM places WHERE url = ?", (url,))

    def clear(self) -> None:
        self._run("DELETE FROM places")

    def close(self) -> None:
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
            self.db = None


class CookieIndex(QObject):
    """Every cookie in a profile, kept current from its cookie store (which can't be searched). One per profile, shared
    by the site settings and chrome.cookies: CookieIndex.of(profile). Make it before the profile's first page: it starts
    from the cookies Chromium saved (its Cookies database, read before Chromium opens it), then follows every change.
    loadAllCookies() can't stand in for that: Qt asks Chromium to load the cookies and drops the answer, so cookies
    that were already there are never reported.

    Qt reports a cookie set again unchanged (pages do that all the time) as removed, with no "added" after it, so a
    removal is no proof: such a cookie stays listed ("unsure") - and deleted with its site - until Chromium's database,
    which gets every change within 30 s, says it's gone. Cookies Foxglove deletes itself go at once."""
    added = pyqtSignal(object)    # QNetworkCookie
    removed = pyqtSignal(object)
    loaded = pyqtSignal()
    changed = pyqtSignal()        # at most every 150 ms (for views)
    LOAD_MS = 1500                # what loadAllCookies() may still report arrives well within this (it never says "done")
    RECHECK_S = 40                # Chromium writes cookie changes to its database at least every 30 s
    SAME_SITE = {0: QNetworkCookie.SameSite.None_, 1: QNetworkCookie.SameSite.Lax, 2: QNetworkCookie.SameSite.Strict}

    def __init__(self, profile: QWebEngineProfile):
        super().__init__(profile)
        self.store = profile.cookieStore()
        self.cookies: dict[tuple, QNetworkCookie] = {}  # (name, domain, path) -> cookie; updated in place, never replaced
        self.unsure: dict[tuple, float] = {}  # reported removed, maybe still there -> when (monotonic)
        self.unknown_values: set[tuple] = set()  # read from the database encrypted: deletable, but can't be made again
        self.ready = False
        self.db_path = None if profile.isOffTheRecord() else Path(profile.persistentStoragePath()) / "Cookies"
        self._notify = QTimer(self)
        self._notify.setSingleShot(True)
        self._notify.setInterval(150)
        self._notify.timeout.connect(self.changed)
        self._recheck = QTimer(self)
        self._recheck.setSingleShot(True)
        self._recheck.timeout.connect(self._reconcile)
        saved = self._read_saved() if self.db_path else None
        if saved is not None:
            self.cookies.update(saved[0])
            self.unknown_values = saved[1]
        self.store.cookieAdded.connect(self._added)
        self.store.cookieRemoved.connect(self._removed)
        self.store.loadAllCookies()
        QTimer.singleShot(self.LOAD_MS, self._loaded)

    def _read_saved(self) -> tuple[dict, set] | None:
        """The cookies in Chromium's database and those whose value it encrypted (None if it can't be read)."""
        try:
            path = self.db_path
            db = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=0.5) if path.is_file() else None
            rows = db.execute("SELECT host_key, name, value, length(encrypted_value), path, expires_utc, is_secure, "
                              "is_httponly, has_expires, samesite FROM cookies").fetchall() if db else []
            if db:
                db.close()
        except sqlite3.Error as exc:
            log(f"Couldn't read the saved cookies: {exc}")
            return None
        now, cookies, unknown = time.time(), {}, set()
        for host, name, value, encrypted, cookie_path, expires, secure, http_only, has_expires, same_site in rows:
            cookie = QNetworkCookie(str(name).encode(), str(value or "").encode())
            cookie.setDomain(str(host))
            cookie.setPath(str(cookie_path or "/"))
            cookie.setSecure(bool(secure))
            cookie.setHttpOnly(bool(http_only))
            cookie.setSameSitePolicy(self.SAME_SITE.get(same_site, QNetworkCookie.SameSite.Default))
            if has_expires:
                seconds = int(expires) / 1e6 - 11644473600  # microseconds since 1601
                if seconds <= now:
                    continue
                cookie.setExpirationDate(QDateTime.fromMSecsSinceEpoch(int(seconds * 1000)))
            cookies[self.key(cookie)] = cookie
            if encrypted and not value:
                unknown.add(self.key(cookie))
        return cookies, unknown

    def _saved_keys(self) -> set[tuple] | None:
        """The keys of the unexpired cookies in Chromium's database (None if it can't be read)."""
        try:
            db = sqlite3.connect(f"file:{quote(str(self.db_path))}?mode=ro", uri=True, timeout=0.5)
            try:
                rows = db.execute("SELECT name, host_key, path FROM cookies WHERE has_expires = 0 OR expires_utc > ?",
                                  ((int(time.time()) + 11644473600) * 1_000_000,)).fetchall()
            finally:
                db.close()
        except sqlite3.Error as exc:
            log(f"Couldn't read the saved cookies: {exc}")
            return None
        return {(str(name), str(host).lower(), str(path or "/")) for name, host, path in rows}

    @classmethod
    def of(cls, profile: QWebEngineProfile) -> "CookieIndex":
        index = profile.findChild(cls)
        return index if index is not None else cls(profile)

    @staticmethod
    def key(cookie: QNetworkCookie) -> tuple:
        return bytes(cookie.name()).decode("utf-8", "replace"), cookie.domain().lower(), cookie.path() or "/"

    @staticmethod
    def url_of(cookie: QNetworkCookie) -> QUrl:
        return QUrl(f"{'https' if cookie.isSecure() else 'http'}://{cookie.domain().lstrip('.')}{cookie.path() or '/'}")

    @staticmethod
    def sent_to(cookie: QNetworkCookie, url: QUrl) -> bool:
        """Whether a request to *url* carries *cookie* (what Chromium deletes by name and URL)."""
        domain, host, path, at = cookie.domain().lower(), url.host().lower(), cookie.path() or "/", url.path() or "/"
        return ((host == domain if not domain.startswith(".") else host == domain[1:] or host.endswith(domain))
                and (at == path or at.startswith(path.rstrip("/") + "/")) and (not cookie.isSecure() or url.scheme() == "https"))

    def _changed(self) -> None:
        self._notify.isActive() or self._notify.start()

    def _added(self, cookie) -> None:
        cookie = QNetworkCookie(cookie)
        key = self.key(cookie)
        self.cookies[key] = cookie
        self.unsure.pop(key, None)
        self.unknown_values.discard(key)
        self.added.emit(cookie)
        self._changed()

    def _removed(self, cookie) -> None:
        cookie = QNetworkCookie(cookie)
        key = self.key(cookie)
        if key in self.cookies:
            if not cookie.isSessionCookie() and cookie.expirationDate() <= QDateTime.currentDateTime():
                self._forget(key)  # expired
            else:  # deleted, or set again unchanged: the database will tell
                self.unsure[key] = time.monotonic()
                if self.db_path is not None and not self._recheck.isActive():
                    self._recheck.start(self.RECHECK_S * 1000)
        self.removed.emit(cookie)
        self._changed()

    def _forget(self, key: tuple) -> None:
        self.cookies.pop(key, None)
        self.unsure.pop(key, None)
        self.unknown_values.discard(key)

    def _reconcile(self) -> None:
        if sip.isdeleted(self) or not self.unsure:
            return
        due = time.monotonic() - self.RECHECK_S + 1
        saved = self._saved_keys()
        settled = [key for key, when in self.unsure.items() if when <= due]
        for key in settled:
            del self.unsure[key]
            if saved is not None and key not in saved:
                self.cookies.pop(key, None)
        if self.unsure:
            self._recheck.start(max(1, int((min(self.unsure.values()) - due + 1) * 1000)))
        self._changed()

    def _loaded(self) -> None:
        if not sip.isdeleted(self):
            self.ready = True
            self.loaded.emit()
            self.changed.emit()

    def for_site(self, site: str) -> list[QNetworkCookie]:
        return sorted((c for c in self.cookies.values() if site_of(c.domain()) == site),
                      key=lambda c: (c.domain().lstrip("."), bytes(c.name())))

    def sites(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cookie in self.cookies.values():
            site = site_of(cookie.domain())
            counts[site] = counts.get(site, 0) + 1
        return counts

    def delete(self, cookies: list[QNetworkCookie], url: QUrl | None = None, keep_others: bool = True) -> list[QNetworkCookie]:
        """Delete *cookies* (by default each at its own URL). Chromium deletes every cookie of that name a request to
        the URL would carry - on parent paths, the domain and the host too: unless *keep_others* is False, those are
        made again. Returns the ones that couldn't be (their value is encrypted in the database)."""
        doomed, hit = {self.key(c) for c in cookies}, {}
        for cookie in cookies:
            target = url if url is not None else self.url_of(cookie)
            hit.update((k, c) for k, c in self.cookies.items() if c.name() == cookie.name() and self.sent_to(c, target))
            self.store.deleteCookie(cookie, target)
        lost = []
        for key, cookie in hit.items():
            unknown = key in self.unknown_values
            self._forget(key)
            if key in doomed or not keep_others:
                continue
            if unknown:
                lost.append(cookie)
                continue
            again = QNetworkCookie(cookie)
            if not cookie.domain().startswith("."):
                again.setDomain("")  # host-only: made from the URL (a domain= would make it a domain cookie)
            self.store.setCookie(again, self.url_of(cookie))
        for key in doomed:
            self._forget(key)
        self._changed()
        return lost

    def delete_all(self) -> None:
        self.store.deleteAllCookies()
        for key in list(self.cookies):
            self._forget(key)
        self._changed()


class SiteDataCleaner(QObject):
    """Clears what web pages store in the browser - local storage, IndexedDB, Cache Storage, service workers - for some
    origins. Qt has no API for that, so a hidden page *in* each origin clears it with the web platform's own calls:
    first an empty document given the origin (no network, works offline; it can't reach service workers), then, when
    *thorough*, the site's /robots.txt, a real document of the origin that can unregister them too (a cookie the site
    sets with it is deleted at once). Limits: an IndexedDB database a tab holds open goes when that tab lets go of it
    (the caller reloads such tabs); sessionStorage belongs to each tab and goes with it."""
    finished = pyqtSignal()
    PARALLEL, STEP_MS = 4, 6000
    BLANK = "<!doctype html><title></title>"
    JS = """(async () => {
  const soon = (p) => Promise.race([p, new Promise(ok => setTimeout(ok, 2500))]);
  try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}
  try { await soon(Promise.all((await indexedDB.databases()).map(d => new Promise(ok => {
    const r = indexedDB.deleteDatabase(d.name); r.onsuccess = r.onerror = r.onblocked = ok; })))); } catch (e) {}
  try { await soon(Promise.all((await caches.keys()).map(k => caches.delete(k)))); } catch (e) {}
  try { await soon(Promise.all((await navigator.serviceWorker.getRegistrations()).map(r => r.unregister()))); } catch (e) {}
  window.__foxgloveCleared = true;
})(); true"""

    def __init__(self, profile: QWebEngineProfile, origins: list[str], thorough: bool = False, parent: QObject | None = None,
                 skip_visited: bool = False):
        super().__init__(parent)
        self.profile = profile
        self.origins = [o for o in dict.fromkeys(origins) if o]
        self.queue = list(self.origins)
        self.thorough = thorough
        self.skip_visited = skip_visited
        self.cleared: set[str] = set()  # origins whose storage was reached
        self.done: set[str] = set()     # origins dealt with
        self.visited: set[str] = set()  # origins a tab loaded since clearing began
        self._robots: dict[str, float | None] = {}  # origin -> until when its /robots.txt may still bring cookies
        self._workers = 0
        if thorough:  # (Qt's cookie filter would do, but PyQt can't take it off again: every request would wait on it)
            CookieIndex.of(profile).added.connect(self._robots_cookie)

    def visit(self, origin: str) -> None:
        """A tab loaded *origin*: with skip_visited, it's left alone if still waiting (what it stores now is new)."""
        self.visited.add(origin)
        if self.skip_visited and origin in self.queue:
            self.queue.remove(origin)

    def pending(self) -> list[str]:
        return [o for o in self.origins if o not in self.done and not (self.skip_visited and o in self.visited)]

    def start(self) -> None:
        self._workers = min(self.PARALLEL, len(self.queue))
        for _ in range(self._workers):
            page = QWebEnginePage(self.profile, self)
            page.settings().setAttribute(QWebEngineSettings.WebAttribute.AutoLoadImages, False)
            page.loadFinished.connect(lambda ok, p=page: p._on_load(ok))
            self._next(page)
        if not self._workers:
            QTimer.singleShot(0, self.finished.emit)

    def _robots_cookie(self, cookie: QNetworkCookie) -> None:
        """A cookie for an origin whose /robots.txt is loading came with it: delete it (it must not sign you back in)."""
        now = time.monotonic()
        for origin, until in list(self._robots.items()):
            if until is not None and until < now:
                del self._robots[origin]
            elif CookieIndex.sent_to(cookie, QUrl(f"https://{QUrl(origin).host()}{cookie.path() or '/'}")):
                CookieIndex.of(self.profile).delete([cookie])
                return

    def _next(self, page: QWebEnginePage) -> None:
        if not self.queue:
            page._on_load = lambda _ok: None
            page.deleteLater()
            self._workers -= 1
            if not self._workers:
                self.finished.emit()
            return
        origin = self.queue.pop(0)
        loads = [lambda: page.setHtml(self.BLANK, QUrl(origin + "/"))]
        url = QUrl(origin)
        if self.thorough and (url.scheme() == "https" or url.host() in ("localhost", "127.0.0.1", "::1")):  # secure: may have workers
            def robots_txt() -> None:
                self._robots[origin] = None  # until it's done: cookies for it meanwhile came with it
                page.load(QUrl(origin + "/robots.txt"))
            loads.append(robots_txt)
        self._step(page, origin, loads)

    def _step(self, page: QWebEnginePage, origin: str, loads: list) -> None:
        if sip.isdeleted(self) or sip.isdeleted(page):
            return
        if not loads:
            self.done.add(origin)
            if origin in self._robots:
                self._robots[origin] = time.monotonic()  # (a cookie it set was reported long before this)
            self._next(page)
            return
        token = page._token = object()
        started = time.monotonic()

        def current() -> bool:
            return not sip.isdeleted(page) and page._token is token

        def advance() -> None:
            if current():
                page._token = None
                self._step(page, origin, loads)

        def check() -> None:
            if current() and time.monotonic() - started < self.STEP_MS / 1000:
                page.runJavaScript("window.__foxgloveCleared === true",
                                   lambda done: (self.cleared.add(origin), advance()) if done else QTimer.singleShot(100, check))
            else:
                advance()

        def loaded(ok: bool) -> None:
            if current():
                if ok and origin_of(page.url()) == origin:
                    page.runJavaScript(self.JS)
                    check()
                else:  # offline, or the site sent us elsewhere: nothing of this origin to run in
                    advance()

        page._on_load = loaded
        QTimer.singleShot(self.STEP_MS + 500, advance)
        loads.pop(0)()


SITE_STORAGE = ("Local Storage", "Session Storage", "IndexedDB", "Service Worker", "WebStorage", "File System",
                "databases", "blob_storage")  # what web pages keep in Chromium's profile folder (not cookies or cache)
PENDING_CLEAR = "clear-site-data.json"  # clearing still running when Foxglove quit
_ORIGIN_BYTES = re.compile(rb"https?://[a-z0-9-]+(?:\.[a-z0-9-]+)*(?::\d{1,5})?")


def stored_origins(storage: Path) -> list[str]:
    """Origins with data in Chromium's storage folders, also ones history no longer knows of (best effort: IndexedDB's
    folder names and the origins in the raw bytes of the storage databases)."""
    found: dict[str, None] = {}
    for item in (storage / "IndexedDB").glob("*.indexeddb.leveldb"):
        if match := re.fullmatch(r"(https?)_([a-z0-9.-]+)_(\d+)\.indexeddb\.leveldb", item.name):
            found[f"{match[1]}://{match[2]}{'' if match[3] == '0' else ':' + match[3]}"] = None
    for pattern in ("Local Storage/leveldb/*", "Session Storage/*", "Service Worker/Database/*",
                    "Service Worker/CacheStorage/*/index.txt", "WebStorage/QuotaManager*"):
        for item in storage.glob(pattern):
            try:
                if item.is_file() and item.stat().st_size < 64 << 20:
                    found.update(dict.fromkeys(m.decode() for m in _ORIGIN_BYTES.findall(item.read_bytes())))
            except OSError:
                pass
    return [o for o in found if origin_of(QUrl(o)) == o]


def finish_clearing(profile_dir: Path, storage: Path) -> tuple[list[str], list[str]]:
    """Clearing that was still running when Foxglove last quit. Every site's, if nothing was loaded meanwhile:
    Chromium's storage folders are deleted, before the profile opens them (complete, unlike page by page). Otherwise
    returns the origins left: (one site's - thorough, every site's - storage only)."""
    data = read_json(profile_dir / PENDING_CLEAR, None)
    (profile_dir / PENDING_CLEAR).unlink(missing_ok=True)
    data = data if isinstance(data, dict) else {}
    if data.get("all"):
        for name in SITE_STORAGE:
            shutil.rmtree(storage / name, ignore_errors=True)
    lists = [data.get(key) if isinstance(data.get(key), list) else [] for key in ("origins", "storage")]
    return tuple([o for o in found if isinstance(o, str) and origin_of(QUrl(o)) == o] if not data.get("all") else []
                 for found in lists)


class FaviconCache(QObject):
    """Remembers site icons per host on disk, so bookmarks, history and restored tabs have favicons."""

    updated = pyqtSignal()

    def __init__(self, directory: Path):
        super().__init__()
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._icons: dict[str, QIcon | None] = {}
        self._digests: dict[str, str] = {}
        self._notify = QTimer(self)
        self._notify.setSingleShot(True)
        self._notify.setInterval(300)
        self._notify.timeout.connect(self.updated.emit)

    @staticmethod
    def _key(url) -> str:
        return (url if isinstance(url, QUrl) else QUrl(str(url))).host().lower()

    def _file(self, key: str) -> Path:
        return self.dir / (re.sub(r"[^a-z0-9.\-]", "_", key)[:150] + ".png")

    def get(self, url, fallback: bool = True) -> QIcon:
        key = self._key(url)
        if key and key not in self._icons:
            pixmap = QPixmap(str(self._file(key)))
            self._icons[key] = None if pixmap.isNull() else QIcon(pixmap)
        found = self._icons.get(key) if key else None
        if found is not None:
            return found
        return icon("globe", P.TEXT_3) if fallback else QIcon()

    def _store_pixmap(self, key: str, pixmap: QPixmap) -> None:
        if not key or pixmap.isNull():
            return
        if pixmap.width() > 64:
            pixmap = pixmap.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buffer, "PNG")
        buffer.close()
        digest = hashlib.sha1(bytes(data)).hexdigest()
        if self._digests.get(key) == digest:
            return
        self._digests[key] = digest
        self._icons[key] = QIcon(pixmap)
        try:
            self._file(key).write_bytes(bytes(data))
        except OSError:
            pass
        self._notify.start()

    def clear(self) -> None:
        self._icons.clear()
        self._digests.clear()
        for file in self.dir.glob("*.png"):
            try:
                file.unlink()
            except OSError:
                pass
        self._notify.start()

    def store(self, url, icon_: QIcon) -> None:
        if not icon_.isNull():
            self._store_pixmap(self._key(url), icon_.pixmap(QSize(32, 32)))

    def store_data_uri(self, url: str, data_uri: str) -> None:
        try:
            payload = base64.b64decode(data_uri.split(",", 1)[1])
        except (IndexError, ValueError):
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(payload):
            self._store_pixmap(self._key(url), pixmap)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Chrome extensions
# ══════════════════════════════════════════════════════════════════════════════════════════
@dataclass
class ExtensionEntry:
    id: str
    name: str
    description: str
    version: str
    path: str
    enabled: bool
    pinned: bool
    popup_url: QUrl
    options_url: QUrl
    icon: QIcon
    newtab_url: QUrl = dc_field(default_factory=QUrl)


class _Relay(QObject):
    """Carries results from worker threads back to the GUI thread."""
    done = pyqtSignal(object)


def _move(source: Path, target: Path) -> None:
    try:
        source.rename(target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:  # different file systems: copy instead
            raise
        shutil.move(str(source), str(target))


class ExtensionsController(QObject):
    """Installs and manages Chrome (Manifest V3) extensions through Qt WebEngine's extension system.

    Qt keeps installed extensions in the profile and reloads them at start-up - always *disabled*,
    so we remember which ones the user enabled and switch them back on. Every extension gets Foxglove's
    chrome.* polyfill wired in (inject_shim) before its code first runs; ExtensionBridge answers its calls.
    """

    changed = pyqtSignal()
    message = pyqtSignal(str, str)       # text, kind ("info", "success" or "error")
    reloaded = pyqtSignal(str)           # an extension was switched on: pages opened while it was off lack chrome.*
    replace_requested = pyqtSignal(dict)  # a package claims an installed extension's ID: ask before replacing it

    def __init__(self, profile: QWebEngineProfile, registry_path: Path, staging_dir: Path, user_agent: str):
        super().__init__()
        self.profile = profile
        self.manager = profile.extensionManager() if HAS_EXTENSIONS else None
        self.registry_path = registry_path
        raw = read_json(registry_path, {})
        self.registry: dict[str, dict] = raw if isinstance(raw, dict) else {}
        self.staging = staging_dir
        self.secret = self._load_secret(registry_path.with_name("extension-bridge.key"))
        self.locale = ui_locale()
        self.stamp = shim_stamp(self.secret, self.locale)
        restored = self._recover_interrupted_updates()
        self._clean_staging()
        # A new browser session: registered content scripts with persistAcrossSessions: false end, as in Chrome. Their
        # extensions' manifests are brought in line when they're first switched on (Qt may have read them already).
        self._session_scripts_ended: set[str] = set()
        for ext_id, state in self.registry.items():
            scripts = state.get("scripts") if isinstance(state, dict) else None
            if isinstance(scripts, list) and any(isinstance(s, dict) and s.get("persistAcrossSessions") is False for s in scripts):
                state["scripts"] = [s for s in scripts if not (isinstance(s, dict) and s.get("persistAcrossSessions") is False)]
                self._session_scripts_ended.add(ext_id)
        if self._session_scripts_ended:
            self.save()
        self.user_agent = user_agent
        self._jobs: dict[str, dict] = {}       # staging folder name -> new install in progress
        self._updates: dict[str, dict] = {}    # extension id -> update (or shim upgrade) in progress
        self._loading: dict[str, dict] = {}    # extension folder -> update waiting for Qt to load it
        self._installing: set[str] = set()     # ids being installed right now (ignore duplicate requests)
        self._removing: set[str] = set()       # ids being uninstalled right now
        self._reshim_failed: set[str] = set()  # couldn't get the polyfill: runs as it is
        self._manifests: dict[str, tuple[float, dict]] = {}
        self._configs: dict[str, tuple[float, dict]] = {}
        self._relays: set[_Relay] = set()
        self._rejected: set[str] = set()       # installed copies taken out again (their ID wasn't the checked one)
        self._restore_after_reject: dict[str, tuple[str, str]] = {}  # id -> (refused copy, the installed one to load again)
        self._recovered: set[str] = set()      # extension folders loaded again without their registered content scripts
        self._reloads: dict[str, dict] = {}    # registered content scripts changed: reload {"previous" manifest.json, "before" scripts, "since", "due"}
        self._script_reloads: dict[str, list[float]] = {}  # ext id -> when such reloads began (the last minute's)
        self._bad_scripts: dict[str, dict[str, str]] = {}  # id -> {registered script (or "set:" all of them) Qt didn't load: its error}
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(1000)
        self._reload_timer.timeout.connect(self._reload_for_scripts)
        self.chromium = qWebEngineChromiumVersion() if HAS_EXTENSIONS else ""
        self._window = None
        self._nam = QNetworkAccessManager(self)
        self.bridge = ExtensionBridge(self)
        self.net = NetRules(self)
        self.changed.connect(self.net.invalidate)
        # Without extensions, Foxglove adds nothing to browsing: no request filter, no foxglove-ext:// - both come
        # with the first extension that needs them.
        self._bridge_on = self.filtering = self._guarding = False
        self._net_filter = None
        if self.manager is not None:
            self._net_filter = NetFilter(self.net, None, profile)  # the profile's: lives as long as it does
            if self.registry or restored:
                self._ensure_bridge()
            self.manager.loadFinished.connect(self._on_load_finished)
            self.manager.installFinished.connect(self._on_install_finished)
            self.manager.uninstallFinished.connect(self._on_uninstall_finished)
            self.manager.unloadFinished.connect(self._on_unload_finished)
            for folder in restored:
                self.manager.loadExtension(folder)
            QTimer.singleShot(0, self._sync_enabled)
            QTimer.singleShot(2500, self._sync_enabled)

    @property
    def available(self) -> bool:
        return self.manager is not None

    def _ensure_bridge(self) -> None:
        """Answer foxglove-ext:// - from the first extension on (until then the scheme is as unknown as anywhere else)."""
        if self._bridge_on or self.manager is None:
            return
        self._bridge_on = True
        if not QWebEngineUrlScheme.schemeByName(EXT_SCHEME.encode()).name().isEmpty():
            self.profile.installUrlSchemeHandler(EXT_SCHEME.encode(), self.bridge)
        else:
            log(f"{EXT_SCHEME}:// isn't registered - extensions run without {APP_NAME}'s API polyfill.")

    def sync_filtering(self) -> None:
        """Filter requests only while an enabled extension may have declarativeNetRequest rules (and look at requests
        for extension files while there are extensions)."""
        if self.manager is None or sip.isdeleted(self.manager):
            return
        bridge = self.bridge
        want = any(i.isEnabled() and (bridge.has_permission(i.id(), "declarativeNetRequest")
                                      or bridge.has_permission(i.id(), "declarativeNetRequestWithHostAccess"))
                   for i in self._infos())
        guard = bool(self._infos())  # (the profile's filter also keeps the polyfill's files to the extensions themselves)
        if (want or guard) != (self.filtering or self._guarding):
            self.profile.setUrlRequestInterceptor(self._net_filter if want or guard else None)
        self._guarding = guard
        if want != self.filtering:
            self.filtering = want
            QTimer.singleShot(0, self.changed.emit)  # the windows give their tabs' filters the same state

    @property
    def window(self):
        win = self._window() if self._window is not None else None
        return win if win is not None and not sip.isdeleted(win) else None

    def attach_window(self, win) -> None:
        self._window = weakref.ref(win)

    TAB_SCRIPT = "foxglove-tab-id"

    def tab_script(self, tab_id: int) -> QWebEngineScript | None:
        """Tells the polyfill (content scripts, extension pages) which tab it is in. DOM events reach every world of a
        page, so each extension asks and is answered under event names only it and Foxglove know."""
        if self.manager is None:
            return None
        pairs = {}
        for info in self._infos():
            cfg = self.shim_config(info.id())
            q, a = cfg.get("tabQuery"), cfg.get("tabAnswer")
            if isinstance(q, str) and isinstance(a, str) and re.fullmatch(r"__fg[0-9a-f]{20}", q) and re.fullmatch(r"__fg[0-9a-f]{20}", a):
                pairs[q] = a
        if not pairs:  # no extension to tell: nothing in the page
            return None
        script = QWebEngineScript()
        script.setName(self.TAB_SCRIPT)
        script.setWorldId(APP_WORLD)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setRunsOnSubFrames(True)
        script.setSourceCode(f"for (const [q, a] of Object.entries({json.dumps(pairs)})) document.addEventListener(q, () => "
                             f"document.dispatchEvent(new CustomEvent(a, {{detail: '{int(tab_id)}'}})));")
        return script

    def wire_tab(self, page: QWebEnginePage, tab_id: int) -> None:
        """Put (or refresh) the tab-id script in a tab's page; it applies from the next page load on. The page also
        gets a request filter that knows its tab (declarativeNetRequest rules for some tabs only) - while filtering."""
        if self.manager is not None:
            if not isinstance(getattr(page, "net_filter", None), NetFilter):
                page.net_filter = NetFilter(self.net, tab_id, page)
            if getattr(page, "net_filtering", False) != self.filtering:
                page.net_filtering = self.filtering
                page.setUrlRequestInterceptor(page.net_filter if self.filtering else None)
        script, scripts = self.tab_script(tab_id), page.scripts()
        old = scripts.find(self.TAB_SCRIPT)
        if script is not None and len(old) == 1 and old[0].sourceCode() == script.sourceCode():
            return
        for item in old:
            scripts.remove(item)
        if script is not None:
            scripts.insert(script)

    def file_access(self, ext_id: str) -> bool:
        """Chrome's "Allow access to file URLs" (off unless the user turns it on)."""
        return bool((self.registry.get(ext_id) or {}).get("file_access"))

    def set_file_access(self, ext_id: str, allowed: bool) -> None:
        self.registry.setdefault(ext_id, {})["file_access"] = bool(allowed)
        self.save()
        self.changed.emit()

    def wants_file_access(self, ext_id: str) -> bool:
        """Whether the extension's host permissions or content scripts would reach file: pages."""
        return any(match_pattern(h, "file:///index.html") for h in self.bridge._hosts(ext_id, scripts=True))

    def save(self) -> None:
        write_json(self.registry_path, self.registry)

    @staticmethod
    def _load_secret(path: Path) -> str:
        try:
            value = path.read_text(encoding="ascii").strip()
            if re.fullmatch(r"[0-9a-f]{32}", value):
                return value
        except (OSError, UnicodeError):
            pass
        value = secrets.token_hex(16)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="ascii")
            os.chmod(path, 0o600)
        except OSError as exc:
            log(f"Couldn't save {path.name}: {exc}")
        return value

    # Queries
    def _infos(self) -> list:
        if self.manager is None or sip.isdeleted(self.manager):  # (a late timer after the profile went away)
            return []
        return [i for i in self.manager.extensions() if i.isInstalled() and i.id() not in COMPONENT_EXTENSIONS]

    def _info(self, ext_id: str):
        return next((i for i in self._infos() if i.id() == ext_id), None) if ext_id else None

    def _manifest(self, path: str) -> dict:
        try:
            mtime = (Path(path) / "manifest.json").stat().st_mtime
        except OSError:
            return {}
        cached = self._manifests.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            manifest = load_manifest(Path(path))
        except (OSError, ValueError):
            manifest = {}
        self._manifests[path] = (mtime, manifest)
        return manifest

    def shim_config(self, ext_id: str) -> dict:
        """The polyfill configuration of an installed extension ({} if it runs without one)."""
        info = self._info(ext_id)
        if info is None:
            return {}
        path = info.path()
        try:
            mtime = (Path(path) / SHIM_FILE).stat().st_mtime
        except OSError:
            return {}
        cached = self._configs.get(path)
        if cached is None or cached[0] != mtime:
            cached = self._configs[path] = (mtime, installed_shim_config(Path(path)))
        return cached[1]

    def _needs_shim(self, info) -> bool:
        try:
            with open(Path(info.path()) / SHIM_FILE, "rb") as fh:
                head = fh.read(80).decode("utf-8", "replace")
        except OSError:
            return True
        return not head.startswith(f"/* foxglove-shim {self.stamp}\n")

    @staticmethod
    def _icon_for(manifest: dict, root: Path) -> QIcon:
        candidates: list[tuple[int, str]] = []
        for source in ((manifest.get("action") or {}).get("default_icon"), manifest.get("icons")):
            if isinstance(source, str):
                candidates.append((0, source))
            elif isinstance(source, dict):
                for size, rel in source.items():
                    if isinstance(rel, str):
                        candidates.append((int(size) if str(size).isdigit() else 0, rel))
        result = QIcon()
        base = root.resolve()
        for _size, rel in sorted(candidates, key=lambda c: -c[0]):
            target = (base / rel.lstrip("/")).resolve()
            if target.is_file() and base in target.parents:
                result.addFile(str(target))
        return result if not result.isNull() else icon("puzzle", P.TEXT_2)

    def entries(self) -> list[ExtensionEntry]:
        out = []
        for info in self._infos():
            manifest = self._manifest(info.path())
            options = (manifest.get("options_ui") or {}).get("page") if isinstance(manifest.get("options_ui"), dict) else None
            options = options or manifest.get("options_page")
            options_url = QUrl(f"chrome-extension://{info.id()}/{options.lstrip('/')}") if isinstance(options, str) and options else QUrl()
            newtab = (manifest.get("chrome_url_overrides") or {}).get("newtab") if isinstance(manifest.get("chrome_url_overrides"), dict) else None
            state = self.registry.get(info.id(), {})
            out.append(ExtensionEntry(
                id=info.id(), name=info.name() or "Extension", description=info.description(),
                version=str(manifest.get("version", "")), path=info.path(), enabled=info.isEnabled(),
                pinned=bool(state.get("pinned", True)), popup_url=info.actionPopupUrl(),
                options_url=options_url, icon=self._icon_for(manifest, Path(info.path())),
                newtab_url=QUrl(f"chrome-extension://{info.id()}/{newtab.lstrip('/')}") if isinstance(newtab, str) and newtab.strip("/") else QUrl()))
        out.sort(key=lambda e: e.name.lower())
        NEWTAB_OVERRIDES.update(e.newtab_url.toString() for e in out if not e.newtab_url.isEmpty())
        return out

    def entry(self, ext_id: str) -> ExtensionEntry | None:
        return next((e for e in self.entries() if e.id == ext_id), None)

    def commands(self, ext_id: str) -> dict[str, dict]:
        info = self._info(ext_id)
        commands = self._manifest(info.path()).get("commands") if info is not None else None
        return {k: v for k, v in commands.items() if isinstance(v, dict)} if isinstance(commands, dict) else {}

    def newtab_override(self) -> QUrl | None:
        """An enabled extension's New Tab page (chrome_url_overrides) - the most recently installed one wins."""
        pages = [e for e in self.entries() if e.enabled and not e.newtab_url.isEmpty()]
        pages.sort(key=lambda e: self.registry.get(e.id, {}).get("installed", 0))
        return pages[-1].newtab_url if pages else None

    # Enable / disable / pin / remove
    def _on_load_finished(self, info) -> None:
        job = self._loading.pop(os.path.realpath(info.path()), None) if info.path() else None
        if job is not None:
            self._finish_update(job, info)
            return
        if not info.isLoaded():
            if info.error():
                log(f"An installed extension failed to load ({info.path()}): {info.error()}")
                self._load_without_registered(info.path())
            return
        if info.isInstalled():
            self._ensure_bridge()
            ext_id = info.id()
            QTimer.singleShot(0, lambda: self._apply_enabled(ext_id))

    def _load_without_registered(self, path: str) -> None:
        """An installed extension that doesn't load with the content scripts it registered (written into its manifest -
        say Foxglove quit before the reload that would have found out): load it without them, once."""
        root = Path(path)
        try:
            if path in self._recovered or os.path.realpath(root.parent) != os.path.realpath(self.manager.installPath()):
                return
            original, current = _read_json_file(root / SHIM_ORIGINAL), _read_json_file(root / "manifest.json")
        except (OSError, ValueError, AttributeError):
            return
        if not isinstance(original, dict) or not isinstance(current, dict):
            return
        declared = wire_content_scripts(original.get("content_scripts"))
        if (current.get("content_scripts") or []) == declared:
            return  # not the registered scripts' doing
        self._recovered.add(path)
        if declared:
            current["content_scripts"] = declared
        else:
            current.pop("content_scripts", None)
        try:
            _replace_file(root / "manifest.json", json.dumps(current, ensure_ascii=False, indent=2).encode("utf-8"))
            ext_id = extension_id_from_key(manifest_key_bytes(current.get("key")))
        except OSError:
            return
        except ValueError:
            ext_id = ""
        if isinstance(self.registry.get(ext_id), dict) and self.registry[ext_id].pop("scripts", None) is not None:
            self.save()
        log(f"Loading {path} again without the content scripts it registered.")
        self.manager.loadExtension(path)

    def _sync_enabled(self) -> None:
        for info in self._infos():
            self._apply_enabled(info.id())

    def _apply_enabled(self, ext_id: str) -> None:
        info = self._info(ext_id)
        if info is None or ext_id in self._updates:
            return
        self._ensure_bridge()
        self.sync_filtering()
        if (self.registry.get(ext_id) or {}).get("remove"):  # asked for while an update was under way (and Foxglove quit)
            self.uninstall(ext_id)
            return
        want = bool(self.registry.get(ext_id, {}).get("enabled", True))
        if info.isLoaded() and want and ext_id not in self._reshim_failed and self._needs_shim(info):
            # Wired by an older Foxglove (or for another UI language): give it the current polyfill. Qt only drops a
            # worker's cached scripts when it unloads an extension that was on - so switch it on first. One the user
            # turned off waits until they turn it on again.
            if not info.isEnabled():
                self.manager.setExtensionEnabled(info, True)
            self._reshim(info)
            return
        if ext_id in self._session_scripts_ended and info.isLoaded():  # (re-wiring writes them too)
            self._session_scripts_ended.discard(ext_id)
            self.sync_registered(ext_id)  # reloads it - unless it registers the same scripts again first, as most do at start
        if info.isEnabled() != want:
            self.manager.setExtensionEnabled(info, want)
            if want:
                self.bridge.extension_enabled(ext_id)
                self.reloaded.emit(ext_id)
            else:
                self.bridge.extension_disabled(ext_id)
        self.changed.emit()

    def set_enabled(self, ext_id: str, enabled: bool) -> None:
        self.registry.setdefault(ext_id, {})["enabled"] = bool(enabled)
        self.save()
        self._apply_enabled(ext_id)

    def set_pinned(self, ext_id: str, pinned: bool) -> None:
        self.registry.setdefault(ext_id, {})["pinned"] = bool(pinned)
        self.save()
        self.changed.emit()

    def uninstall(self, ext_id: str) -> None:
        job = self._updates.get(ext_id)
        if job is not None and "reload" in job and not job.get("unloading"):  # a reload that hasn't begun: not needed now
            self._updates.pop(ext_id)
            self._next_after(job, drop=True)
            job = None
        if job is not None:  # never drop a removal: it follows once the update is through - or at the next start
            job["remove_after"] = True
            self.registry.setdefault(ext_id, {})["remove"] = True
            self.save()
            name = job.get("name") or "The extension"
            self.message.emit(f"“{name}” will be removed in a moment." if job.get("silent")  # (Foxglove's own: no update to speak of)
                              else f"“{name}” will be removed once its update finishes.", "info")
            return
        info = self._info(ext_id)
        if info is None or ext_id in self._removing:
            return
        self._removing.add(ext_id)
        self.bridge.extension_disabled(ext_id)
        if info.isEnabled():
            self.manager.setExtensionEnabled(info, False)
        self.manager.uninstallExtension(info)

    def _on_uninstall_finished(self, info) -> None:
        if info.path() in self._rejected:
            self._rejected.discard(info.path())
            return
        self._removing.discard(info.id())
        if info.error():
            (self.registry.get(info.id()) or {}).pop("remove", None)
            self.message.emit(f"Couldn't remove the extension: {info.error()}", "error")
        else:
            self.registry.pop(info.id(), None)
            self.bridge.extension_removed(info.id())
            self.save()
            self.message.emit(f"“{info.name() or 'The extension'}” was removed.", "success")
        self.changed.emit()

    # Installing
    def install_from_path(self, path: str) -> None:
        if not self.available:
            self.message.emit("Extensions need Qt WebEngine 6.10 or newer: python3 -m pip install --upgrade PyQt6 PyQt6-WebEngine", "error")
            return
        self._stage_in_background(lambda: self._stage(path=Path(path)), source="file")

    def install_from_webstore(self, ext_id: str, title: str = "") -> None:
        if not self.available:
            self.message.emit("Extensions need Qt WebEngine 6.10 or newer: python3 -m pip install --upgrade PyQt6 PyQt6-WebEngine", "error")
            return
        request = QNetworkRequest(QUrl(WEBSTORE_CRX_URL.format(version=qWebEngineChromiumVersion(), id=ext_id)))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, self.user_agent)
        request.setTransferTimeout(90_000)
        reply = self._nam.get(request)
        name = title or "the extension"
        self.message.emit(f"Downloading {name} from the Chrome Web Store…", "info")
        reply.finished.connect(lambda: self._on_crx_downloaded(reply, ext_id, name))

    def _on_crx_downloaded(self, reply: QNetworkReply, ext_id: str, name: str) -> None:
        reply.deleteLater()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self.message.emit(f"Couldn't download {name}: {reply.errorString()}", "error")
            return
        data = bytes(reply.readAll())
        if status not in (None, 200) or not data:
            self.message.emit(f"The Chrome Web Store didn't provide {name} (HTTP {status}).", "error")
            return
        self._stage_in_background(lambda: self._stage(data=data, expected_id=ext_id), source="webstore")

    def _in_background(self, work, done) -> None:
        relay = _Relay()
        self._relays.add(relay)

        def finished(result) -> None:
            self._relays.discard(relay)
            done(result)

        relay.done.connect(finished)

        def run() -> None:
            try:
                result = work()
            except InstallError as exc:
                result = exc
            except Exception as exc:  # unexpected file problems are reported, never fatal
                result = InstallError(f"The extension couldn't be installed: {exc}")
            try:
                relay.done.emit(result)
            except RuntimeError:  # Foxglove quit meanwhile (the relay went with it): nobody to tell - staging is tidied at the next start
                pass

        threading.Thread(target=run, daemon=True).start()

    def _stage_in_background(self, work, source: str) -> None:
        def done(result) -> None:
            if isinstance(result, Exception):
                self.message.emit(str(result) or "The extension couldn't be installed.", "error")
                return
            result["source"] = source
            self._on_staged(result)

        self._in_background(work, done)

    def _stage(self, path: Path | None = None, data: bytes | None = None, expected_id: str | None = None) -> dict:
        """Prepare a clean copy of the extension for Qt's installer (runs on a worker thread)."""
        # Qt's installer names its copy "<folder>_XXXXXX" with a fixed suffix, so every staging folder must be unique.
        target = self.staging / f"ext-{uuid.uuid4().hex[:12]}"
        target.parent.mkdir(parents=True, exist_ok=True)
        key = None
        try:
            if path is not None and path.is_dir():
                if not (path / "manifest.json").is_file():
                    raise InstallError("That folder doesn't contain a manifest.json file, so it isn't an unpacked extension.")
                shutil.copytree(path, target, ignore=shutil.ignore_patterns(".git", "_metadata", "__MACOSX"))
            else:
                if data is None:
                    if path is None or not path.is_file():
                        raise InstallError("The extension file couldn't be found.")
                    data = path.read_bytes()
                archive, key = parse_crx(data)
                try:
                    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                        _safe_extract(zf, target)
                except zipfile.BadZipFile as exc:
                    raise InstallError("The extension package is damaged or isn't a Chrome extension.") from exc
                if not (target / "manifest.json").is_file():
                    inner = [p for p in target.iterdir() if p.is_dir() and (p / "manifest.json").is_file()]
                    if len(inner) > 1:
                        raise InstallError(f"The package contains several extensions ({', '.join(sorted(p.name for p in inner))}); "
                                           "unpack it and install one folder at a time.")
                    if not inner:
                        raise InstallError("The package doesn't contain a manifest.json file.")
                    holding = target.with_name(target.name + "-inner")
                    inner[0].rename(holding)
                    shutil.rmtree(target)
                    holding.rename(target)
            shutil.rmtree(target / "_metadata", ignore_errors=True)  # Chrome Web Store signatures: not needed unpacked
            try:
                manifest = load_manifest(target)
            except (OSError, ValueError) as exc:
                raise InstallError(f"The extension's manifest.json can't be read: {exc}") from exc
            name = localized(target, manifest, manifest.get("name")) or "This extension"
            version = manifest.get("manifest_version")
            if version != 3:
                raise InstallError(
                    f"“{name}” uses Manifest V{version}. {APP_NAME}'s engine (Qt WebEngine) only runs Manifest V3 "
                    "extensions - the kind the Chrome Web Store ships today.")
            source_path = str(path.resolve()) if path is not None else None
            derived = None
            if key:  # packed: the signed CRX key decides the ID, as in Chrome
                ext_id, id_source = extension_id_from_key(key), "crx"
                manifest["key"] = base64.b64encode(key).decode("ascii")
            elif "key" in manifest:  # read as Chrome does (PEM or base64) - and written back plain, so Qt goes by the same bytes
                try:
                    key_bytes = manifest_key_bytes(manifest["key"])
                except ValueError as exc:  # never install it under an ID nobody checked
                    raise InstallError(f"“{name}” can't be installed: its manifest.json has an invalid “key”.") from exc
                ext_id, id_source = extension_id_from_key(key_bytes), "manifest"
                manifest["key"] = base64.b64encode(key_bytes).decode("ascii")
            else:  # unpacked: a key made from where it came from keeps the ID (and its data) stable, as in Chrome
                derived = base64.b64encode(hashlib.sha256(f"{DATA_NAME}:{source_path or uuid.uuid4()}".encode()).digest()).decode("ascii")
                ext_id, id_source = None, None
            if expected_id and ext_id != expected_id:
                raise InstallError("The downloaded file doesn't match the requested extension.")
            inject_shim(target, manifest, self.secret, self.locale)
            return {"dir": str(target), "name": name, "id": ext_id, "id_source": id_source, "derived_key": derived,
                    "source_path": source_path, "version": str(manifest.get("version", "")), "unpacked": key is None and data is None,
                    "needs_chrome": self._needs_newer_engine(manifest.get("minimum_chrome_version"))}
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def _needs_newer_engine(self, wanted) -> str:
        """The Chrome version the manifest asks for, if the engine is older ("" otherwise)."""
        version = lambda text: tuple(int(n) for n in re.findall(r"\d+", str(text))[:4])
        return str(wanted) if isinstance(wanted, str) and version(wanted) and self.chromium and version(wanted) > version(self.chromium) else ""

    @staticmethod
    def _set_key(directory: Path, key: str) -> None:
        for name in ("manifest.json", SHIM_ORIGINAL):
            try:
                manifest = _read_json_file(directory / name)
                manifest["key"] = key
                (directory / name).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            except (OSError, ValueError, TypeError):
                pass

    def _on_staged(self, job: dict) -> None:
        ext_id = job.get("id")
        source_path = job.get("source_path")
        if self._after_silent(ext_id, job):
            return
        busy_paths = {j.get("source_path") for j in [*self._jobs.values(), *self._updates.values()] if not j.get("silent")} - {None}
        if (ext_id and (ext_id in self._installing or ext_id in self._updates)) or (source_path and source_path in busy_paths):
            shutil.rmtree(job["dir"], ignore_errors=True)
            self.message.emit(f"“{job.get('name')}” is already being installed.", "info")
            return
        if not ext_id and job.get("derived_key"):
            derived = extension_id_from_key(base64.b64decode(job["derived_key"]))
            legacy = next((eid for eid, state in self.registry.items() if isinstance(state, dict)
                           and state.get("source_path") == source_path and self._info(eid) is not None), None) if source_path else None
            if self._info(derived) is None and legacy is not None:
                ext_id = legacy  # installed before IDs were derived: its ID is its folder's, so it must stay keyless
            else:
                ext_id = derived
                self._set_key(Path(job["dir"]), job["derived_key"])
            if self._after_silent(ext_id, job):
                return
            if ext_id in self._installing or ext_id in self._updates:
                shutil.rmtree(job["dir"], ignore_errors=True)
                self.message.emit(f"“{job.get('name')}” is already being installed.", "info")
                return
        job["id"] = ext_id
        if ext_id:
            substitute_css(Path(job["dir"]), ext_id, self.locale)
        existing = self._info(ext_id)
        if existing is None:
            self._install_staged(job)
        elif self._replacing_needs_consent(ext_id, job):
            if self.receivers(self.replace_requested) == 0:
                shutil.rmtree(job["dir"], ignore_errors=True)
                self.message.emit(f"“{existing.name() or job.get('name')}” is already installed from somewhere else. "
                                  "Remove it first to install this copy.", "error")
                return
            self._installing.add(ext_id)
            job["existing_name"] = existing.name() or "the extension"
            self.replace_requested.emit(job)
        else:
            self._start_update(existing, job)

    def _replacing_needs_consent(self, ext_id: str, job: dict) -> bool:
        """Anyone can copy a public key into a manifest: only a signed .crx (or the same source) may silently update."""
        if job.get("id_source") == "crx" or not job.get("id_source") or job.get("consented"):
            return False
        state = self.registry.get(ext_id, {})
        return state.get("source_path") != job.get("source_path") or state.get("source") != job.get("source")

    def resolve_replace(self, job: dict, accepted: bool) -> None:
        ext_id = job.get("id") or ""
        self._installing.discard(ext_id)
        existing = self._info(ext_id)
        if accepted and existing is not None:
            job["consented"] = True  # (not asked again if it has to wait)
            if ext_id not in self._updates:
                self._start_update(existing, job)
                return
            if self._after_silent(ext_id, job):  # Foxglove reloads or re-wires it right now: right after that
                return
        shutil.rmtree(job["dir"], ignore_errors=True)
        if not accepted:
            self.message.emit(f"“{job.get('existing_name') or job.get('name')}” was left as it was.", "info")
        else:
            self.message.emit(f"“{job.get('existing_name') or job.get('name')}” couldn't be replaced: "
                              + ("it is being updated." if existing is not None else "it was removed."), "error")

    # Updates replace the files inside the extension's existing folder, so its ID - and with it the
    # extension's saved data - stays the same. The previous version is kept until the new one loads.
    RESTORE_MARKER = ".foxglove-restore-to"

    def _after_silent(self, ext_id: str | None, job: dict) -> bool:
        """An update the user asks for while Foxglove re-wires or reloads the extension itself: it follows right after
        (_next_after)."""
        current = self._updates.get(ext_id) if ext_id else None
        if current is None or not current.get("silent"):
            return False
        if current.get("then"):  # (asked for twice: the newer one)
            shutil.rmtree(current["then"]["dir"], ignore_errors=True)
        current["then"] = job
        return True

    def _next_after(self, job: dict, drop: bool = False) -> None:
        then = job.pop("then", None)
        if then is None:
            return
        if drop or job.get("remove_after"):
            shutil.rmtree(then["dir"], ignore_errors=True)
        else:
            QTimer.singleShot(0, lambda: self._on_staged(then))

    def _start_update(self, info, job: dict) -> None:
        job.update(ext_id=info.id(), target=info.path(), name=info.name() or job.get("name"), backup=None,
                   previous=str(self._manifest(info.path()).get("version", "")))
        job.pop("unloaded", None)
        self._updates[info.id()] = job
        self.bridge.extension_disabled(info.id())
        self.manager.unloadExtension(info)  # continues in _on_unload_finished

    def _reshim(self, info) -> None:
        """Give an extension installed by an older Foxglove the current polyfill (same folder, so the same ID)."""
        ext_id, source = info.id(), Path(info.path())
        state = self.registry.get(ext_id, {})
        job = {"silent": True, "name": info.name(), "source": state.get("source", "file"), "source_path": state.get("source_path"),
               "reshim": True, **self._applied_scripts(ext_id, info.path())}
        registered = [entry for _sid, entry in job["applied_cs"]]
        self._updates[ext_id] = job

        def work() -> dict:
            target = self.staging / f"ext-{uuid.uuid4().hex[:12]}"
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copytree(source, target)
                try:
                    manifest = load_manifest(target)
                except (OSError, ValueError) as exc:
                    raise InstallError(str(exc)) from exc
                inject_shim(target, manifest, self.secret, self.locale, registered)
                substitute_css(target, ext_id, self.locale)
            except BaseException:
                shutil.rmtree(target, ignore_errors=True)
                raise
            return {"dir": str(target)}

        def done(result) -> None:
            current = self._info(ext_id)
            if isinstance(result, Exception) or current is None or self._updates.get(ext_id) is not job:
                if not isinstance(result, Exception):
                    shutil.rmtree(result["dir"], ignore_errors=True)
                log(f"Couldn't add {APP_NAME}'s API support to {info.path()}: {result}")
                if self._updates.get(ext_id) is job:
                    self._updates.pop(ext_id)
                    if current is not None and current.isEnabled():  # switched on for the update: off, so it's turned on properly
                        self.manager.setExtensionEnabled(current, False)
                self._next_after(job)
                self._reshim_failed.add(ext_id)
                QTimer.singleShot(0, lambda: self.uninstall(ext_id) if job.get("remove_after") else self._apply_enabled(ext_id))
                return
            job.update(result)
            self._start_update(current, job)

        self._in_background(work, done)

    def sync_registered(self, ext_id: str, before: list | None = None) -> None:
        """Write the extension's registered content scripts into its installed manifest and reload it (Qt reads them
        when it loads an extension). *before*: the registered scripts as they were (put back if the reload fails)."""
        info = self._info(ext_id)
        if info is None:
            return
        if ext_id in self._updates:  # an update or reload writes them once it's through (_finish_update)
            return
        root = Path(info.path())
        try:
            original, current = _read_json_file(root / SHIM_ORIGINAL), _read_json_file(root / "manifest.json")
        except (OSError, ValueError):
            return
        if not isinstance(original, dict) or not isinstance(current, dict):
            return
        scripts = wire_content_scripts(original.get("content_scripts")) + wire_content_scripts(self.bridge.registered_scripts(ext_id))
        if scripts == (current.get("content_scripts") or []):
            return
        if scripts:
            current["content_scripts"] = scripts
        else:
            current.pop("content_scripts", None)
        try:
            previous = (root / "manifest.json").read_bytes()
            _replace_file(root / "manifest.json", json.dumps(current, ensure_ascii=False, indent=2).encode("utf-8"))
        except OSError as exc:
            log(f"Couldn't save the content scripts of “{info.name()}”: {exc}")
            return
        if before is None:
            before = [dict(s) for s in self.bridge._registered(ext_id)]
        # what to go back to if the new manifest doesn't load
        now = time.monotonic()
        wait = self._reloads.setdefault(ext_id, {"previous": previous, "before": before, "since": now})
        # Reloaded once the scripts stop changing for a while - longer for each reload of the last minute: a reload
        # starts the worker again, and one that changes its scripts in steps further apart than the wait (a default
        # first, then a setting it loads) would otherwise have it reloaded each time it starts, for ever.
        recent = self._script_reloads[ext_id] = [t for t in self._script_reloads.get(ext_id, []) if now - t < 60]
        wait["due"] = now + min(self.RELOAD_SETTLE * 2 ** len(recent), self.RELOAD_SETTLE_MAX)
        self._reload_timer.start()

    RELOAD_WAIT = 8.0  # s
    RELOAD_SETTLE, RELOAD_SETTLE_MAX = 1.0, 30.0  # s

    def _extension_tabs(self, ext_id: str) -> list:
        """The tabs and windows showing one of the extension's pages (a restored tab not opened yet has no page to lose)."""
        return [t for t in self.bridge.tabs() if getattr(t, "pending", None) is None and not sip.isdeleted(t)
                and t.url().scheme() == "chrome-extension" and t.url().host() == ext_id]

    def _holds_reload(self, ext_id: str, since: float) -> bool:
        """Whether a reload of the extension has to wait: it would close its open pop-up, and reload its pages in tabs
        (losing what was typed there). Wait for as long as the user looks at one - its pop-up, its page as the current
        tab or in a window of its own (not its New Tab page) - and a few seconds for one in a tab in the background."""
        win = self.window
        if win is None:
            return False
        if any(p.ext_id == ext_id and p.isVisible() for p in win.findChildren(ExtensionPopup) if not sip.isdeleted(p)):
            return True
        tabs, info = self._extension_tabs(ext_id), self._info(ext_id)
        overrides = self._manifest(info.path()).get("chrome_url_overrides") if tabs and info is not None else None
        newtab = overrides.get("newtab") if isinstance(overrides, dict) else None
        newtab = "/" + newtab.lstrip("/") if isinstance(newtab, str) else None
        if any((t is win.current_tab() or (isinstance(t, PopupWindow) and t.isVisible())) and t.url().path() != newtab for t in tabs):
            return True
        return bool(tabs) and time.monotonic() - since < self.RELOAD_WAIT

    def _reopen_pages(self, ext_id: str, pages: list) -> None:
        """Open the extension's pages again in the tabs that showed them when it was reloaded."""
        for ref, url in pages:
            tab = ref()
            if tab is None or sip.isdeleted(tab) or getattr(tab, "pending", None) is not None or not self.bridge._enabled(ext_id):
                continue
            now = tab.url()
            if now == url or (now.scheme() == "chrome-extension" and now.host() == ext_id):  # (not if the user went elsewhere)
                tab.load(QUrl(url))

    def _reload_for_scripts(self) -> None:
        """Reload the extensions whose registered content scripts changed (Qt reads them when it loads one) - once the
        calls have settled, and not while the user looks at one of the extension's pages (_holds_reload). Its
        chrome.storage.session is kept across the reload, and its pages in tabs are opened again afterwards."""
        for ext_id, wait in list(self._reloads.items()):
            info = self._info(ext_id)
            try:  # back to what it loaded with (unregistered and registered again, as some do at every start): no reload
                same = info is not None and _read_json_file(Path(info.path()) / "manifest.json") == json.loads(wait["previous"])
            except (OSError, ValueError):
                same = False
            if info is None or ext_id in self._updates or same:  # gone, or an update writes them itself
                self._reloads.pop(ext_id)
            elif time.monotonic() < wait.get("due", 0) - 0.1:  # (the timer may fire a little early)
                continue
            elif not self._holds_reload(ext_id, wait["since"]):
                self._reloads.pop(ext_id)
                self._script_reloads.setdefault(ext_id, []).append(time.monotonic())
                # "applied": the registered scripts in the manifest it is reloaded with (each change since wrote it)
                job = {"reload": wait["previous"], "scripts_before": wait["before"], "silent": True, "name": info.name(),
                       "ext_id": ext_id, "target": info.path(), "unloaded": True, **self._applied_scripts(ext_id, info.path()),
                       "pages": [(weakref.ref(t), t.url()) for t in self._extension_tabs(ext_id)]}
                self._updates[ext_id] = job
                self.bridge.save_session(ext_id, lambda data, e=ext_id, j=job: self._unload_for_reload(e, j, data))
        if self._reloads:
            self._reload_timer.start()

    def _applied_scripts(self, ext_id: str, path: str) -> dict:
        """The registered content scripts as they are now - and where they are in the manifest written with them (after
        the extension's own): a load error names an entry there."""
        try:
            original = _read_json_file(Path(path) / SHIM_ORIGINAL)
        except (OSError, ValueError):
            original = {}
        declared = len(wire_content_scripts(original.get("content_scripts"))) if isinstance(original, dict) else 0
        return {"applied": json.loads(json.dumps(self.bridge._registered(ext_id))), "declared": declared,
                "applied_cs": self.bridge.registered_scripts(ext_id, ids=True)}

    def _refused_scripts(self, ext_id: str, job: dict, error: str, whole_set: bool = True) -> set[str]:
        """The registered content scripts (IDs) a load error names - remembered, so they aren't registered again. An
        error that names none of them marks the whole set (*whole_set*: when nothing else could have failed)."""
        pairs, declared = job.get("applied_cs") or [], job.get("declared", 0)
        m = re.search(r"content_scripts\[(\d+)\]", error)
        if m:
            n = int(m.group(1)) - declared
            ids = {pairs[n][0]} if 0 <= n < len(pairs) else set()
        else:
            named = {f.lstrip("/") for f in re.findall(r"'([^']+)'", error)}
            ids = {sid for sid, e in pairs if named & {f.lstrip("/") for f in (e.get("js") or []) + (e.get("css") or [])}}
        bad = self._bad_scripts.setdefault(ext_id, {})
        for sid, entry in pairs:
            if sid in ids:
                bad[json.dumps(entry, sort_keys=True)] = error
        if not ids and pairs and whole_set:
            bad["set:" + json.dumps([e for _sid, e in pairs], sort_keys=True)] = error
        return ids

    def _unload_for_reload(self, ext_id: str, job: dict, session: dict | None) -> None:
        info = self._info(ext_id)
        if self._updates.get(ext_id) is not job:
            return
        if info is None:
            self._updates.pop(ext_id, None)
            self._next_after(job, drop=True)
            return
        job["session"], job["unloading"] = session, True
        job["offscreen"] = self.bridge.offscreen_url(ext_id)  # opened again afterwards: Chrome wouldn't close it
        self.bridge.extension_disabled(ext_id)
        self.manager.unloadExtension(info)  # continues in _on_unload_finished

    def _on_unload_finished(self, info) -> None:
        restore = self._restore_after_reject.pop(info.id(), None)
        if restore is not None:  # a refused copy that took an installed extension's ID (Qt unloads by ID): load that again
            self._rejected.discard(restore[0])
            shutil.rmtree(restore[0], ignore_errors=True)
            if not sip.isdeleted(self.manager):
                self.manager.loadExtension(restore[1])  # switched back on (if it was) in _on_load_finished
            return
        job = self._updates.get(info.id())
        if job is not None and "reload" in job and job.get("target") == info.path():
            QTimer.singleShot(0, lambda: self._load_update(job, rollback=False))
        elif job is not None and "target" in job and "unloaded" not in job:
            job["unloaded"] = True
            QTimer.singleShot(0, lambda: self._swap_in_update(job))

    def _swap_in_update(self, job: dict) -> None:
        target, staged = Path(job["target"]), Path(job["dir"])
        backup = self.staging / f"previous-{uuid.uuid4().hex[:10]}"
        note = backup.with_name(backup.name + ".restore")
        try:
            note.write_text(str(target), encoding="utf-8")  # first: a crash between the two moves stays recoverable
            _move(target, backup)
        except OSError as exc:
            note.unlink(missing_ok=True)
            job["error"] = str(exc)
            self._load_update(job, rollback=True)  # nothing was moved: just load the old version again
            return
        job["backup"] = str(backup)
        try:
            _move(staged, target)
        except OSError as exc:
            job["error"] = str(exc)
            self._restore_backup(job)
            self._load_update(job, rollback=True)
            return
        self._load_update(job, rollback=False)

    def _load_update(self, job: dict, rollback: bool) -> None:
        if sip.isdeleted(self.manager):
            return
        job["rollback"] = rollback
        self._loading[os.path.realpath(job["target"])] = job
        self.manager.loadExtension(job["target"])

    def _restore_backup(self, job: dict) -> bool:
        """Put the previous version back. On failure the backup (and its note) stay for the next start."""
        backup, target = job.get("backup"), Path(job["target"])
        if not backup:
            return True
        try:
            if target.exists():  # the new version: move it aside first, never delete it in place
                trash = self.staging / f"failed-{uuid.uuid4().hex[:10]}"
                _move(target, trash)
                shutil.rmtree(trash, ignore_errors=True)
            _move(Path(backup), target)
        except OSError as exc:
            log(f"Couldn't restore the previous version of an extension: {exc}")
            job["restore_failed"] = True
            return False
        Path(backup + ".restore").unlink(missing_ok=True)
        (target / self.RESTORE_MARKER).unlink(missing_ok=True)
        job["backup"] = None
        return True

    def _finish_update(self, job: dict, info) -> None:
        ext_id = job["ext_id"]
        loaded = info.isLoaded() and not info.error() and info.id() == ext_id
        if "reload" in job:  # same files, new registered content scripts
            if not loaded and not job["rollback"]:  # back to the manifest it loaded with before
                error = info.error() or "unknown error"
                log(f"“{job['name']}” didn't load with its registered content scripts: {error}")
                refused = self._refused_scripts(ext_id, job, error)
                state = self.registry.setdefault(ext_id, {})
                # only the scripts Qt refused go (what else was registered or unregistered meanwhile stands - written by the
                # next reload); if it named none, the registered scripts are put back as they were
                state["scripts"] = ([s for s in self.bridge._registered(ext_id) if s.get("id") not in refused] if refused
                                    else job.get("scripts_before") or [])
                self.save()
                try:
                    _replace_file(Path(job["target"]) / "manifest.json", job["reload"])
                except OSError:
                    pass
                self.message.emit(f"“{job['name']}” registered content scripts that couldn't be used ({error}); they were dropped.", "error")
                self._load_update(job, rollback=True)
                return
            self._updates.pop(ext_id, None)
            if job.get("session") and self.registry.get(ext_id, {}).get("enabled", True):
                self.bridge._session_restore[ext_id] = job["session"]  # put back when it's switched on again
            if job.get("offscreen") and self.registry.get(ext_id, {}).get("enabled", True):
                self.bridge._offscreen_restore[ext_id] = job["offscreen"]
            for delay in (0, 500):
                QTimer.singleShot(delay, lambda: self._apply_enabled(ext_id))
            QTimer.singleShot(800, lambda: self._reopen_pages(ext_id, job.get("pages") or []))
            self._next_after(job)
            if job.get("remove_after"):
                QTimer.singleShot(0, lambda: self.uninstall(ext_id))
            else:  # what was registered meanwhile still has to be written - measured against what the manifest has now
                QTimer.singleShot(0, lambda: self.sync_registered(
                    ext_id, (job.get("scripts_before") or []) if job["rollback"] else (job.get("applied") or [])))
            self.changed.emit()
            return
        if not loaded and not job["rollback"]:
            if info.isLoaded():  # something with a different ID loaded from this folder: take it out again
                self.manager.unloadExtension(info)
            job["error"] = info.error() or "the new version couldn't be loaded"
            self._restore_backup(job)
            self._load_update(job, rollback=True)
            return
        self._updates.pop(ext_id, None)
        shutil.rmtree(job["dir"], ignore_errors=True)
        if job.get("backup") and not job.get("restore_failed"):  # the note first: a half-deleted backup must never go back
            Path(job["backup"] + ".restore").unlink(missing_ok=True)
            shutil.rmtree(job["backup"], ignore_errors=True)
        silent = job.get("silent")
        refused = self._refused_scripts(ext_id, job, job.get("error") or "", whole_set=False) if job["rollback"] and job.get("reshim") else set()
        if refused:  # registered scripts it can't load with (from before they were checked): again, without them
            state = self.registry.setdefault(ext_id, {})
            state["scripts"] = [s for s in self.bridge._registered(ext_id) if s.get("id") not in refused]
            self.save()
            log(f"“{job['name']}” didn't load with its registered content scripts: {job.get('error')}")
            self.message.emit(f"“{job['name']}” registered content scripts that couldn't be used ({job.get('error')}); they were dropped.", "error")
        elif job["rollback"] and silent:
            self._reshim_failed.add(ext_id)
            log(f"Couldn't add {APP_NAME}'s API support to “{job['name']}”: {job.get('error') or 'unknown error'}")
        elif not job["rollback"]:
            if job.get("reshim"):  # registered while it was being re-wired: written next
                QTimer.singleShot(0, lambda: self.sync_registered(ext_id, job.get("applied") or []))
            if silent and self._needs_shim(info):  # never loop on an extension the polyfill can't be added to
                self._reshim_failed.add(ext_id)
            if not silent:
                state = self.registry.setdefault(ext_id, {})
                state.update(source=job.get("source", state.get("source", "file")),
                             source_path=job.get("source_path") or state.get("source_path"), installed=time.time(),
                             unpacked=bool(job.get("unpacked")))
                self.save()
            self.bridge.installed(ext_id, {"reason": "chrome_update"} if silent else
                                  {"reason": "update", "previousVersion": job.get("previous", "")})
        for delay in (0, 500):  # Qt loads extensions disabled; switch it back on if the user had it on
            QTimer.singleShot(delay, lambda: self._apply_enabled(ext_id))
        if job["rollback"] and not silent:
            kept = (f"The previous version couldn't be put back yet - it will be when {APP_NAME} restarts."
                    if job.get("restore_failed") else "The previous version was kept.")
            self.message.emit(f"Couldn't update “{job['name']}”: {job.get('error') or 'unknown error'}. {kept}", "error")
        elif not silent:
            self.message.emit(f"“{info.name() or job['name']}” was updated.", "success")
        self._next_after(job)
        if job.get("remove_after"):
            QTimer.singleShot(0, lambda: self.uninstall(ext_id))
        self.changed.emit()

    def _recover_interrupted_updates(self) -> list[str]:
        """Put back any previous version an interrupted update had moved aside."""
        restored = []
        found = [(m.parent, m) for m in self.staging.glob(f"previous-*/{self.RESTORE_MARKER}")]
        found += [(n.with_suffix(""), n) for n in self.staging.glob("previous-*.restore")]
        for backup, note in found:
            try:
                target = Path(note.read_text(encoding="utf-8").strip())
                if target.parent.name != "Extensions" or not backup.is_dir():
                    note.unlink(missing_ok=True)
                    continue
                if target.exists():  # a half-installed new version: move it out of the way (staging is cleared next)
                    _move(target, self.staging / f"failed-{uuid.uuid4().hex[:10]}")
                _move(backup, target)
                (target / self.RESTORE_MARKER).unlink(missing_ok=True)
                note.unlink(missing_ok=True)
                restored.append(str(target))
            except OSError as exc:
                log(f"Couldn't restore an extension's previous version: {exc}")
        return restored

    def _clean_staging(self) -> None:
        """Empty the staging folder - except previous versions that still have to go back (a failed restore)."""
        keep = {n.with_suffix("").name for n in self.staging.glob("previous-*.restore")}
        keep |= {m.parent.name for m in self.staging.glob(f"previous-*/{self.RESTORE_MARKER}")}
        if not self.staging.is_dir():
            return
        for child in self.staging.iterdir():
            if child.name in keep or (child.suffix == ".restore" and child.with_suffix("").name in keep):
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    def _install_staged(self, job: dict) -> None:
        self._ensure_bridge()
        if job.get("id"):
            self._installing.add(job["id"])
        self._jobs[Path(job["dir"]).name] = job
        self.manager.installExtension(job["dir"])

    def _on_install_finished(self, info) -> None:
        # Success reports the installed copy ("<staging name>_XXXXXX"), failure reports the source folder.
        name = Path(info.path()).name if info.path() else ""
        job_key = next((k for k in self._jobs if name == k or name.startswith(k + "_")), None)
        if job_key is None:
            # Qt also reports *uninstall* failures through this signal.
            if info.error() and info.isInstalled():
                self.message.emit(f"Extension error: {info.error()}", "error")
                self.changed.emit()
            return
        job = self._jobs.pop(job_key)
        self._installing.discard(job.get("id") or "")
        self._installing.discard(info.id())
        shutil.rmtree(job["dir"], ignore_errors=True)
        if not info.isLoaded() or info.error():
            self.message.emit(f"Couldn't install “{job.get('name')}”: {info.error() or 'unknown error'}", "error")
            return
        # Only the ID checked before installing may be kept - and only as a new extension: one that is already installed
        # (with its data) is replaced by an update, which asks first when it has to (_on_staged).
        taken = [i.path() for i in self._infos() if i.id() == info.id() and i.path() != info.path()]
        if not job.get("id") or info.id() != job["id"] or taken:
            log(f"Refused an extension that loaded as {info.id()} (checked: {job.get('id') or 'none'}, already installed: {bool(taken)}).")
            self._rejected.add(info.path())
            if taken:  # Qt loaded it in place of the installed one: take it out, then load the installed one again
                self.bridge.extension_disabled(info.id())
                self._restore_after_reject[info.id()] = (info.path(), taken[0])
                self.manager.unloadExtension(info)  # continues in _on_unload_finished
                QTimer.singleShot(2000, lambda path=info.path(): shutil.rmtree(path, ignore_errors=True))
            else:
                self.manager.uninstallExtension(info)
            self.message.emit(f"Couldn't install “{job.get('name')}”: " + (
                "an extension with the same ID is already installed." if taken and info.id() == job.get("id")
                else "the package isn't what it claims to be."), "error")
            self.changed.emit()
            return
        # A new extension starts from nothing: whatever an earlier one with this ID left (granted permissions, file
        # access, rules...) isn't its to inherit.
        state = self.registry[info.id()] = {"pinned": True}
        state.update(enabled=True, source=job.get("source", "file"), source_path=job.get("source_path"),
                     installed=time.time(), unpacked=bool(job.get("unpacked")))
        self.save()
        ext_id = info.id()
        self.bridge.installed(ext_id, {"reason": "install"})
        QTimer.singleShot(0, lambda: self._apply_enabled(ext_id))
        newer = (f" It is made for Chrome {job['needs_chrome']} or newer ({APP_NAME}'s engine is Chromium "
                 f"{self.chromium.split('.')[0]}), so parts of it may not work.") if job.get("needs_chrome") else ""
        self.message.emit(f"“{info.name()}” was added to {APP_NAME}.{newer}", "success")
        self.changed.emit()


APP_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld.value  # Foxglove's own isolated world in web pages
FIRST_EXTENSION_WORLD = 16           # chrome.scripting: a world per extension from here on (never Foxglove's)
HOST_SCHEMES = ("http", "https", "ws", "wss", "ftp")  # what host permissions reach (file: only if the user allows it)
MAIN_WINDOW_ID = 1
_TAB_IDS = itertools.count(1)        # chrome.tabs ids: stable for a tab's lifetime, never reused
_WINDOW_IDS = itertools.count(MAIN_WINDOW_ID + 1)
BADGE_COLOR = [217, 48, 37, 255]


class ApiError(Exception):
    """Reaches the calling extension as a rejected promise / chrome.runtime.lastError."""


def _color(value) -> list[int] | None:
    """chrome.action colours: [r, g, b, a] or any CSS colour string -> [r, g, b, a]."""
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return [int(clamp(int(v), 0, 255)) for v in [*value[:4], 255][:4]]
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        m = re.fullmatch(r"\s*rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+%?))?\s*\)\s*", value)
        if m:
            alpha = m.group(4) or "1"
            a = float(alpha[:-1]) / 100 if alpha.endswith("%") else float(alpha)
            return [int(clamp(float(m.group(i)), 0, 255)) for i in (1, 2, 3)] + [int(clamp(a * 255, 0, 255))]
        color = QColor(value.strip())
        if color.isValid():
            return [color.red(), color.green(), color.blue(), color.alpha()]
    return None


def _origin(url: QUrl) -> str:
    return f"{url.scheme()}://{url.authority()}"


def _page_here(url: QUrl) -> str:
    """What `location.protocol + "//" + location.host` reads in a page at *url* (as Chromium writes URLs)."""
    scheme, host, port = url.scheme().lower(), url.host(QUrl.ComponentFormattingOption.FullyEncoded).lower(), url.port()
    host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host}" + (f":{port}" if port not in (-1, DEFAULT_PORTS.get(scheme)) else "")


def _glob(pattern: str) -> re.Pattern:
    return re.compile(".*".join(map(re.escape, pattern.lstrip("/").split("*"))))


def _strings(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


class ExtensionBridge(QWebEngineUrlSchemeHandler):
    """The browser half of the chrome.* polyfill.

    Extension pages and service workers fetch() foxglove-ext://bridge/call (Foxglove knows them by their origin;
    content scripts run in the page's origin and prove themselves with a token from their shim). Events go the
    other way through a hidden page of the extension (foxglove-bridge.html), which also wakes a stopped worker.
    foxglove-ext://<id>/<file> serves web_accessible_resources, which Qt refuses to web pages.
    """

    action_changed = pyqtSignal(str)
    CS_APIS = {"storage.changed", "storage.pending", "tabs.reply"}  # all a content script may call
    NEEDS = {"contextMenus": ("contextMenus", "menus"), "notifications": ("notifications",), "alarms": ("alarms",),
             "scripting": ("scripting",), "offscreen": ("offscreen",), "sidePanel": ("sidePanel",), "downloads": ("downloads",),
             "dnr": ("declarativeNetRequest", "declarativeNetRequestWithHostAccess"), "webNavigation": ("webNavigation",),
             "fontSettings": ("fontSettings",), "cookies": ("cookies",)}  # namespace -> any of these permissions
    EVENT_NEEDS = {"webNavigation": "webNavigation", "cookies": "cookies"}  # events that report what the user does
    MENU_KEYS = ("type", "title", "checked", "contexts", "visible", "parentId", "documentUrlPatterns", "targetUrlPatterns", "enabled")
    PAGE_IDLE_MS = 60_000
    MAX_PARTS, MAX_PENDING = 700, 8  # ~8 MB calls; calls in parts at once, per extension
    REFUSED_PAUSE = 20.0  # s
    NO_RECEIVER = "Could not establish connection. Receiving end does not exist."
    NO_HOST = "Cannot access contents of the page. Extension manifest must request permission to access the respective host."
    SCRIPT_GONE = "Frame with ID 0 was removed."
    SCRIPT_CHECK_MS = 1000  # how often a script sent to a page that hasn't answered yet is looked after

    def __init__(self, controller: "ExtensionsController"):
        super().__init__(controller)
        self.c = controller
        self.action: dict[str, dict[int, dict]] = {}    # ext id -> {tab id (0: every tab) -> {key: value}}
        self._pages: dict[str, dict] = {}               # ext id -> its bridge page {"page", "ready", "queue", "timer"}
        self._pending: dict[str, list] = {}             # events waiting for the extension to be switched on
        self._replies: dict[str, tuple] = {}            # tabs.sendMessage call id -> (ext id, reply, timer)
        self._alarms: dict[str, dict[str, QTimer]] = {}
        self._offscreen: dict[str, QWebEnginePage] = {}
        self._session_rules: dict[str, list] = {}
        self._notes: dict[str, dict[str, dict]] = {}
        self._grants: dict[str, tuple[int, str]] = {}   # activeTab: ext id -> (tab id, origin)
        self._worlds: dict[str, int] = {}               # ext id -> its chrome.scripting world
        self._asking: set[str] = set()                  # permissions.request: a question is open
        self._refused: dict[str, float] = {}            # ... the user said no: not again before this time
        self._page_listeners: dict[str, set[str]] = {}  # events extension pages (not the worker) listen to
        self._started: set[str] = set()
        self._session_restore: dict[str, dict] = {}     # chrome.storage.session to put back once a reload is through
        self._offscreen_restore: dict[str, str] = {}    # the offscreen document to open again once a reload is through
        self._early_storage: dict[str, tuple] = {}      # storage writes announced before they were made: "id:eid" -> (what, timer)
        self._storage_done: dict[str, None] = {}        # "id:eid" of the storage changes delivered (oldest first)
        self._script_patterns: dict[str, tuple] = {}    # ext id -> (its manifest's content_scripts, their PatternSet)
        self._downloads = itertools.count(1)
        self._parts: dict[tuple, dict] = {}             # calls arriving in parts: (ext id, id) -> {"sender", "count", "parts", "time"}
        self._cookies: dict[tuple, QNetworkCookie] = {}  # the profile's cookies (chrome.cookies): the CookieIndex's, once watched
        self._removed: dict[tuple, QNetworkCookie] = {}  # just removed: an overwrite if it comes right back
        self._cookie_waiters: dict[tuple, list] = {}     # cookies.set calls waiting for the store
        self._cookies_ready = self._cookies_watched = False

    def watch_cookies(self) -> None:
        """Keep a copy of the profile's cookies (chrome.cookies) - from the first extension allowed to use them on."""
        if self._cookies_watched or self.c.manager is None:
            return
        self._cookies_watched = True
        index = CookieIndex.of(self.c.profile)  # shared with the site settings
        self._cookies = index.cookies
        index.added.connect(self._cookie_added)
        index.removed.connect(self._cookie_removed)
        if index.ready:
            self._cookies_ready = True
        else:  # loading reports every cookie as added
            index.loaded.connect(lambda: setattr(self, "_cookies_ready", True))

    # ── requests ─────────────────────────────────────────────────────────────────────────
    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:
        try:
            if job.requestUrl().host() == "bridge":
                self._call(job)
            else:
                self._serve(job)
        except Exception as exc:  # an exception escaping here would take the browser down
            log(f"Extension bridge error: {exc!r}")
            try:
                self._deny(job)
            except RuntimeError:
                pass

    @staticmethod
    def _deny(job: QWebEngineUrlRequestJob) -> None:
        """Refuse a request as a network error. (job.fail() on a CORS-enabled scheme leaves a fetch()'s response
        unfinished for ever - and answers web pages differently from a browser without Foxglove's scheme.)"""
        job.redirect(QUrl("about:blank"))  # fetch() rejects, <img>/<script> fail: what an unknown scheme gets

    def _enabled(self, ext_id: str) -> bool:
        info = self.c._info(ext_id)
        return info is not None and info.isEnabled()

    def _token_owner(self, token) -> str:
        if not isinstance(token, str) or len(token) < 16:
            return ""
        return next((i.id() for i in self.c._infos() if i.isEnabled()
                     and hmac.compare_digest(str(self.c.shim_config(i.id()).get("token", "")), token)), "")

    @staticmethod
    def _body(job: QWebEngineUrlRequestJob) -> bytes:
        """The request body - read with a bound: past 16 KiB Qt's device starts over instead of ending (the shim
        sends bigger calls in parts)."""
        body, data = job.requestBody(), bytearray()
        if body is None:
            return b""
        if not body.isOpen():
            body.open(QIODevice.OpenModeFlag.ReadOnly)
        while len(data) < 16384:
            chunk = bytes(body.read(16384 - len(data)))
            data += chunk
            if not chunk or body.atEnd():
                break
        return bytes(data)

    def _part(self, job: QWebEngineUrlRequestJob, data: bytes) -> bytes | None:
        """One part of a call too big for a single request: None until the last one, then the whole body. Each part
        starts with a line naming the caller (a content script's token; empty for the extension's own pages), so
        nothing is kept for web pages - and each extension has a few slots of its own."""
        m = re.fullmatch(r"part=([a-z0-9]{8,40})\.(\d+)\.(\d+)", job.requestUrl().query())
        initiator = job.initiator()
        if m is None or not 1 < int(m.group(3)) <= self.MAX_PARTS or int(m.group(2)) >= int(m.group(3)):
            raise ValueError("bad part")
        auth, newline, data = data.partition(b"\n")
        owner = initiator.host() if initiator.scheme() == "chrome-extension" and self._enabled(initiator.host()) else ""
        owner = owner or (self._token_owner(auth.decode("ascii", "replace")) if newline else "")
        if not owner:
            raise ValueError("unknown caller")
        key, index, count = (owner, m.group(1)), int(m.group(2)), int(m.group(3))
        now = time.monotonic()
        for stale in [k for k, v in self._parts.items() if now - v["time"] > 60]:
            del self._parts[stale]
        if key not in self._parts and sum(k[0] == owner for k in self._parts) >= self.MAX_PENDING:
            raise ValueError("too many calls in parts")
        entry = self._parts.setdefault(key, {"sender": initiator.toString(), "count": count, "parts": {}, "time": now})
        if entry["sender"] != initiator.toString() or entry["count"] != count:
            self._parts.pop(key, None)
            raise ValueError("bad part")
        entry["parts"][index] = data
        entry["time"] = now
        if len(entry["parts"]) < count:
            return None
        del self._parts[key]
        return b"".join(entry["parts"][i] for i in range(count))

    def _call(self, job: QWebEngineUrlRequestJob) -> None:
        data = self._body(job)
        try:
            if job.requestUrl().hasQuery():
                data = self._part(job, data)
                if data is None:
                    self._reply(job)
                    return
            req = json.loads(data or b"{}")
            if not isinstance(req, dict):
                raise ValueError("not an object")
        except ValueError:
            self._deny(job)
            return
        initiator, api = job.initiator(), str(req.get("api") or "")
        ext_id = initiator.host() if initiator.scheme() == "chrome-extension" else ""
        ctx = {"from": str(req.get("from") or ""), "cs": False, "tab": None}
        if not self._enabled(ext_id):  # a content script - or a web page, which gets nothing
            ext_id = self._token_owner(req.get("token"))
            if not ext_id or api not in self.CS_APIS:
                self._deny(job)
                return
            ctx.update(cs=True, tab=req.get("tab"))
        handler = getattr(self, "api_" + api.replace(".", "_"), None) if re.fullmatch(r"[A-Za-z]+\.[A-Za-z]+", api) else None
        if handler is None:
            self._reply(job, error=f"{APP_NAME} doesn't support chrome.{api}.")
            return
        needs = self.NEEDS.get(api.split(".")[0])
        if needs and not any(self.has_permission(ext_id, p) for p in needs):  # the shim hides these; don't trust it
            self._reply(job, error=f"The extension needs the “{needs[0]}” permission for chrome.{api}.")
            return
        if needs == ("cookies",):  # (allowed since it was switched on: permissions.request)
            self.watch_cookies()
        args = req.get("args")
        try:
            result = handler(ext_id, args if isinstance(args, dict) else {}, ctx)
            if callable(result):  # answered later
                result(self._later(job))
                return
        except ApiError as exc:
            self._reply(job, error=str(exc))
            return
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            self._reply(job, error=f"Invalid arguments for chrome.{api}: {exc}")
            return
        self._reply(job, result)

    @staticmethod
    def _reply(job: QWebEngineUrlRequestJob, value=None, error: str | None = None) -> None:
        payload = {"ok": False, "error": str(error)} if error is not None else {"ok": True} if value is None else {"ok": True, "value": value}
        buffer = QBuffer(job)
        buffer.setData(json.dumps(payload, default=str).encode("utf-8"))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        job.setAdditionalResponseHeaders({b"Access-Control-Allow-Origin": [b"*"], b"Cache-Control": [b"no-store"]})
        job.reply(b"application/json", buffer)

    def _later(self, job: QWebEngineUrlRequestJob):
        """A reply(value=None, error=None) that is safe to call after the caller went away."""
        alive = [True]
        job.destroyed.connect(lambda *_: alive.__setitem__(0, False))

        def reply(value=None, error: str | None = None) -> None:
            if alive[0] and not sip.isdeleted(job):
                alive[0] = False
                self._reply(job, value, error)
        return reply

    @staticmethod
    def _extension_file(root: Path, url: QUrl) -> tuple[Path, str] | None:
        """The extension file a foxglove-ext:// URL names, and its path inside the extension - decoded one segment at
        a time (Chromium already removed "..": an encoded "/" or ".." must not bring it back)."""
        parts = [unquote(p, errors="strict") for p in url.path(QUrl.ComponentFormattingOption.FullyEncoded).split("/") if p]
        if not parts or any(p in (".", "..") or re.search(r"[/\\:\0]", p) for p in parts):
            return None
        target = (root / "/".join(parts)).resolve()
        if root not in target.parents or not target.is_file():
            return None
        rel = target.relative_to(root).as_posix()
        return None if Path(rel).name.lower().startswith("foxglove-") else (target, rel)  # the polyfill's files: never

    def _serve(self, job: QWebEngineUrlRequestJob) -> None:
        url = job.requestUrl()
        ext_id = url.host()
        info = self.c._info(ext_id)
        found = None
        if info is not None and info.isEnabled():
            try:
                found = self._extension_file(Path(info.path()).resolve(), url)
            except (UnicodeError, ValueError, OSError):
                found = None
        if found is None or not self._war_allowed(ext_id, found[1], job.initiator()):
            self._deny(job)
            return
        target = found[0]
        mime = "text/javascript" if target.suffix in (".js", ".mjs") else mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        buffer = QBuffer(job)
        buffer.setData(target.read_bytes())
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        headers = {b"Access-Control-Allow-Origin": [b"*"]}
        if mime in ("text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml"):
            headers[b"Content-Security-Policy"] = [self._page_csp(ext_id).encode("utf-8", "replace")]
        job.setAdditionalResponseHeaders(headers)
        job.reply(mime.encode(), buffer)

    def _page_csp(self, ext_id: str) -> str:
        """The extension's Content-Security-Policy for its pages (Chrome's default if it has none): its pages shown
        through foxglove-ext:// get it as they do as chrome-extension:// pages - no inline scripts from markup in them."""
        m = self._manifest(ext_id)
        csp = m.get("content_security_policy")
        csp = csp.get("extension_pages") if isinstance(csp, dict) else csp
        if isinstance(csp, str) and csp.strip() and "\n" not in csp and "\r" not in csp:
            return csp.strip()
        return ("script-src 'self' 'wasm-unsafe-eval'; object-src 'self';" if m.get("manifest_version") == 3
                else "script-src 'self' blob: filesystem:; object-src 'self';")

    def _war_allowed(self, ext_id: str, rel: str, initiator: QUrl) -> bool:
        opaque = not initiator.isEmpty() and not (initiator.isValid() and initiator.scheme())  # sandboxed frames, data: ...
        origin = "" if opaque or initiator.isEmpty() else _origin(initiator)
        if origin in (f"chrome-extension://{ext_id}", f"{EXT_SCHEME}://{ext_id}"):  # the extension itself: all its files
            return True
        for entry in self._manifest(ext_id).get("web_accessible_resources") or []:
            if not isinstance(entry, dict) or not any(_glob(r).fullmatch(rel) for r in _strings(entry.get("resources"))):
                continue
            matches = _strings(entry.get("matches"))
            if opaque:
                if "<all_urls>" in matches or "*://*/*" in matches:
                    return True
                continue
            if not origin:  # the browser itself (a typed address)
                return True
            if any(match_pattern(m, origin + "/") for m in matches):
                return True
            ids = _strings(entry.get("extension_ids"))
            if initiator.scheme() == "chrome-extension" and ("*" in ids or initiator.host() in ids):
                return True
        return False

    # ── events: Foxglove -> extension ────────────────────────────────────────────────────
    def emit(self, ext_id: str, name: str, args: list, eid: str | None = None, worker_only: bool = False) -> None:
        """Deliver an event to the extension's worker (waking it) and its open pages (not with *worker_only*)."""
        self._run_in_page(ext_id, f"window.__foxgloveEmit && __foxgloveEmit({json.dumps(name)}, {json.dumps(args, default=str)}, "
                                  f"{json.dumps(eid)}, {json.dumps(worker_only)})")

    def _run_in_page(self, ext_id: str, script: str) -> bool:
        """Run *script* in the extension's bridge page (opened if needed; scripts run in the order they came)."""
        if not self._enabled(ext_id) or not self.c.shim_config(ext_id):
            return False
        slot = self._pages.get(ext_id)
        if slot is None:
            page = WebPage(self.c.profile, self)  # WebPage: keeps the extension's console messages out of the terminal
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(self.PAGE_IDLE_MS)  # costs a renderer process: close it when it's been quiet a while
            timer.timeout.connect(lambda e=ext_id: self.close_page(e))
            slot = self._pages[ext_id] = {"page": page, "ready": False, "queue": [], "timer": timer}
            page.loadFinished.connect(lambda ok, e=ext_id, p=page: self._page_loaded(e, p, ok))
            page.load(QUrl(f"chrome-extension://{ext_id}/{SHIM_BRIDGE}"))
        slot["timer"].start()
        if slot["ready"]:
            slot["page"].runJavaScript(script, 0)
        else:
            slot["queue"].append(script)
        return True

    def save_session(self, ext_id: str, done) -> None:
        """Read the extension's chrome.storage.session (through its bridge page), then done(data or None): Foxglove
        reloads an extension for its registered content scripts, which Chrome never does - so its session data, which
        lasts until the browser quits there, must come back afterwards (extension_enabled)."""
        if not self.has_permission(ext_id, "storage"):
            done(None)
            return
        self._ask_page(ext_id, "window.__foxgloveSession ? __foxgloveSession.get() : null",
                       lambda data: done(data if isinstance(data, dict) and data else None))

    def _ask_page(self, ext_id: str, expression: str, done, timeout: float = 4.0) -> None:
        """Work out *expression* (JavaScript; a promise is waited for) in the extension's bridge page, then done(its
        value, through JSON) - or done(None) if that failed or took too long."""
        key = "__fgAsk" + secrets.token_hex(6)
        script = (f"window.{key} = null; Promise.resolve().then(() => {expression})"
                  f".then((d) => {{ window.{key} = JSON.stringify(d ?? null); }}, () => {{ window.{key} = 'null'; }}); 0")
        if not self._run_in_page(ext_id, script):
            done(None)
            return
        deadline, finished = time.monotonic() + timeout, [False]

        def finish(data) -> None:
            if not finished[0]:
                finished[0] = True
                done(data)

        def poll() -> None:
            slot = self._pages.get(ext_id)
            if finished[0]:
                return
            if slot is None or time.monotonic() > deadline:
                finish(None)
            elif not slot["ready"]:
                QTimer.singleShot(50, poll)
            else:
                slot["page"].runJavaScript(f"(() => {{ const v = window.{key}; if (typeof v === 'string') delete window.{key}; "
                                           "return v ?? null; })()", 0, got)

        def got(value) -> None:
            if isinstance(value, str):
                try:
                    finish(json.loads(value))
                except ValueError:
                    finish(None)
            elif time.monotonic() > deadline:
                finish(None)
            else:
                QTimer.singleShot(50, poll)
        QTimer.singleShot(0, poll)

    def _page_loaded(self, ext_id: str, page: QWebEnginePage, ok: bool) -> None:
        slot = self._pages.get(ext_id)
        if slot is None or slot["page"] is not page or slot["ready"]:
            return
        if not ok:
            self.close_page(ext_id)
            return
        slot["ready"] = True
        for script in slot["queue"]:
            page.runJavaScript(script, 0)
        slot["queue"].clear()

    def close_page(self, ext_id: str) -> None:
        slot = self._pages.pop(ext_id, None)
        if slot is not None:
            slot["timer"].stop()
            slot["timer"].deleteLater()
            slot["page"].deleteLater()

    def listens(self, ext_id: str, name: str) -> bool:
        state = self.c.registry.get(ext_id)
        return (isinstance(state, dict) and name in (state.get("listeners") or [])) or name in self._page_listeners.get(ext_id, ())

    def listeners(self, name: str) -> list[str]:
        return [i.id() for i in self.c._infos() if i.isEnabled() and self.listens(i.id(), name)]

    def script_tabs(self, ext_id: str) -> list:
        """The tabs the extension's content scripts may be in: one of their match patterns covers a frame of the tab."""
        info = self.c._info(ext_id)
        entries = self.c._manifest(info.path()).get("content_scripts") if info is not None else None
        cached = self._script_patterns.get(ext_id)
        if cached is None or cached[0] is not entries:  # (the same list until manifest.json changes)
            cached = self._script_patterns[ext_id] = (entries, PatternSet(
                m for e in entries if isinstance(e, dict) for m in _strings(e.get("matches"))) if isinstance(entries, list) else PatternSet(()))
        patterns = cached[1]
        if not patterns:
            return []
        out = []
        for tab in self.tabs():
            if tab.pending is not None or sip.isdeleted(tab.page):
                continue
            urls, frames = {tab.url().toString()}, [tab.page.mainFrame()]
            while frames and len(urls) < 200:
                frame = frames.pop()
                if frame.isValid():
                    urls.add(frame.url().toString())
                    frames.extend(frame.children())
            if any(patterns.matches(u) for u in urls if u):
                out.append(tab)
        return out

    def relay(self, page: QWebEnginePage, ext_id: str, payload: dict, done=None) -> bool:
        """Hand something to the extension's content scripts in *page* (a DOM event only they know the name of)."""
        name = self.c.shim_config(ext_id).get("relay")
        if not name or sip.isdeleted(page):
            return False
        page.runJavaScript(f"!document.dispatchEvent(new CustomEvent({json.dumps(name)}, "
                           f"{{detail: {json.dumps(json.dumps(payload, default=str))}, cancelable: true}}))",
                           APP_WORLD, done or (lambda _result: None))
        return True

    # ── extension life cycle (called by ExtensionsController) ───────────────────────────
    def installed(self, ext_id: str, details: dict) -> None:
        state = self.c.registry.setdefault(ext_id, {})
        if details.get("reason") != "chrome_update":  # a new Foxglove (polyfill) keeps them, as a Chrome update does
            # menus are re-created in onInstalled; alarms and registered scripts end; the new worker adds its listeners;
            # the static rulesets switched on or off go back to the manifest's (its dynamic rules stay, as in Chrome)
            for key in ("menus", "alarms", "scripts", "listeners", "rulesets"):
                state.pop(key, None)
            self._started.add(ext_id)  # no onStartup right after installing or updating
            # what the previous version set at run time (pop-up, badge, switched off...) and its session rules go with it
            for store in (self.action, self._session_rules, self.c._bad_scripts):
                store.pop(ext_id, None)
            self.c.net.invalidate()
            self.action_changed.emit(ext_id)
        # (after a Foxglove upgrade the worker's code is the same, so the listeners it had are still the ones it has: its
        # onStartup still fires - extension_enabled() goes by them)
        self.c.save()
        self._pending.setdefault(ext_id, []).append(("runtime.onInstalled", [details]))

    def extension_enabled(self, ext_id: str) -> None:
        if self.has_permission(ext_id, "cookies"):
            self.watch_cookies()
        for name in list((self.c.registry.get(ext_id) or {}).get("alarms") or {}):
            self._arm(ext_id, name)
        session = self._session_restore.pop(ext_id, None)
        if session:  # back from a reload of Foxglove's: before any event reaches the worker
            self._run_in_page(ext_id, f"window.__foxgloveSession && __foxgloveSession.restore({json.dumps(session, default=str)})")
        offscreen = self._offscreen_restore.pop(ext_id, None)
        if offscreen and ext_id not in self._offscreen:  # (Chrome never reloads it: its offscreen document stays open)
            self._open_offscreen(ext_id, QUrl(offscreen))
        events = self._pending.pop(ext_id, [])
        if ext_id not in self._started:
            self._started.add(ext_id)
            if self.listens(ext_id, "runtime.onStartup"):
                events.append(("runtime.onStartup", []))
        for name, args in events:
            self.emit(ext_id, name, args)

    def extension_disabled(self, ext_id: str) -> None:
        self._session_restore.pop(ext_id, None)  # switched off: its session ends, as in Chrome
        self._offscreen_restore.pop(ext_id, None)
        for timer in self._alarms.pop(ext_id, {}).values():
            timer.stop()
            timer.deleteLater()
        self.close_page(ext_id)
        page = self._offscreen.pop(ext_id, None)
        if page is not None:
            page.deleteLater()
        self._page_listeners.pop(ext_id, None)
        for call_id, (owner, reply, timer) in list(self._replies.items()):
            if owner == ext_id:
                self._replies.pop(call_id)
                timer.stop()
                reply(error=self.NO_RECEIVER)
        win = self.c.window
        if win is not None:
            win.close_extension_popups(ext_id)

    def extension_removed(self, ext_id: str) -> None:
        self.extension_disabled(ext_id)
        for store in (self.action, self._session_rules, self._notes, self._grants, self._pending):
            store.pop(ext_id, None)
        self.c.net.invalidate()
        self.action_changed.emit(ext_id)

    # ── helpers ─────────────────────────────────────────────────────────────────────────
    def _manifest(self, ext_id: str) -> dict:
        info = self.c._info(ext_id)
        return self.c._manifest(info.path()) if info is not None else {}

    def _granted(self, ext_id: str) -> dict:
        granted = (self.c.registry.get(ext_id) or {}).get("granted")
        return granted if isinstance(granted, dict) else {}

    def has_permission(self, ext_id: str, perm: str) -> bool:
        return perm in _strings(self._manifest(ext_id).get("permissions")) or perm in _strings(self._granted(ext_id).get("permissions"))

    def _hosts(self, ext_id: str, scripts: bool = False) -> list[str]:
        """The extension's host permissions (as granted). *scripts*: with its declared content scripts' match patterns,
        where its scripts run - which give it no host access to the APIs (cookies, tabs, scripting...), as in Chrome."""
        m = self._manifest(ext_id)
        hosts = _strings(m.get("host_permissions")) + [p for p in _strings(m.get("permissions")) if "://" in p or p == "<all_urls>"]
        declared = self.c.shim_config(ext_id).get("manifest") if scripts else None  # as declared - not registered ones
        declared = declared if isinstance(declared, dict) else m
        for entry in declared.get("content_scripts") or [] if scripts and isinstance(declared.get("content_scripts"), list) else []:
            hosts += _strings(entry.get("matches")) if isinstance(entry, dict) else []
        return hosts + _strings(self._granted(ext_id).get("origins"))

    def covers(self, ext_id: str, pattern: str) -> bool:
        """Whether the extension's host access includes everything a match pattern does (roughly, as Chrome's check)."""
        hosts = self._hosts(ext_id, scripts=True)
        if "<all_urls>" in hosts or pattern in hosts:
            return True
        m = re.fullmatch(r"(\*|[a-z][a-z0-9+.-]*)://(\*|\*\.)?([^/]*)(/.*)", pattern)
        if pattern == "<all_urls>" or m is None:
            return False
        host = m.group(3) if m.group(2) != "*" else "any-host.invalid"
        if host.endswith(":*"):  # any port of the host: only patterns for any port of it cover that
            host = host[:-2]
            hosts = [h for h in hosts if (hm := MATCH_PATTERN.fullmatch(h)) is None or hm.group(3) in (None, "*")]
        sample = f"{'https' if m.group(1) == '*' else m.group(1)}://{host}{m.group(4).replace('*', 'x')}"
        return any(match_pattern(h, sample) for h in hosts) and (m.group(1) != "*" or any(match_pattern(h, "http" + sample[5:]) for h in hosts))

    def can_access(self, ext_id: str, url: str, tab_id: int | None = None) -> bool:
        """Host access as in Chrome: web pages - never another extension's or Foxglove's own pages, and file: pages
        only once the user allowed it for this extension."""
        scheme = QUrl(url).scheme().lower()
        if scheme not in HOST_SCHEMES and not (scheme == "file" and self.c.file_access(ext_id)):
            return False
        if any(match_pattern(h, url) for h in self._hosts(ext_id)):
            return True
        grant = self._grants.get(ext_id)
        return grant is not None and grant[0] == tab_id and grant[1] == _origin(QUrl(url))

    def grant_active_tab(self, ext_id: str, tab) -> None:
        """The user invoked the extension on this tab (toolbar button, menu item): activeTab gives it access."""
        if tab is not None and self.has_permission(ext_id, "activeTab"):
            self._grants[ext_id] = (tab.tab_id, _origin(tab.url()))

    def _win(self):
        win = self.c.window
        if win is None:
            raise ApiError("No browser window is open.")
        return win

    def tabs(self) -> list:
        win = self.c.window
        return [] if win is None else [*win.tabs(), *(p for p in list(win.popups) if not sip.isdeleted(p))]

    def _tab(self, tab_id, current: bool = False):
        if tab_id is None and current:
            tab = self._win().current_tab()
            if tab is not None:
                return tab
        tab = (next((t for t in self.tabs() if t.tab_id == tab_id), None)
               if isinstance(tab_id, int) and not isinstance(tab_id, bool) else None)
        if tab is None:
            raise ApiError(f"No tab with id: {tab_id}.")
        return tab

    def tab_info(self, ext_id: str, tab) -> dict:
        win = self.c.window
        popup = isinstance(tab, PopupWindow)
        page, url = tab.page, tab.url().toString()
        active = popup or (win is not None and tab is win.current_tab())
        info = {"id": tab.tab_id, "index": 0 if popup or win is None else win.index_of(tab), "windowId": tab.window_id,
                "active": active, "highlighted": active, "selected": active, "pinned": False, "incognito": False,
                "audible": page.recentlyAudible(), "mutedInfo": {"muted": page.isAudioMuted()},
                "discarded": tab.pending is not None, "autoDiscardable": True, "frozen": False, "groupId": -1,
                "status": "unloaded" if tab.pending is not None else "loading" if tab.loading else "complete",
                "width": tab.width(), "height": tab.height()}
        opener = getattr(tab, "opener", None)
        if opener is not None:
            info["openerTabId"] = opener.tab_id
        if self.has_permission(ext_id, "tabs") or self.can_access(ext_id, url, tab.tab_id):
            info.update(url=url, title=tab.title())
            if not page.iconUrl().isEmpty():
                info["favIconUrl"] = page.iconUrl().toString()
        return info

    def _url(self, raw) -> QUrl | None:
        if raw in (None, ""):
            return None
        url = QUrl(str(raw))
        if not url.isValid() or url.scheme().lower() in ("javascript", "", "file"):  # file: needs the user's consent in Chrome
            raise ApiError(f"Invalid url: \"{raw}\".")
        if url.scheme() == "chrome" and url.host() == "newtab":
            return QUrl(NEWTAB)
        return url

    def _files(self, ext_id: str, files) -> str:
        info = self.c._info(ext_id)
        root = Path(info.path()).resolve() if info is not None else None
        parts = []
        for name in _strings(files):
            target = (root / name.lstrip("/")).resolve() if root is not None else None
            if target is None or root not in target.parents or not target.is_file():
                raise ApiError(f"Could not load file: '{name}'.")
            parts.append(target.read_text(encoding="utf-8", errors="replace"))
        if not parts:
            raise ApiError("Either 'files' or 'func' (or 'css') must be specified.")
        return "\n;\n".join(parts)

    def _script_tab(self, ext_id: str, target) -> "Tab":
        tab = self._tab((target or {}).get("tabId") if isinstance(target, dict) else None)
        if tab.pending is not None:
            raise ApiError("Cannot access contents of a tab that hasn't been loaded yet.")
        if not self.can_access(ext_id, tab.url().toString(), tab.tab_id):
            raise ApiError(self.NO_HOST)
        return tab

    def tab_event(self, name: str, tab, make_args) -> None:
        """A tabs.* / webNavigation.* event, for the extensions that listen to it (make_args -> None: skip it)."""
        needs = self.EVENT_NEEDS.get(name.split(".")[0])
        for ext_id in self.listeners(name):
            if needs and not self.has_permission(ext_id, needs):  # anyone may listen; only these may hear
                continue
            args = make_args(ext_id)
            if args is not None:
                self.emit(ext_id, name, args)

    def tab_navigated(self, tab, url: QUrl) -> None:
        """activeTab lasts while the tab stays on the origin it was given for (as in Chrome) - not until it comes back."""
        origin = _origin(url)
        for ext_id, grant in list(self._grants.items()):
            if grant[0] == tab.tab_id and grant[1] != origin:
                self._grants.pop(ext_id)

    def tab_closed(self, tab) -> None:
        for state in self.action.values():
            state.pop(tab.tab_id, None)
        for ext_id, grant in list(self._grants.items()):
            if grant[0] == tab.tab_id:
                self._grants.pop(ext_id)

    # ── listeners ───────────────────────────────────────────────────────────────────────
    def api_events_listen(self, ext_id: str, a: dict, ctx: dict):
        name = str(a.get("name") or "")
        if not a.get("worker"):
            self._page_listeners.setdefault(ext_id, set()).add(name)
            return None
        names = self.c.registry.setdefault(ext_id, {}).setdefault("listeners", [])
        if name not in names:  # remembered like Chrome's lazy listeners: the worker is woken only for these
            names.append(name)
            self.c.save()
        return None

    # ── storage.onChanged: for content scripts, and for a worker that has stopped ───────
    def api_storage_changed(self, ext_id: str, a: dict, ctx: dict):
        area, changes, src = str(a.get("area") or ""), a.get("changes") if isinstance(a.get("changes"), dict) else {}, a.get("src")
        eid = a.get("eid") if isinstance(a.get("eid"), str) and a["eid"] else None
        key = f"{ext_id}:{eid}"
        early = self._early_storage.pop(key, None) if eid else None
        if early is not None:
            early[1].stop()
            early[1].deleteLater()
        if eid and key in self._storage_done:  # its early notice was delivered already (the writer took its time)
            return None
        if eid:
            self._storage_delivered(key)
        if changes:
            self._deliver_storage(ext_id, area, changes, src, eid, ctx)
        return None

    def _deliver_storage(self, ext_id: str, area: str, changes: dict, src, eid: str | None, ctx: dict, fallback: bool = False) -> None:
        if area != "session" and (ctx["cs"] or self.c.shim_config(ext_id).get("cs")):
            payload = {"event": "storage.changed", "args": [area, changes, src], "eid": eid}
            for tab in self.script_tabs(ext_id):
                self.relay(tab.page, ext_id, payload)
        worker = (self.c.registry.get(ext_id) or {}).get("listeners") or []
        if fallback:  # the writer told no one: the worker (woken) and the open pages (the bridge page's channel) hear it here
            self.emit(ext_id, "storage.changed", [area, changes, src], eid=eid)
        elif not ctx["cs"] and ctx["from"] and ("storage.onChanged" in worker or f"storage.{area}.onChanged" in worker):
            self.emit(ext_id, "storage.changed", [area, changes, src], eid=eid, worker_only=True)  # a running worker drops a 2nd copy

    EARLY_STORAGE_MS = 1500

    def api_storage_pending(self, ext_id: str, a: dict, ctx: dict):
        """A write is on its way (sent before it's made): if its storage.changed doesn't follow - the page that wrote
        closed right away, like a pop-up that saves and closes - Foxglove tells the others itself (without old values)."""
        eid, area = a.get("eid"), str(a.get("area") or "")
        key = f"{ext_id}:{eid}"
        if not isinstance(eid, str) or not eid or key in self._storage_done or key in self._early_storage or len(self._early_storage) > 200:
            return None
        if isinstance(a.get("set"), dict):
            changes = {str(k): {"newValue": v} for k, v in a["set"].items()}
        elif isinstance(a.get("remove"), list):
            changes = {str(k): {} for k in a["remove"]}
        else:
            return None
        if not changes:
            return None
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(self.EARLY_STORAGE_MS)
        timer.timeout.connect(lambda: self._early_storage_due(key))
        self._early_storage[key] = ((ext_id, area, changes, a.get("src"), eid, dict(ctx)), timer)
        timer.start()
        return None

    def _early_storage_due(self, key: str) -> None:
        """The writer never said its write was through: pass on what of it is really stored (the notice is only what
        the caller says - and a write can fail)."""
        early = self._early_storage.pop(key, None)
        if early is None:
            return
        early[1].deleteLater()
        ext_id, area, changes, src, eid, ctx = early[0]
        if area not in ("local", "sync", "session") or not self._enabled(ext_id):
            return

        def stored(values) -> None:
            if not isinstance(values, dict) or key in self._storage_done or not self._enabled(ext_id):
                return  # (or the writer's own notice came after all)
            real = {k: ({"newValue": values[k]} if "newValue" in c else {}) for k, c in changes.items()
                    if ("newValue" in c and k in values and values[k] == c["newValue"]) or ("newValue" not in c and k not in values)}
            if real:
                self._storage_delivered(key)
                self._deliver_storage(ext_id, area, real, src, eid, ctx, fallback=True)
        self._ask_page(ext_id, f"window.__foxgloveRead ? __foxgloveRead({json.dumps(area)}, {json.dumps(list(changes))}) : null", stored)

    def _storage_delivered(self, key: str) -> None:
        self._storage_done[key] = None
        while len(self._storage_done) > 1000:
            self._storage_done.pop(next(iter(self._storage_done)))

    # ── chrome.action ───────────────────────────────────────────────────────────────────
    def action_value(self, ext_id: str, key: str, tab_id: int | None = None):
        state = self.action.get(ext_id, {})
        for slot in (tab_id, 0):
            if slot is not None and key in state.get(slot, {}):
                return state[slot][key]
        return None

    def action_icon(self, ext_id: str, tab_id: int | None = None) -> QIcon | None:
        value = self.action_value(ext_id, "icon", tab_id)
        info = self.c._info(ext_id)
        if not isinstance(value, dict) or info is None:
            return None
        result, root = QIcon(), Path(info.path()).resolve()
        for src in value.values():
            if not isinstance(src, str):
                continue
            if src.startswith("data:image/") and "," in src:
                pixmap = QPixmap()
                try:
                    if pixmap.loadFromData(base64.b64decode(src.split(",", 1)[1])):
                        result.addPixmap(pixmap)
                except ValueError:
                    pass
            elif QUrl(src).scheme() == "chrome-extension" and QUrl(src).host() == ext_id:
                target = (root / QUrl(src).path().lstrip("/")).resolve()
                if root in target.parents and target.is_file():
                    result.addFile(str(target))
        return None if result.isNull() else result

    def api_action_set(self, ext_id: str, a: dict, ctx: dict):
        key, value, tab_id = a.get("key"), a.get("value"), a.get("tabId")
        if key not in ("badgeText", "badgeBackground", "badgeTextColor", "title", "icon", "popup", "enabled"):
            raise ApiError(f"Unknown action property {key!r}.")
        if isinstance(tab_id, int) and not isinstance(tab_id, bool):
            self._tab(tab_id)
        slot = self.action.setdefault(ext_id, {}).setdefault(tab_id if isinstance(tab_id, int) and tab_id > 0 else 0, {})
        if key in ("badgeBackground", "badgeTextColor"):
            value = _color(value)
            if value is None:
                raise ApiError("The color specification could not be parsed.")
        elif key == "popup" and value and not str(value).startswith(f"chrome-extension://{ext_id}/"):
            raise ApiError("The pop-up must be a page of the extension.")
        if value is None:
            slot.pop(key, None)
        else:
            slot[key] = value
        self.action_changed.emit(ext_id)
        return None

    def text(self, ext_id: str, value) -> str:
        """A manifest string in the user's language (as chrome.i18n has it)."""
        messages = self.c.shim_config(ext_id).get("messages")
        if isinstance(messages, dict):
            return message_text(value, messages)
        info = self.c._info(ext_id)
        return localized(Path(info.path()), self._manifest(ext_id), value) if info is not None else ""

    def default_title(self, ext_id: str) -> str:
        action = self._manifest(ext_id).get("action")
        return self.text(ext_id, action.get("default_title")) if isinstance(action, dict) else ""

    def api_action_get(self, ext_id: str, a: dict, ctx: dict):
        key, tab_id = a.get("key"), a.get("tabId")
        value = self.action_value(ext_id, key, tab_id if isinstance(tab_id, int) else None)
        if value is not None:
            return value
        manifest = self._manifest(ext_id)
        action = manifest.get("action") if isinstance(manifest.get("action"), dict) else {}
        popup = action.get("default_popup")
        return {"badgeText": "", "badgeBackground": BADGE_COLOR, "badgeTextColor": [255, 255, 255, 255], "enabled": True,
                "title": self.default_title(ext_id) or self.text(ext_id, manifest.get("name")),
                "popup": f"chrome-extension://{ext_id}/{popup.lstrip('/')}" if isinstance(popup, str) and popup else ""}.get(key)

    def api_action_openPopup(self, ext_id: str, a: dict, ctx: dict):
        """Only the pop-up: no activeTab and no action.onClicked - those come from the user alone, never the API."""
        win = self._win()
        if not win.open_extension(ext_id, invoked=False):
            raise ApiError("Extension does not have a popup on the active tab.")
        return None

    def action_clicked(self, ext_id: str, tab) -> None:
        self.emit(ext_id, "action.onClicked", [self.tab_info(ext_id, tab)] if tab is not None else [])

    # ── chrome.contextMenus ─────────────────────────────────────────────────────────────
    def menus(self, ext_id: str) -> list[dict]:
        items = (self.c.registry.get(ext_id) or {}).get("menus")
        return items if isinstance(items, list) else []

    def _menu(self, ext_id: str, item_id) -> dict:
        item = next((m for m in self.menus(ext_id) if m.get("id") == item_id), None)
        if item is None:
            raise ApiError(f"Cannot find menu item with id {item_id}")
        return item

    def api_contextMenus_create(self, ext_id: str, a: dict, ctx: dict):
        items = self.c.registry.setdefault(ext_id, {}).setdefault("menus", [])
        item_id = a.get("id")
        if not isinstance(item_id, (str, int)) or isinstance(item_id, bool):
            raise ApiError("Menu items need an id.")
        if any(m.get("id") == item_id for m in items):
            raise ApiError(f"Cannot create item with duplicate id {item_id}")
        if a.get("parentId") is not None:
            self._menu(ext_id, a["parentId"])
        items.append({"id": item_id, **{k: a[k] for k in self.MENU_KEYS if k in a}})
        self.c.save()
        return None

    def api_contextMenus_update(self, ext_id: str, a: dict, ctx: dict):
        item = self._menu(ext_id, a.get("id"))
        props = a.get("props") if isinstance(a.get("props"), dict) else {}
        if props.get("parentId") is not None:
            self._menu(ext_id, props["parentId"])
        item.update({k: props[k] for k in self.MENU_KEYS if k in props})
        self.c.save()
        return None

    def api_contextMenus_remove(self, ext_id: str, a: dict, ctx: dict):
        self._menu(ext_id, a.get("id"))
        items, doomed = self.menus(ext_id), {a.get("id")}
        while True:  # the item and everything below it
            more = {m.get("id") for m in items if m.get("parentId") in doomed} - doomed
            if not more:
                break
            doomed |= more
        items[:] = [m for m in items if m.get("id") not in doomed]
        self.c.save()
        return None

    def api_contextMenus_removeAll(self, ext_id: str, a: dict, ctx: dict):
        self.c.registry.setdefault(ext_id, {})["menus"] = []
        self.c.save()
        return None

    def menu_clicked(self, ext_id: str, item: dict, info: dict, tab) -> None:
        kind = item.get("type")
        if kind in ("checkbox", "radio"):
            info["wasChecked"] = bool(item.get("checked"))
            if kind == "checkbox":
                item["checked"] = not info["wasChecked"]
            else:  # a radio group: neighbouring radio items under the same parent
                items = [m for m in self.menus(ext_id) if m.get("parentId") == item.get("parentId")]
                at = next(i for i, m in enumerate(items) if m is item)
                start, end = at, at
                while start > 0 and items[start - 1].get("type") == "radio":
                    start -= 1
                while end + 1 < len(items) and items[end + 1].get("type") == "radio":
                    end += 1
                for m in items[start:end + 1]:
                    m["checked"] = m is item
            info["checked"] = bool(item["checked"])
            self.c.save()
        self.grant_active_tab(ext_id, tab)
        self.emit(ext_id, "contextMenus.onClicked", [info, self.tab_info(ext_id, tab)] if tab is not None else [info])

    # ── chrome.notifications ────────────────────────────────────────────────────────────
    def api_notifications_create(self, ext_id: str, a: dict, ctx: dict):
        note_id = str(a.get("id") or uuid.uuid4())
        options = a.get("options") if isinstance(a.get("options"), dict) else {}
        self._notes.setdefault(ext_id, {})[note_id] = options
        win = self.c.window
        if win is not None:
            win.show_extension_notification(ext_id, note_id, options)
        return note_id

    def api_notifications_update(self, ext_id: str, a: dict, ctx: dict):
        notes = self._notes.get(ext_id, {})
        note_id = str(a.get("id"))
        if note_id not in notes:
            return False
        notes[note_id].update(a.get("options") or {})
        win = self.c.window
        if win is not None:
            win.show_extension_notification(ext_id, note_id, notes[note_id])
        return True

    def api_notifications_clear(self, ext_id: str, a: dict, ctx: dict):
        return self._notes.get(ext_id, {}).pop(str(a.get("id")), None) is not None

    def api_notifications_getAll(self, ext_id: str, a: dict, ctx: dict):
        return {note_id: True for note_id in self._notes.get(ext_id, {})}

    def notification_clicked(self, ext_id: str, note_id: str) -> None:
        self._notes.get(ext_id, {}).pop(note_id, None)
        self.emit(ext_id, "notifications.onClicked", [note_id])
        self.emit(ext_id, "notifications.onClosed", [note_id, True])

    # ── chrome.alarms ───────────────────────────────────────────────────────────────────
    def _alarm_store(self, ext_id: str) -> dict:
        state = self.c.registry.setdefault(ext_id, {})
        if not isinstance(state.get("alarms"), dict):
            state["alarms"] = {}
        return state["alarms"]

    def api_alarms_create(self, ext_id: str, a: dict, ctx: dict):
        name, now = str(a.get("name") or ""), time.time() * 1000
        least = 0.0 if (self.c.registry.get(ext_id) or {}).get("unpacked") else 0.5  # Chrome's 30 s (not for unpacked ones)
        period = a.get("periodInMinutes")
        period = max(float(period), least) if isinstance(period, (int, float)) and period > 0 else None
        if isinstance(a.get("when"), (int, float)):
            when = float(a["when"])
        elif isinstance(a.get("delayInMinutes"), (int, float)):
            when = now + max(float(a["delayInMinutes"]), least) * 60000
        elif period:
            when = now + period * 60000
        else:
            raise ApiError("An alarm needs 'when', 'delayInMinutes' or 'periodInMinutes'.")
        alarm = {"name": name, "scheduledTime": when}
        if period:
            alarm["periodInMinutes"] = period
        self._alarm_store(ext_id)[name] = alarm
        self.c.save()
        self._arm(ext_id, name)
        return None

    def _arm(self, ext_id: str, name: str) -> None:
        timers = self._alarms.setdefault(ext_id, {})
        old = timers.pop(name, None)
        if old is not None:
            old.stop()
            old.deleteLater()
        alarm = self._alarm_store(ext_id).get(name)
        if not isinstance(alarm, dict):
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda e=ext_id, n=name: self._fire_alarm(e, n))
        timer.start(int(clamp(float(alarm.get("scheduledTime", 0)) - time.time() * 1000, 0, 2 ** 31 - 1)))
        timers[name] = timer

    def _fire_alarm(self, ext_id: str, name: str) -> None:
        store = self._alarm_store(ext_id)
        alarm = store.get(name)
        if not isinstance(alarm, dict) or not self._enabled(ext_id):
            return
        now = time.time() * 1000
        if float(alarm.get("scheduledTime", 0)) > now + 1000:  # a far-off alarm (timers top out at ~24 days)
            self._arm(ext_id, name)
            return
        self.emit(ext_id, "alarms.onAlarm", [dict(alarm)])
        if alarm.get("periodInMinutes"):  # not saved each time: after a restart an overdue alarm fires once, as in Chrome
            alarm["scheduledTime"] = now + float(alarm["periodInMinutes"]) * 60000
            self._arm(ext_id, name)
        else:
            store.pop(name, None)
            self._alarms.get(ext_id, {}).pop(name, None)
            self.c.save()

    def api_alarms_get(self, ext_id: str, a: dict, ctx: dict):
        return self._alarm_store(ext_id).get(str(a.get("name") or ""))

    def api_alarms_getAll(self, ext_id: str, a: dict, ctx: dict):
        return list(self._alarm_store(ext_id).values())

    def api_alarms_clear(self, ext_id: str, a: dict, ctx: dict):
        name = str(a.get("name") or "")
        timer = self._alarms.get(ext_id, {}).pop(name, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        found = self._alarm_store(ext_id).pop(name, None) is not None
        self.c.save()
        return found

    def api_alarms_clearAll(self, ext_id: str, a: dict, ctx: dict):
        for timer in self._alarms.pop(ext_id, {}).values():
            timer.stop()
            timer.deleteLater()
        self._alarm_store(ext_id).clear()
        self.c.save()
        return True

    # ── chrome.tabs ─────────────────────────────────────────────────────────────────────
    def api_tabs_query(self, ext_id: str, q: dict, ctx: dict):
        out = []
        for tab in self.tabs():
            info = self.tab_info(ext_id, tab)
            if self._matches_query(q, tab, info):
                out.append(info)
        return out

    @staticmethod
    def _matches_query(q: dict, tab, info: dict) -> bool:
        for key in ("active", "highlighted", "pinned", "audible", "discarded", "status", "index", "groupId", "autoDiscardable"):
            if q.get(key) is not None and info.get(key) != q[key]:
                return False
        if q.get("muted") is not None and info["mutedInfo"]["muted"] != q["muted"]:
            return False
        for key in ("currentWindow", "lastFocusedWindow"):
            if q.get(key) is not None and (info["windowId"] == MAIN_WINDOW_ID) != bool(q[key]):
                return False
        window = q.get("windowId")
        if isinstance(window, int) and window != -1 and info["windowId"] != (MAIN_WINDOW_ID if window == -2 else window):
            return False
        if q.get("windowType") and q["windowType"] != ("popup" if isinstance(tab, PopupWindow) else "normal"):
            return False
        patterns = [q["url"]] if isinstance(q.get("url"), str) else _strings(q.get("url"))
        if patterns and not ("url" in info and any(match_pattern(p, info["url"]) for p in patterns)):
            return False
        if isinstance(q.get("title"), str) and not ("title" in info and wildcard_match(q["title"], info["title"])):
            return False
        return True

    def api_tabs_get(self, ext_id: str, a: dict, ctx: dict):
        return self.tab_info(ext_id, self._tab(a.get("tabId")))

    def api_tabs_getCurrent(self, ext_id: str, a: dict, ctx: dict):
        caller = ctx.get("from")
        tab = next((t for t in self.tabs() if caller and t.pending is None and t.url().toString() == caller), None)
        return self.tab_info(ext_id, tab) if tab is not None else None

    def api_tabs_create(self, ext_id: str, a: dict, ctx: dict):
        win = self._win()
        url = self._url(a.get("url")) or win._home_url()
        active = a.get("active", a.get("selected", True)) is not False
        opener = next((t for t in win.tabs() if t.tab_id == a.get("openerTabId")), None)
        index = a.get("index") if isinstance(a.get("index"), int) and a.get("index") >= 0 else None
        tab = win.new_tab(url, background=not active, index=index, opener=opener)
        if active:
            win.close_extension_popups()
        return self.tab_info(ext_id, tab)

    def api_tabs_update(self, ext_id: str, a: dict, ctx: dict):
        win = self._win()
        tab = self._tab(a.get("tabId"), current=True)
        url = self._url(a.get("url"))
        if url is not None:
            tab.load(url)
            if tab is win.current_tab():  # the pop-up belonged to the page it just sent away
                win.close_extension_popups(ext_id)
        if (a.get("active") or a.get("highlighted") or a.get("selected")) and win.index_of(tab) >= 0:
            win.tab_bar.setCurrentIndex(win.index_of(tab))
            win.close_extension_popups()
        if isinstance(a.get("muted"), bool):
            tab.page.setAudioMuted(a["muted"])
            if win.index_of(tab) >= 0:
                win._refresh_tab(tab)
        return self.tab_info(ext_id, tab)

    def api_tabs_remove(self, ext_id: str, a: dict, ctx: dict):
        win = self._win()
        for tab in [self._tab(t) for t in (a.get("tabIds") or [])]:
            if isinstance(tab, PopupWindow):
                tab.close()
            else:
                win._remove_tab(tab)
        return None

    def api_tabs_reload(self, ext_id: str, a: dict, ctx: dict):
        tab = self._tab(a.get("tabId"), current=True)
        if tab.pending is not None:
            tab.ensure_loaded()
        else:
            WA = QWebEnginePage.WebAction
            tab.page.triggerAction(WA.ReloadAndBypassCache if a.get("bypassCache") else WA.Reload)
        return None

    def api_tabs_duplicate(self, ext_id: str, a: dict, ctx: dict):
        win, tab = self._win(), self._tab(a.get("tabId"))
        if isinstance(tab, PopupWindow):
            raise ApiError("Pop-up windows can't be duplicated.")
        win.duplicate_tab(tab)
        return self.tab_info(ext_id, win.tab_at(win.index_of(tab) + 1))

    def api_tabs_move(self, ext_id: str, a: dict, ctx: dict):
        win = self._win()
        tabs = [self._tab(t) for t in (a.get("tabIds") or [])]
        index = a.get("index") if isinstance(a.get("index"), int) else -1
        for offset, tab in enumerate(t for t in tabs if not isinstance(t, PopupWindow)):
            target = win.tab_bar.count() - 1 if index < 0 else clamp(index + offset, 0, win.tab_bar.count() - 1)
            win.tab_bar.moveTab(win.index_of(tab), target)
        return [self.tab_info(ext_id, t) for t in tabs]

    def api_tabs_highlight(self, ext_id: str, a: dict, ctx: dict):
        win = self._win()
        indices = [a.get("tabs")] if isinstance(a.get("tabs"), int) else [i for i in a.get("tabs") or [] if isinstance(i, int)]
        if not indices or win.tab_at(indices[0]) is None:
            raise ApiError("No tab at the given index.")
        win.tab_bar.setCurrentIndex(indices[0])
        return self._window_info(ext_id, win, populate=True)

    def api_tabs_navigate(self, ext_id: str, a: dict, ctx: dict):
        tab = self._tab(a.get("tabId"), current=True)
        tab.page.triggerAction(QWebEnginePage.WebAction.Back if a.get("step", -1) < 0 else QWebEnginePage.WebAction.Forward)
        return None

    def api_tabs_captureVisibleTab(self, ext_id: str, a: dict, ctx: dict):
        tab = self._win().current_tab()
        if tab is None or not self.can_access(ext_id, tab.url().toString(), tab.tab_id):
            raise ApiError("Either the '<all_urls>' or 'activeTab' permission is required.")
        options = a.get("options") if isinstance(a.get("options"), dict) else {}
        jpeg = options.get("format") == "jpeg"
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        tab.view.grab().save(buffer, "JPEG" if jpeg else "PNG", int(options.get("quality", 92)) if jpeg else -1)
        return f"data:image/{'jpeg' if jpeg else 'png'};base64," + base64.b64encode(bytes(buffer.data())).decode("ascii")

    def api_tabs_getZoom(self, ext_id: str, a: dict, ctx: dict):
        return self._tab(a.get("tabId"), current=True).page.zoomFactor()

    def api_tabs_setZoom(self, ext_id: str, a: dict, ctx: dict):
        factor = float(a.get("zoomFactor") or 0)
        self._tab(a.get("tabId"), current=True).page.setZoomFactor(clamp(factor, 0.25, 5.0) if factor > 0 else 1.0)
        return None

    def api_tabs_sendMessage(self, ext_id: str, a: dict, ctx: dict):
        try:
            tab = self._tab(a.get("tabId"))
        except ApiError:
            raise ApiError(self.NO_RECEIVER) from None
        if tab.pending is not None:
            raise ApiError(self.NO_RECEIVER)
        call_id = secrets.token_hex(8)
        payload = {"msg": a.get("msg"), "callId": call_id, "frameId": a.get("frameId"),
                   "from": ctx.get("from") or f"chrome-extension://{ext_id}/"}

        def later(reply) -> None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._replies.pop(call_id, None) and reply(error="The message port closed before a response was received."))
            timer.start(300_000)
            self._replies[call_id] = (ext_id, reply, timer)  # before delivering: the answer can beat the callback

            def delivered(ok) -> None:
                if ok is not True and self._replies.pop(call_id, None):
                    timer.stop()
                    reply(error=self.NO_RECEIVER)
            if not self.relay(tab.page, ext_id, payload, delivered):
                delivered(False)
        return later

    def api_tabs_reply(self, ext_id: str, a: dict, ctx: dict):
        entry = self._replies.get(str(a.get("callId")))
        if entry is not None and entry[0] == ext_id:
            self._replies.pop(str(a.get("callId")))
            entry[2].stop()
            entry[1](a.get("value"))
        return None

    # ── chrome.windows ──────────────────────────────────────────────────────────────────
    def _window(self, window_id):
        win = self._win()
        if window_id in (None, -2, MAIN_WINDOW_ID):
            return win
        popup = next((p for p in list(win.popups) if not sip.isdeleted(p) and p.window_id == window_id), None)
        if popup is None:
            raise ApiError(f"No window with id: {window_id}.")
        return popup

    def _window_info(self, ext_id: str, window, populate: bool = False) -> dict:
        popup = isinstance(window, PopupWindow)
        geometry = window.geometry()
        state = ("fullscreen" if window.isFullScreen() else "maximized" if window.isMaximized()
                 else "minimized" if window.isMinimized() else "normal")
        info = {"id": window.window_id if popup else MAIN_WINDOW_ID, "type": "popup" if popup else "normal",
                "focused": window.isActiveWindow(), "state": state, "incognito": False, "alwaysOnTop": False,
                "top": geometry.y(), "left": geometry.x(), "width": geometry.width(), "height": geometry.height()}
        if populate:
            info["tabs"] = [self.tab_info(ext_id, t) for t in ([window] if popup else window.tabs())]
        return info

    def api_windows_get(self, ext_id: str, a: dict, ctx: dict):
        return self._window_info(ext_id, self._window(a.get("windowId")), bool(a.get("populate")))

    def api_windows_getAll(self, ext_id: str, a: dict, ctx: dict):
        win = self.c.window
        if win is None:
            return []
        types = _strings(a.get("windowTypes"))
        windows = [w for w in [win, *(p for p in list(win.popups) if not sip.isdeleted(p))]
                   if not types or ("popup" if isinstance(w, PopupWindow) else "normal") in types]
        return [self._window_info(ext_id, w, bool(a.get("populate"))) for w in windows]

    def api_windows_create(self, ext_id: str, a: dict, ctx: dict):
        win = self._win()
        urls = [u for u in (self._url(raw) for raw in (a.get("url") or [])) if u is not None]
        if a.get("type") in ("popup", "panel"):
            number = lambda key: int(a[key]) if isinstance(a.get(key), (int, float)) else 0
            popup = PopupWindow(win, QRect(number("left"), number("top"), number("width"), number("height")))
            popup.page.load(urls[0] if urls else QUrl("about:blank"))
            popup.show()
            if a.get("focused") is not False:
                popup.activateWindow()
            return self._window_info(ext_id, popup, populate=True)
        for url in urls or [win._home_url()]:  # Foxglove has one browser window: new windows become tabs
            win.new_tab(url)
        win.close_extension_popups()
        return self._window_info(ext_id, win, populate=True)

    def api_windows_update(self, ext_id: str, a: dict, ctx: dict):
        window = self._window(a.get("windowId"))
        state = a.get("state")
        if state == "maximized":
            window.showMaximized()
        elif state == "minimized":
            window.showMinimized()
        elif state == "fullscreen":
            window.showFullScreen()
        elif state == "normal":
            window.showNormal()
        geometry = window.geometry()
        if any(isinstance(a.get(k), (int, float)) for k in ("left", "top", "width", "height")):
            window.setGeometry(int(a.get("left", geometry.x())), int(a.get("top", geometry.y())),
                               int(a.get("width", geometry.width())), int(a.get("height", geometry.height())))
        if a.get("focused") is True:
            window.raise_()
            window.activateWindow()
        return self._window_info(ext_id, window)

    def api_windows_remove(self, ext_id: str, a: dict, ctx: dict):
        window = self._window(a.get("windowId"))
        if not isinstance(window, PopupWindow):
            raise ApiError(f"Extensions can't close the main {APP_NAME} window.")
        window.close()
        return None

    # ── chrome.scripting (in Foxglove's world of the page: DOM access, no chrome.* there) ──
    def _world(self, ext_id: str) -> int:
        """The extension's own isolated world in pages: its injected code can't reach Foxglove's (or another extension's)."""
        if ext_id not in self._worlds:
            self._worlds[ext_id] = FIRST_EXTENSION_WORLD + len(self._worlds)
        return self._worlds[ext_id]

    def _run_js(self, page: QWebEnginePage, code: str, world: int, done, lost) -> None:
        """page.runJavaScript(code, world, done) - or lost() once it's plain the document it went to is gone before it
        answered: Qt never answers then (the page went on to a site in another renderer, or its renderer died). A
        document runs one call after another, so a later call that comes back first tells."""
        state = {"over": False, "ticks": 0, "later": False}

        def finish(result) -> None:
            if not state["over"]:
                state["over"] = True
                done(result)

        def check() -> None:
            if state["over"]:
                return
            state["ticks"] += 1
            if sip.isdeleted(page) or state["later"] or state["ticks"] > 60:  # (a script that runs a minute: given up)
                state["over"] = True
                lost()
                return
            if state["ticks"] % 2:
                page.runJavaScript("0", world, lambda _result: state.__setitem__("later", True))
            QTimer.singleShot(self.SCRIPT_CHECK_MS, check)
        page.runJavaScript(code, world, finish)
        QTimer.singleShot(self.SCRIPT_CHECK_MS, check)

    @staticmethod
    def _guard(here: str, then: str) -> str:
        """JS that does *then* (with `moved`: the page's URL) if the page isn't at *here* (any more)."""
        at = "(location.protocol + '//' + location.host)"  # (unforgeable, in the page's own world too)
        return f"if ({at} !== {json.dumps(here)}) {{ const moved = {{at: location.href}}; {then} }}"

    def _inject(self, ext_id: str, tab, world: int, code, ran, reply) -> None:
        """Run code(here) in the tab's page: code that checks first that the page still is at *here*, where access was
        checked - by the time it gets there the page may have gone on to another site of the same renderer, which runs
        it as soon as that commits - and reports where it is instead. ran(result, moved) gets the result, and calls
        moved(where) for such a report: then it runs again if the extension may access that place as well (as Chrome
        checks in the page), and fails if not. A page that never answers fails it too."""
        page, tries = tab.page, [0]

        def moved(where) -> None:  # nothing ran (in the page's own world the page could change the report: where it
            tries[0] += 1          # says it is, is only where the code checks for next)
            if (tries[0] < 4 and isinstance(where, dict) and isinstance(where.get("at"), str)
                    and not sip.isdeleted(page) and self.can_access(ext_id, where["at"], tab.tab_id)):
                go(_page_here(QUrl(where["at"])))
            else:
                reply(error=self.NO_HOST)

        def go(here: str) -> None:
            self._run_js(page, code(here), world, lambda result: ran(result, moved), lambda: reply(error=self.SCRIPT_GONE))
        go(_page_here(tab.url()))

    @staticmethod
    def _after_directives(script: str, code: str) -> str:
        """*code* in front of a script - but after its "use strict" (a statement before it would turn that off)."""
        m = re.match(r"(?:\s|//[^\n]*|/\*.*?\*/)*(['\"])use strict\1(?:[ \t]*;|(?=[ \t]*(?://[^\n]*)?\r?\n\s*[\w$'\"{]))", script, re.S)
        return script[:m.end()] + ";" + code + script[m.end():] if m else code + script

    def api_scripting_executeScript(self, ext_id: str, a: dict, ctx: dict):
        tab = self._script_tab(ext_id, a.get("target"))
        world = 0 if a.get("world") == "MAIN" else self._world(ext_id)
        key = "__fg" + secrets.token_hex(6)
        func = a["func"] if isinstance(a.get("func"), str) and a["func"].strip() else None
        files = None if func is not None else self._files(ext_id, a.get("files"))

        def code(here: str) -> str:
            # The check stops the whole script, before anything of the extension's runs: a script's last value is its
            # result, it can't return early - and a function's text could close the function it's put in. (Nothing it
            # declares can stand in for what the check uses: location and window can't be replaced, the rest are literals.)
            guard = self._guard(here, f"window.{key}m = moved; throw 'The page went on to another site.';")
            if func is None:
                return self._after_directives(files, guard)
            return (f"{guard} (() => {{ try {{ const r = ({func})(...{json.dumps(a.get('args') or [], default=str)});"
                    f" if (r && typeof r.then === 'function') {{ window.{key} = null; r.then((v) => {{ window.{key} = {{v}}; }},"
                    f" (e) => {{ window.{key} = {{e: String((e && e.message) || e)}}; }}); return {{p: 1}}; }}"
                    f" return {{v: r}}; }} catch (e) {{ return {{e: String((e && e.message) || e)}}; }} }})()")

        def later(reply) -> None:
            page, deadline = tab.page, time.monotonic() + 30
            gone = lambda: reply(error=self.SCRIPT_GONE)

            def answer(value) -> None:
                reply([{"frameId": 0, "documentId": "", "result": value}])

            def settled(result) -> None:  # a promise the function returned
                if isinstance(result, dict) and "e" in result:
                    reply(error=result["e"])
                elif isinstance(result, dict) or time.monotonic() > deadline or sip.isdeleted(page):
                    answer(result.get("v") if isinstance(result, dict) else None)
                else:
                    QTimer.singleShot(50, lambda: self._run_js(page, f"window.{key}", world, settled, gone) if not sip.isdeleted(page) else answer(None))

            def ran(result, moved) -> None:
                if result is None:  # nothing - or the page was elsewhere, and the note says where
                    self._run_js(page, f"(() => {{ const m = window.{key}m; delete window.{key}m; return m === undefined ? null : m; }})()",
                                 world, lambda note: moved(note) if note else answer(None), lambda: answer(None))
                elif func is None:
                    answer(result)
                elif isinstance(result, dict) and "e" in result:
                    reply(error=result["e"])
                elif isinstance(result, dict) and result.get("p"):
                    settled(None)
                else:
                    answer(result.get("v") if isinstance(result, dict) else None)
            self._inject(ext_id, tab, world, code, ran, reply)
        return later

    def _css(self, ext_id: str, a: dict, remove: bool):
        tab = self._script_tab(ext_id, a.get("target"))
        css = a["css"] if isinstance(a.get("css"), str) else self._files(ext_id, a.get("files"))
        key = hashlib.sha256(f"{ext_id}\0{css}".encode()).hexdigest()[:16]
        # A constructed style sheet: the page's CSP (style-src) doesn't apply to it, as it doesn't to Chrome's insertCSS.
        # Kept in the extension's own world, so only it can find the sheet again.
        change = (f"const m = window.__foxgloveCss || (window.__foxgloveCss = new Map()), s = m.get('{key}');"
                  " if (s) { m.delete('" + key + "'); document.adoptedStyleSheets = document.adoptedStyleSheets.filter((x) => x !== s); }"
                  if remove else
                  f"const m = window.__foxgloveCss || (window.__foxgloveCss = new Map()); if (m.has('{key}')) return 1;"
                  f" const s = new CSSStyleSheet(); s.replaceSync({json.dumps(css)}); m.set('{key}', s);"
                  " document.adoptedStyleSheets = [...document.adoptedStyleSheets, s];")

        def later(reply) -> None:
            def ran(result, moved) -> None:
                if isinstance(result, dict) and "m" in result:
                    moved(result["m"])
                else:
                    reply(None)
            self._inject(ext_id, tab, self._world(ext_id), lambda here: f"(() => {{ {self._guard(here, 'return {m: moved};')} {change} return 1; }})()",
                         ran, reply)
        return later

    def api_scripting_insertCSS(self, ext_id: str, a: dict, ctx: dict):
        return self._css(ext_id, a, remove=False)

    def api_scripting_removeCSS(self, ext_id: str, a: dict, ctx: dict):
        return self._css(ext_id, a, remove=True)

    # ── chrome.scripting content scripts: Qt reads content scripts from the manifest when it loads an extension, so
    #    registered ones are written there and run from the extension's next start on (getRegistered... at once) ──
    RUN_AT = ("document_start", "document_end", "document_idle")

    def _registered(self, ext_id: str) -> list[dict]:
        state = self.c.registry.setdefault(ext_id, {})
        if not isinstance(state.get("scripts"), list):
            state["scripts"] = []
        return state["scripts"]

    def _script_entry(self, ext_id: str, raw, old: dict | None = None) -> dict:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise ApiError("Each content script needs an 'id'.")
        sid = raw["id"]
        if sid.startswith("_"):
            raise ApiError(f"Script's ID '{sid}' must not start with '_'.")
        entry = dict(old or {"id": sid, "allFrames": False, "runAt": "document_idle", "persistAcrossSessions": True,
                             "matchOriginAsFallback": False, "world": "ISOLATED"})
        for key in ("matches", "excludeMatches", "js", "css"):
            if key in raw and raw[key] is not None:
                if not isinstance(raw[key], list) or not all(isinstance(v, str) for v in raw[key]):
                    raise ApiError(f"Script with ID '{sid}' has an invalid '{key}'.")
                entry[key] = list(raw[key])
        for key in ("allFrames", "persistAcrossSessions", "matchOriginAsFallback"):
            if key in raw and raw[key] is not None:
                entry[key] = bool(raw[key])
        if raw.get("runAt") is not None:
            if raw["runAt"] not in self.RUN_AT:
                raise ApiError(f"Script with ID '{sid}' has an invalid 'runAt'.")
            entry["runAt"] = raw["runAt"]
        if raw.get("world") is not None:
            if raw["world"] not in ("ISOLATED", "MAIN"):
                raise ApiError(f"Script with ID '{sid}' has an invalid 'world'.")
            entry["world"] = raw["world"]
        if not entry.get("matches"):
            raise ApiError(f"Script with ID '{sid}' must specify 'matches'.")
        # What Chrome refuses here, Qt refuses when it loads the manifest - and then the whole extension wouldn't load
        for pattern in entry.get("matches", []) + entry.get("excludeMatches", []):
            if not valid_match_pattern(pattern) or re.match(r"wss?:", pattern):  # (no content scripts in WebSockets)
                raise ApiError(f"Script with ID '{sid}' has an invalid match pattern: '{pattern}'.")
            if (entry.get("matchOriginAsFallback") and pattern in entry.get("matches", []) and pattern != "<all_urls>"
                    and not re.fullmatch(r"[^/]*://[^/]*/\*", pattern)):
                raise ApiError(f"Script with ID '{sid}': the path of a match pattern must be '*' when "
                               f"'matchOriginAsFallback' is true: '{pattern}'.")
        if not entry.get("js") and not entry.get("css"):
            raise ApiError(f"Script with ID '{sid}' must specify at least one 'css' or 'js' file.")
        info = self.c._info(ext_id)
        root = Path(info.path()).resolve() if info is not None else None
        for kind, names in (("javascript", entry.get("js", [])), ("css", entry.get("css", []))):
            for name in names:
                target = (root / name.lstrip("/")).resolve() if root is not None and name.strip("/") else None
                if target is None or root not in target.parents or not target.is_file():
                    raise ApiError(f"Could not load {kind} '{name}' for script.")
                try:  # Chrome (and Qt) only take UTF-8 content scripts
                    utf8 = NONCHARACTERS.search(target.read_bytes().decode("utf-8")) is None
                except (OSError, UnicodeDecodeError):
                    utf8 = False
                if not utf8:
                    raise ApiError(f"Could not load {kind} '{name}' for script. It isn't UTF-8 encoded.")
        return entry

    def _scripts_changed(self, ext_id: str, before: list) -> None:
        bad = self.c._bad_scripts.get(ext_id)
        if bad:  # what Qt refused to load already: refused at once (a worker registering it at every start would loop)
            pairs = self.registered_scripts(ext_id, ids=True)
            keys = [(sid, json.dumps(e, sort_keys=True)) for sid, e in pairs] + [("", "set:" + json.dumps([e for _sid, e in pairs], sort_keys=True))]
            hit = next(((sid, bad[k]) for sid, k in keys if k in bad), None)
            if hit is not None:
                self._registered(ext_id)[:] = before
                raise ApiError(f"Script with ID '{hit[0]}' couldn't be loaded: {hit[1]}" if hit[0] else f"The content scripts couldn't be loaded: {hit[1]}")
        self.c.save()
        self.c.sync_registered(ext_id, before)

    def api_scripting_register(self, ext_id: str, a: dict, ctx: dict):
        scripts, have = self._registered(ext_id), {s.get("id") for s in self._registered(ext_id)}
        before = [dict(s) for s in scripts]
        new = [self._script_entry(ext_id, raw) for raw in (a.get("scripts") if isinstance(a.get("scripts"), list) else [])]
        for entry in new:
            if entry["id"] in have:
                raise ApiError(f"Duplicate script ID '{entry['id']}'.")
            have.add(entry["id"])
        scripts.extend(new)
        self._scripts_changed(ext_id, before)
        return None

    def api_scripting_update(self, ext_id: str, a: dict, ctx: dict):
        scripts = self._registered(ext_id)
        before = [dict(s) for s in scripts]
        index = {s.get("id"): i for i, s in enumerate(scripts)}
        updated = {}
        for raw in a.get("scripts") if isinstance(a.get("scripts"), list) else []:
            sid = raw.get("id") if isinstance(raw, dict) else None
            if sid not in index:
                raise ApiError(f"Script with ID '{sid}' does not exist or is not fully registered.")
            updated[index[sid]] = self._script_entry(ext_id, raw, scripts[index[sid]])
        for i, entry in updated.items():
            scripts[i] = entry
        self._scripts_changed(ext_id, before)
        return None

    def api_scripting_registered(self, ext_id: str, a: dict, ctx: dict):
        ids = (a.get("filter") or {}).get("ids") if isinstance(a.get("filter"), dict) else None
        return [dict(s) for s in self._registered(ext_id) if not isinstance(ids, list) or s.get("id") in ids]

    def api_scripting_unregister(self, ext_id: str, a: dict, ctx: dict):
        scripts = self._registered(ext_id)
        before = [dict(s) for s in scripts]
        ids = (a.get("filter") or {}).get("ids") if isinstance(a.get("filter"), dict) else None
        if isinstance(ids, list):
            missing = [i for i in ids if i not in {s.get("id") for s in scripts}]
            if missing:
                raise ApiError(f"Nonexistent script ID '{missing[0]}'.")
        scripts[:] = [s for s in scripts if isinstance(ids, list) and s.get("id") not in ids]
        self._scripts_changed(ext_id, before)
        return None

    def registered_scripts(self, ext_id: str, ids: bool = False) -> list:
        """The registered content scripts in manifest form, kept to the hosts the extension may reach (*ids*: as
        (script id, entry) pairs)."""
        out = []
        for entry in self._registered(ext_id) if ext_id in self.c.registry else []:
            matches = [p for p in _strings(entry.get("matches")) if self.covers(ext_id, p)]
            if matches:
                out += [(entry.get("id"), m) for m in registered_as_manifest([{**entry, "matches": matches}])]
        return out if ids else [m for _sid, m in out]

    # ── chrome.webNavigation (frames: Foxglove only knows each tab's top frame) / fontSettings ──
    def _frame(self, tab) -> dict:
        return {"frameId": 0, "parentFrameId": -1, "processId": 0, "url": tab.url().toString(), "errorOccurred": False,
                "documentId": "", "documentLifecycle": "active", "frameType": "outermost_frame"}

    def api_webNavigation_getAllFrames(self, ext_id: str, a: dict, ctx: dict):
        try:
            return [self._frame(self._tab(a.get("tabId")))]
        except ApiError:
            return None

    def api_webNavigation_getFrame(self, ext_id: str, a: dict, ctx: dict):
        try:
            tab = self._tab(a.get("tabId"))
        except ApiError:
            return None
        return self._frame(tab) if a.get("frameId") in (0, None) else None

    def api_fontSettings_getFontList(self, ext_id: str, a: dict, ctx: dict):
        return [{"fontId": f, "displayName": f} for f in sorted(set(QFontDatabase.families()), key=str.lower)]

    # ── chrome.cookies, on the profile's cookie store (Foxglove keeps a copy current: the store has no lookup) ──
    _cookie_key = staticmethod(CookieIndex.key)

    @staticmethod
    def _cookie_info(cookie: QNetworkCookie) -> dict:
        same = {QNetworkCookie.SameSite.None_: "no_restriction", QNetworkCookie.SameSite.Lax: "lax",
                QNetworkCookie.SameSite.Strict: "strict"}.get(cookie.sameSitePolicy(), "unspecified")
        info = {"name": bytes(cookie.name()).decode("utf-8", "replace"), "value": bytes(cookie.value()).decode("utf-8", "replace"),
                "domain": cookie.domain(), "hostOnly": not cookie.domain().startswith("."), "path": cookie.path() or "/",
                "secure": cookie.isSecure(), "httpOnly": cookie.isHttpOnly(), "sameSite": same,
                "session": cookie.isSessionCookie(), "storeId": "0"}
        if not cookie.isSessionCookie():
            info["expirationDate"] = cookie.expirationDate().toMSecsSinceEpoch() / 1000
        return info

    @staticmethod
    def _cookie_applies(cookie: dict, url: QUrl) -> bool:
        host, domain, path = url.host().lower(), cookie["domain"].lower(), url.path() or "/"
        if cookie["hostOnly"] and host != domain.lstrip("."):
            return False
        if not cookie["hostOnly"] and not (host == domain[1:] or host.endswith(domain)):
            return False
        if cookie["secure"] and url.scheme() not in ("https", "wss"):
            return False
        return path == cookie["path"] or path.startswith(cookie["path"].rstrip("/") + "/") or cookie["path"] == "/"

    def _cookie_url(self, ext_id: str, raw) -> QUrl:
        url = QUrl(str(raw or ""))
        if url.scheme() not in ("http", "https") or not url.host():
            raise ApiError(f"Invalid url: \"{raw}\".")
        if not self.can_access(ext_id, url.toString()):
            raise ApiError(f"No host permissions for cookies at url: \"{raw}\".")
        return url

    def _cookie_visible(self, ext_id: str, cookie: dict) -> bool:
        host = cookie["domain"].lstrip(".")
        return any(self.can_access(ext_id, f"{scheme}://{host}{cookie['path']}") for scheme in ("https", "http"))

    def api_cookies_get(self, ext_id: str, a: dict, ctx: dict):
        url = self._cookie_url(ext_id, a.get("url"))
        found = [c for c in map(self._cookie_info, self._cookies.values()) if c["name"] == a.get("name") and self._cookie_applies(c, url)]
        return max(found, key=lambda c: len(c["path"])) if found else None

    def api_cookies_getAll(self, ext_id: str, a: dict, ctx: dict):
        url = self._cookie_url(ext_id, a["url"]) if a.get("url") else None
        domain = str(a.get("domain") or "").lower().lstrip(".")
        out = []
        for c in map(self._cookie_info, self._cookies.values()):
            host = c["domain"].lower().lstrip(".")
            if ((url is not None and not self._cookie_applies(c, url)) or (domain and host != domain and not host.endswith("." + domain))
                    or any(a.get(k) is not None and c[k] != a[k] for k in ("name", "path", "secure", "session"))
                    or not self._cookie_visible(ext_id, c)):
                continue
            out.append(c)
        return out

    def api_cookies_getAllCookieStores(self, ext_id: str, a: dict, ctx: dict):
        return [{"id": "0", "tabIds": [t.tab_id for t in self.tabs()]}]

    def api_cookies_set(self, ext_id: str, a: dict, ctx: dict):
        url = self._cookie_url(ext_id, a.get("url"))
        cookie = QNetworkCookie(str(a.get("name") or "").encode(), str(a.get("value") or "").encode())
        if a.get("domain"):
            cookie.setDomain("." + str(a["domain"]).lstrip("."))
        cookie.setPath(str(a.get("path") or url.path() or "/"))
        cookie.setSecure(bool(a.get("secure")))
        cookie.setHttpOnly(bool(a.get("httpOnly")))
        same = {"no_restriction": QNetworkCookie.SameSite.None_, "lax": QNetworkCookie.SameSite.Lax,
                "strict": QNetworkCookie.SameSite.Strict}.get(a.get("sameSite"))
        if same is not None:
            cookie.setSameSitePolicy(same)
        if isinstance(a.get("expirationDate"), (int, float)) and not isinstance(a.get("expirationDate"), bool):
            cookie.setExpirationDate(QDateTime.fromMSecsSinceEpoch(int(a["expirationDate"] * 1000)))
        key = self._cookie_key(cookie) if a.get("domain") else (self._cookie_key(cookie)[0], url.host().lower(), cookie.path())

        def later(reply) -> None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            waiter = [reply, timer]
            self._cookie_waiters.setdefault(key, []).append(waiter)

            def gave_up() -> None:
                if waiter in self._cookie_waiters.get(key, []):
                    self._cookie_waiters[key].remove(waiter)
                    reply(error=f"Failed to parse or set cookie named \"{key[0]}\".")
            timer.timeout.connect(gave_up)
            timer.start(3000)
            self.c.profile.cookieStore().setCookie(cookie, url)
        return later

    def api_cookies_remove(self, ext_id: str, a: dict, ctx: dict):
        url = self._cookie_url(ext_id, a.get("url"))
        name = QNetworkCookie(str(a.get("name") or "").encode())
        CookieIndex.of(self.c.profile).delete([name], url, keep_others=False)  # Chrome's: every match for the URL
        return {"url": url.toString(), "name": str(a.get("name") or ""), "storeId": "0"}

    def _cookie_added(self, cookie) -> None:
        cookie = QNetworkCookie(cookie)
        key = self._cookie_key(cookie)  # (the index has it already)
        replaced = self._removed.pop(key, None)
        if replaced is not None:
            self._cookie_event(replaced, True, "overwrite")
        self._cookie_event(cookie, False, "explicit")
        for reply, timer in self._cookie_waiters.pop(key, []):
            timer.stop()
            reply(self._cookie_info(cookie))

    def _cookie_removed(self, cookie) -> None:
        cookie = QNetworkCookie(cookie)
        key = self._cookie_key(cookie)
        self._removed[key] = cookie  # an overwrite comes back at once (removed, then added)

        def settle() -> None:
            if not sip.isdeleted(self) and self._removed.get(key) is cookie:
                del self._removed[key]
                expired = not cookie.isSessionCookie() and cookie.expirationDate() <= QDateTime.currentDateTime()
                self._cookie_event(cookie, True, "expired" if expired else "explicit")
        QTimer.singleShot(50, settle)

    def _cookie_event(self, cookie: QNetworkCookie, removed: bool, cause: str) -> None:
        if not self._cookies_ready:
            return
        info = self._cookie_info(cookie)
        for ext_id in self.listeners("cookies.onChanged"):
            if self.has_permission(ext_id, "cookies") and self._cookie_visible(ext_id, info):
                self.emit(ext_id, "cookies.onChanged", [{"removed": removed, "cookie": info, "cause": cause}])

    # ── chrome.runtime / sidePanel ──────────────────────────────────────────────────────
    def open_extension_page(self, ext_id: str, url: QUrl) -> None:
        win = self._win()
        tab = next((t for t in win.tabs() if t.url() == url), None)
        if tab is not None:
            win.tab_bar.setCurrentIndex(win.index_of(tab))
        else:
            win.open_url(url, "tab")
        win.close_extension_popups()

    def api_runtime_openOptionsPage(self, ext_id: str, a: dict, ctx: dict):
        entry = self.c.entry(ext_id)
        if entry is None or entry.options_url.isEmpty():
            raise ApiError("Could not create an options page.")
        self.open_extension_page(ext_id, entry.options_url)
        return None

    def api_sidePanel_open(self, ext_id: str, a: dict, ctx: dict):
        path = a.get("path")
        if not isinstance(path, str) or not path.strip("/"):
            raise ApiError("No side panel is set for the extension.")
        self.open_extension_page(ext_id, QUrl(f"chrome-extension://{ext_id}/{path.lstrip('/')}"))  # no side panels: a tab
        return None

    # ── chrome.offscreen (a hidden page; Qt's own crashes the browser) ──────────────────
    def api_offscreen_createDocument(self, ext_id: str, a: dict, ctx: dict):
        if ext_id in self._offscreen:
            raise ApiError("Only a single offscreen document may be created.")
        url = QUrl(str(a.get("url") or ""))
        if url.scheme() != "chrome-extension" or url.host() != ext_id:
            raise ApiError("The offscreen document must be a page of the extension.")

        def later(reply) -> None:
            self._open_offscreen(ext_id, url, lambda ok: reply() if ok else reply(error="The offscreen document couldn't be loaded."))
        return later

    def _open_offscreen(self, ext_id: str, url: QUrl, done=None) -> None:
        page = self._offscreen[ext_id] = WebPage(self.c.profile, self)

        def loaded(ok: bool) -> None:
            if page.property("foxglove-loaded"):
                return
            page.setProperty("foxglove-loaded", True)
            if done is not None:
                done(ok)
        page.loadFinished.connect(loaded)
        page.load(url)

    def offscreen_url(self, ext_id: str) -> str:
        page = self._offscreen.get(ext_id)
        return page.url().toString() or page.requestedUrl().toString() if page is not None and not sip.isdeleted(page) else ""

    def api_offscreen_closeDocument(self, ext_id: str, a: dict, ctx: dict):
        page = self._offscreen.pop(ext_id, None)
        if page is None:
            raise ApiError("No current offscreen document.")
        page.deleteLater()
        return None

    def api_offscreen_hasDocument(self, ext_id: str, a: dict, ctx: dict):
        page = self._offscreen.get(ext_id)
        return page.url().toString() or page.requestedUrl().toString() if page is not None else ""

    # ── chrome.declarativeNetRequest: kept here, enforced by NetRules ──────────────────
    def _rules(self, ext_id: str, kind: str) -> list:
        if kind == "session":
            return self._session_rules.setdefault(ext_id, [])
        state = self.c.registry.setdefault(ext_id, {})
        if not isinstance(state.get("dnr"), list):
            state["dnr"] = []
        return state["dnr"]

    def api_dnr_update(self, ext_id: str, a: dict, ctx: dict):
        rules = self._rules(ext_id, a.get("kind"))
        doomed = set(a.get("removeRuleIds") or [])
        kept = [r for r in rules if r.get("id") not in doomed]
        ids = {r.get("id") for r in kept}
        for rule in a.get("addRules") or []:
            if not isinstance(rule, dict) or not isinstance(rule.get("id"), int) or rule["id"] < 1:
                raise ApiError("Rules need a positive integer id.")
            if rule["id"] in ids:
                raise ApiError(f"Rule with id {rule['id']} does not have a unique ID.")
            ids.add(rule["id"])
            kept.append(rule)
        rules[:] = kept
        if a.get("kind") != "session":
            self.c.save()
        self.c.net.invalidate()
        return None

    def api_dnr_get(self, ext_id: str, a: dict, ctx: dict):
        wanted = a.get("ruleIds")
        return [r for r in self._rules(ext_id, a.get("kind")) if not isinstance(wanted, list) or r.get("id") in wanted]

    def api_dnr_rulesets(self, ext_id: str, a: dict, ctx: dict):
        manifest = self._manifest(ext_id)
        dnr = manifest.get("declarative_net_request")
        known = [r.get("id") for r in dnr.get("rule_resources") or [] if isinstance(r, dict)] if isinstance(dnr, dict) else []
        enabled = list(self.c.net.enabled_rulesets(ext_id, manifest))
        if "enableRulesetIds" not in a and "disableRulesetIds" not in a:
            return enabled
        for rid in _strings(a.get("enableRulesetIds")) + _strings(a.get("disableRulesetIds")):
            if rid not in known:
                raise ApiError(f"Invalid ruleset id: {rid}.")
        off = set(_strings(a.get("disableRulesetIds")))
        self.c.registry.setdefault(ext_id, {})["rulesets"] = [r for r in dict.fromkeys(enabled + _strings(a.get("enableRulesetIds"))) if r not in off]
        self.c.save()
        self.c.net.invalidate()
        return None

    # ── chrome.permissions ──────────────────────────────────────────────────────────────
    def api_permissions_getAll(self, ext_id: str, a: dict, ctx: dict):
        m, granted = self._manifest(ext_id), self._granted(ext_id)
        perms = [p for p in _strings(m.get("permissions")) if "://" not in p and p != "<all_urls>"]
        return {"permissions": list(dict.fromkeys(perms + _strings(granted.get("permissions")))),
                "origins": list(dict.fromkeys(_strings(m.get("host_permissions")) + _strings(granted.get("origins"))))}

    def _covered(self, ext_id: str, origin: str) -> bool:
        return any(pattern_contains(h, origin) for h in self._hosts(ext_id))

    def api_permissions_contains(self, ext_id: str, a: dict, ctx: dict):
        return (all(self.has_permission(ext_id, p) for p in _strings(a.get("permissions")))
                and all(self._covered(ext_id, o) for o in _strings(a.get("origins"))))

    def api_permissions_request(self, ext_id: str, a: dict, ctx: dict):
        bad = next((o for o in _strings(a.get("origins")) if not valid_match_pattern(o)), None)
        if bad is not None:
            raise ApiError(f"Invalid value for origin pattern {bad}.")
        m = self._manifest(ext_id)
        optional = _strings(m.get("optional_permissions")) + _strings(m.get("optional_host_permissions"))
        perms = [p for p in _strings(a.get("permissions")) if not self.has_permission(ext_id, p)]
        origins = [o for o in _strings(a.get("origins")) if not self._covered(ext_id, o)]
        if any(p not in optional for p in perms) or any(not any(pattern_contains(x, o) for x in optional) for o in origins):
            raise ApiError("Only permissions specified in the manifest may be requested.")
        if not perms and not origins:
            return True
        if ext_id in self._asking:  # one question at a time - and none for a while after a "no" (no dialog loops)
            raise ApiError("A permission request is already in progress.")
        if time.monotonic() < self._refused.get(ext_id, 0):
            return False
        win = self.c.window
        self._asking.add(ext_id)

        def later(reply) -> None:
            def ask() -> None:
                name = (self.c.entry(ext_id).name if self.c.entry(ext_id) else "The extension")
                try:
                    allowed = win is not None and not sip.isdeleted(win) and ask_question(
                        win, "Extension Permissions", f"“{name}” asks for more access:\n\n" + "\n".join(perms + origins), "Allow")
                finally:
                    self._asking.discard(ext_id)
                if allowed:
                    granted = self.c.registry.setdefault(ext_id, {}).setdefault("granted", {})
                    granted["permissions"] = list(dict.fromkeys(_strings(granted.get("permissions")) + perms))
                    granted["origins"] = list(dict.fromkeys(_strings(granted.get("origins")) + origins))
                    self.c.save()
                    self.c.net.invalidate()
                    if self._registered(ext_id):  # registered scripts for hosts it may reach now: into its manifest
                        self.c.sync_registered(ext_id)
                    self.emit(ext_id, "permissions.onAdded", [{"permissions": perms, "origins": origins}])
                else:
                    self._refused[ext_id] = time.monotonic() + self.REFUSED_PAUSE
                reply(bool(allowed))
            QTimer.singleShot(0, ask)
        return later

    def api_permissions_remove(self, ext_id: str, a: dict, ctx: dict):
        m, granted = self._manifest(ext_id), dict(self._granted(ext_id))
        perms, origins = _strings(a.get("permissions")), _strings(a.get("origins"))
        required = _strings(m.get("host_permissions")) + [p for p in _strings(m.get("permissions")) if "://" in p or p == "<all_urls>"]
        optional = _strings(m.get("optional_host_permissions")) + [p for p in _strings(m.get("optional_permissions")) if "://" in p or p == "<all_urls>"]
        # as Chrome: only optional ones, and none the manifest asks for as well
        if (any(p in _strings(m.get("permissions")) or p not in _strings(m.get("optional_permissions")) for p in perms)
                or any(not any(pattern_contains(x, o) for x in optional) or any(pattern_contains(r, o) or pattern_contains(o, r) for r in required)
                       for o in origins)):
            raise ApiError("You cannot remove required permissions.")
        had_perms, had_origins = _strings(granted.get("permissions")), _strings(granted.get("origins"))
        gone_perms = [p for p in had_perms if p in perms]
        gone_origins = [h for h in had_origins if any(pattern_contains(o, h) for o in origins)]
        if not gone_perms and not gone_origins:
            return True
        granted["permissions"] = [p for p in had_perms if p not in gone_perms]
        granted["origins"] = [h for h in had_origins if h not in gone_origins]
        self.c.registry.setdefault(ext_id, {})["granted"] = granted
        self.c.save()
        self.c.net.invalidate()
        if self._registered(ext_id):  # registered scripts for hosts it may not reach any more: out of its manifest
            self.c.sync_registered(ext_id)
        self.emit(ext_id, "permissions.onRemoved", [{"permissions": gone_perms, "origins": gone_origins}])
        return True

    # ── chrome.downloads ────────────────────────────────────────────────────────────────
    def api_downloads_download(self, ext_id: str, a: dict, ctx: dict):
        url = QUrl(str(a.get("url") or ""))
        if not url.isValid() or url.scheme() in ("", "javascript"):
            raise ApiError("Invalid URL.")
        filename = Path(str(a.get("filename") or "")).name
        own = url.scheme() in ("blob", "chrome-extension") and f"chrome-extension://{ext_id}" in url.toString()
        win = self.c.window
        tab = win.current_tab() if win is not None and not own else None
        if tab is not None and tab.pending is None:
            tab.page.download(url, filename)
        else:  # the extension's own (blob) data: download it from a page of the extension
            page = WebPage(self.c.profile, self)
            page.load(QUrl(f"chrome-extension://{ext_id}/{SHIM_BRIDGE}"))
            page.loadFinished.connect(lambda _ok: page.download(url, filename))
            QTimer.singleShot(60_000, page.deleteLater)
        return next(self._downloads)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  chrome.declarativeNetRequest, enforced: Qt keeps an extension's rules but never applies them
# ══════════════════════════════════════════════════════════════════════════════════════════
DNR_ACTIONS = ("allow", "allowAllRequests", "block", "upgradeScheme", "redirect")  # at equal priority the first one wins
DNR_TYPES = frozenset(("main_frame", "sub_frame", "stylesheet", "script", "image", "font", "object", "xmlhttprequest", "ping",
                       "csp_report", "media", "websocket", "webtransport", "webbundle", "other"))
_RT = QWebEngineUrlRequestInfo.ResourceType
DNR_TYPE_OF = {getattr(_RT, "ResourceType" + qt): dnr for qt, dnr in (
    ("MainFrame", "main_frame"), ("NavigationPreloadMainFrame", "main_frame"), ("SubFrame", "sub_frame"),
    ("NavigationPreloadSubFrame", "sub_frame"), ("Stylesheet", "stylesheet"), ("Script", "script"), ("Worker", "script"),
    ("SharedWorker", "script"), ("ServiceWorker", "script"), ("Image", "image"), ("Favicon", "image"), ("FontResource", "font"),
    ("Object", "object"), ("PluginResource", "object"), ("Media", "media"), ("Xhr", "xmlhttprequest"), ("Ping", "ping"),
    ("CspReport", "csp_report"), ("WebSocket", "websocket")) if hasattr(_RT, "ResourceType" + qt)}
_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "mil"}
_COMMON_WORDS = {"http", "https", "www", "com"}  # in nearly every URL: no use for finding rules


def _site(host: str) -> str:
    """Roughly the registrable domain ("news.bbc.co.uk" -> "bbc.co.uk"): first or third party."""
    labels = host.split(".")
    if len(labels) < 3 or re.fullmatch(r"[\d.]+|\[.*\]", host):
        return host
    return ".".join(labels[-3:] if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else labels[-2:])


def _suffixes(host: str) -> list[str]:
    """"a.b.com" -> ["a.b.com", "b.com", "com"]: what a rule's domain list is checked against."""
    labels = host.split(".") if host else []
    return [".".join(labels[i:]) for i in range(len(labels))]


DNR_URL_LIMIT = 8192    # how much of a URL declarativeNetRequest rules look at: a page can't make matching slow with a long one
DNR_REGEX_URL_LIMIT = 2048  # ... for regexFilter rules when only Python's (backtracking) re is there
DNR_REGEX_MAX = 2000    # a longer regexFilter wouldn't fit Chrome's 2 KB RE2 program either
_URL_SEPARATOR = r"(?:[^A-Za-z0-9_\-.%]|\Z)"  # urlFilter's "^": a separator character, or the end of the URL
_URL_AUTHORITY = re.compile(r"[a-z][a-z0-9+.-]*://(?:[^/?#@]*@)?", re.I)  # where "||" looks for the host

try:  # linear-time regular expressions, as Chrome's (pip install google-re2)
    import re2 as _re2
except ImportError:
    _re2 = None


class UrlFilter:
    """A declarativeNetRequest urlFilter ("||ads.example^", "|https://*/x.js|"), matched in linear time.

    The pattern's pieces between "*"s are looked for one after another, each as far to the left as it goes: with "*" as
    the only wildcard that finds a match whenever there is one - and unlike a regular expression with a ".*" per "*",
    no URL a web page makes up can keep the browser busy for long."""
    __slots__ = ("domain", "start", "end", "pieces", "last")

    def __init__(self, text: str, case: bool = False):
        self.domain = text.startswith("||")
        self.start = not self.domain and text.startswith("|")
        text = text[2:] if self.domain else text[1:] if self.start else text
        self.end = text.endswith("|")
        text = text[:-1] if self.end else text
        flags = 0 if case else re.I
        piece = lambda part: "".join(_URL_SEPARATOR if ch == "^" else re.escape(ch) for ch in part)
        parts = text.split("*")
        self.pieces = [re.compile(piece(part), flags) for part in parts]
        self.last = re.compile(piece(parts[-1]) + r"\Z", flags) if self.end else self.pieces[-1]  # ends the URL

    def _first(self, url: str) -> int | None:
        """Where the first piece ends, found at its anchor (or anywhere) - None if it isn't there."""
        first = self.last if len(self.pieces) == 1 else self.pieces[0]
        if self.domain:  # at the start of the host name, or of one of its labels
            m = _URL_AUTHORITY.match(url)
            if m is None:
                return None
            host_end = next((i for i in range(m.end(), len(url)) if url[i] in "/?#"), len(url))
            starts = [m.end()] + [i + 1 for i in range(m.end(), host_end) if url[i] == "."]
            hit = next((h for h in (first.match(url, s) for s in starts) if h is not None), None)
        else:
            hit = first.match(url) if self.start else first.search(url)
        return hit.end() if hit is not None else None

    def search(self, url: str, cut: bool = False) -> bool:
        """Whether the filter matches *url* (*cut*: only the start of a longer URL - nothing can match its end)."""
        if self.end and cut:
            return False
        pos = self._first(url)
        if pos is None:
            return False
        for i in range(1, len(self.pieces)):
            hit = (self.last if i == len(self.pieces) - 1 else self.pieces[i]).search(url, pos)
            if hit is None:
                return False
            pos = hit.end()
        return True


def compile_regex_filter(text: str, case: bool = False):
    """A regexFilter as Chrome takes it: RE2 syntax, so no look-arounds or back-references - with RE2 itself if it's
    installed, else with re (and DNR_REGEX_URL_LIMIT). Raises re.error for one Chrome would refuse."""
    if len(text) > DNR_REGEX_MAX:
        raise re.error("regexFilter too long")
    if _re2 is not None:
        try:
            return _re2.compile(text if case else "(?i)" + text)
        except Exception as exc:  # re2.error
            raise re.error(str(exc)) from exc
    if re.search(r"(?<!\\)(?:\\\\)*(?:\\[1-9]|\\g<|\(\?(?:[=!]|<[=!]|P=))", text):  # RE2 has neither: Chrome refuses it
        raise re.error("look-around or back-reference")
    return re.compile(text, 0 if case else re.I)


def _filter_keys(text: str) -> list[str]:
    """Where a urlFilter rule is filed: the domain of "||domain^", else its longest whole word (one a URL splits out
    as it is - not cut by a "*" or an open end)."""
    low = text.lower()
    m = re.match(r"\|\|([a-z0-9-]+(?:\.[a-z0-9-]+)+)(?:[\^/:]|\|$)", low)
    if m:
        return ["d:" + m.group(1)]
    start, end = low.startswith("|"), low.endswith("|")
    pieces, best = low.lstrip("|")[:-1 if end else None].split("*"), ""
    for i, piece in enumerate(pieces):
        for w in re.finditer(r"[a-z0-9]+", piece):
            whole = (w.start() > 0 or (i == 0 and start)) and (w.end() < len(piece) or (i == len(pieces) - 1 and end))
            if whole and len(w.group()) > len(best) and w.group() not in _COMMON_WORDS:
                best = w.group()
    return ["t:" + best] if best else []


class NetRule:
    """One declarativeNetRequest rule, ready to test against requests (its pattern compiles on first use)."""
    __slots__ = ("id", "ruleset", "priority", "kind", "rank", "action", "text", "is_regex", "case", "regex", "types",
                 "domains", "not_domains", "initiators", "not_initiators", "methods", "not_methods", "party", "tabs",
                 "not_tabs", "keys")

    def __init__(self, raw: dict, ruleset: str):
        cond, action = raw.get("condition"), raw.get("action")
        if not isinstance(cond, dict) or not isinstance(action, dict) or not isinstance(raw.get("id"), int):
            raise ValueError("not a rule")
        if cond.get("responseHeaders") or cond.get("excludedResponseHeaders"):
            raise ValueError("matches responses")  # decided once the response is in: not something Qt lets us see
        self.kind = action.get("type")
        if self.kind not in DNR_ACTIONS and self.kind != "modifyHeaders":
            raise ValueError("unknown action")
        if self.kind == "modifyHeaders" and not action.get("requestHeaders"):
            raise ValueError("response headers only")  # Qt can't change those
        self.id, self.ruleset, self.action = raw["id"], ruleset, action
        self.priority = raw.get("priority") if isinstance(raw.get("priority"), int) else 1
        self.rank = DNR_ACTIONS.index(self.kind) if self.kind in DNR_ACTIONS else len(DNR_ACTIONS)
        value = lambda key, alias=None: cond.get(key, cond.get(alias) if alias else None)  # "domains": the old name
        words = lambda key, alias=None: frozenset(s.lower() for s in _strings(value(key, alias)))
        listed = lambda key, alias=None: words(key, alias) if isinstance(value(key, alias), list) else None
        self.text = cond.get("regexFilter") if isinstance(cond.get("regexFilter"), str) else cond.get("urlFilter")
        self.text = self.text if isinstance(self.text, str) and self.text else ""
        self.is_regex, self.regex = isinstance(cond.get("regexFilter"), str), None
        self.case = cond.get("isUrlFilterCaseSensitive") is True
        types, excluded = listed("resourceTypes"), listed("excludedResourceTypes")
        self.types = types if types else DNR_TYPES - (excluded if excluded is not None else {"main_frame"})
        if self.kind == "allowAllRequests":
            self.types &= {"main_frame", "sub_frame"}
        self.domains, self.not_domains = listed("requestDomains"), words("excludedRequestDomains")
        self.initiators = listed("initiatorDomains", "domains")
        self.not_initiators = words("excludedInitiatorDomains", "excludedDomains")
        self.methods, self.not_methods = listed("requestMethods"), words("excludedRequestMethods")
        self.party = cond.get("domainType") if cond.get("domainType") in ("firstParty", "thirdParty") else None
        ids = lambda key: frozenset(v for v in cond.get(key) if isinstance(v, int)) if isinstance(cond.get(key), list) else None
        self.tabs, self.not_tabs = ids("tabIds"), ids("excludedTabIds") or frozenset()
        self.keys = (["d:" + d for d in self.domains] if self.domains else
                     (_filter_keys(self.text) if self.text and not self.is_regex else []) or
                     (["i:" + d for d in self.initiators] if self.initiators else []))

    def matches(self, req: "NetRequest") -> bool | None:
        """None: it depends on the tab, which this request doesn't know."""
        if req.type not in self.types or (self.domains is not None and self.domains.isdisjoint(req.hosts)):
            return False
        if self.not_domains and not self.not_domains.isdisjoint(req.hosts):
            return False
        if (self.methods is not None and req.method not in self.methods) or req.method in self.not_methods:
            return False
        if self.initiators is not None and self.initiators.isdisjoint(req.initiators):
            return False
        if (self.not_initiators and not self.not_initiators.isdisjoint(req.initiators)) or (self.party and (self.party == "thirdParty") != req.third):
            return False
        if self.text:
            if self.regex is None:
                try:
                    self.regex = compile_regex_filter(self.text, self.case) if self.is_regex else UrlFilter(self.text, self.case)
                except re.error:
                    self.text, self.types = "", frozenset()  # Chrome refuses such a rule when it loads
                    return False
            if not (self.regex.search(req.regex_head) if self.is_regex else self.regex.search(req.head, req.cut)):
                return False
        if self.tabs is None and not self.not_tabs:
            return True
        if req.tab is None:
            return None
        return (self.tabs is None or req.tab in self.tabs) and req.tab not in self.not_tabs


class RuleIndex:
    """A rule set filed by domain, word and initiator, so a request is only tested against rules that might match."""

    def __init__(self, raw_rules, ruleset: str):
        self.keyed: dict[str, list[NetRule]] = {}
        self.generic: list[NetRule] = []
        self.tabbed = self.allow_all = False
        for raw in raw_rules if isinstance(raw_rules, list) else []:
            try:
                rule = NetRule(raw, ruleset)
            except (ValueError, TypeError, AttributeError):
                continue
            self.tabbed = self.tabbed or rule.tabs is not None or bool(rule.not_tabs)
            self.allow_all = self.allow_all or rule.kind == "allowAllRequests"
            for key in rule.keys:
                self.keyed.setdefault(key, []).append(rule)
            if not rule.keys:
                self.generic.append(rule)

    def candidates(self, keys: list[str]):
        yield from self.generic
        for key in keys:
            yield from self.keyed.get(key, ())


@dataclass
class NetRequest:
    url: str                 # without the fragment, percent-encoded
    type: str                # declarativeNetRequest's resource type
    method: str
    hosts: list[str]         # the host and the domains above it
    initiators: list[str]    # the same for the initiator ([] if none)
    third: bool
    tab: int | None          # None: not known here
    keys: list[str] = dc_field(default_factory=list)
    head: str = dc_field(init=False, default="")         # what urlFilters look at (DNR_URL_LIMIT)...
    cut: bool = dc_field(init=False, default=False)      # ... and whether that's only the start of the URL
    regex_head: str = dc_field(init=False, default="")   # what regexFilters look at

    def __post_init__(self) -> None:
        self.head, self.cut = self.url[:DNR_URL_LIMIT], len(self.url) > DNR_URL_LIMIT
        self.regex_head = self.url if _re2 is not None else self.url[:DNR_REGEX_URL_LIMIT]


@dataclass
class NetExtension:
    id: str
    indexes: list
    hosts: list[str]         # host permissions: redirects and header changes need them
    needs_hosts: bool        # declarativeNetRequestWithHostAccess only: every action needs them

    def may(self, url: str) -> bool:
        return any(match_pattern(h, url) for h in self.hosts)


class NetFilter(QWebEngineUrlRequestInterceptor):
    """Qt asks the profile's filter (tab_id None) about every request, then - unless it was blocked or redirected -
    the filter of the tab it belongs to."""

    def __init__(self, rules: "NetRules", tab_id: int | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.rules, self.tab_id = rules, tab_id

    def interceptRequest(self, info) -> None:
        try:
            if info.requestUrl().scheme() == "chrome-extension":
                if self.tab_id is None and not self.polyfill_allowed(info):
                    info.block(True)
                return
            if self.rules.c is None or self.rules.c.filtering:
                self.rules.apply(info, self.tab_id)
        except Exception as exc:  # a rule must never take a request (or the browser) down
            log(f"Request filter error: {exc!r}")

    @staticmethod
    def polyfill_allowed(info) -> bool:
        """Foxglove's files in an extension (its polyfill holds what the extension's content scripts prove themselves
        with) load only in the extension's own pages and worker - or as a page Foxglove opens (its bridge page). Not in
        the extension's sandboxed pages and frames (an opaque origin), which may run content from anywhere."""
        url, initiator = info.requestUrl(), info.initiator()
        if not url.path().rsplit("/", 1)[-1].lower().startswith("foxglove-"):
            return True
        if initiator.isEmpty() or not initiator.scheme():  # the browser - or an opaque origin, for which Qt gives none
            return info.resourceType() in (_RT.ResourceTypeMainFrame, _RT.ResourceTypeServiceWorker)
        return initiator.scheme() == "chrome-extension" and initiator.host() == url.host()


class NetRules:
    """Applies the enabled extensions' declarativeNetRequest rules - their rule sets, dynamic and session rules - to
    every request: block, allow, allowAllRequests, redirect, upgradeScheme and request-header changes. Response
    headers can't be changed from here, and allowAllRequests counts for the top page of a tab, not each frame."""

    def __init__(self, controller: "ExtensionsController"):
        self.c = controller
        self._exts: list[NetExtension] | None = None
        self._static: dict[str, tuple[float, RuleIndex]] = {}  # rule set file -> (mtime, index)
        self._documents: dict[tuple[str, str], int | None] = {}  # (ext id, page URL) -> its allowAllRequests priority
        self._deferred = None    # the request the profile's filter left to the tab's (a rule needs the tab)
        self.tabbed = False

    def invalidate(self) -> None:
        self._exts = None
        self._documents.clear()
        if self.c is not None and not sip.isdeleted(self.c):
            self.c.sync_filtering()  # (an extension switched on or off, a permission granted...)

    def enabled_rulesets(self, ext_id: str, manifest: dict) -> list[str]:
        dnr = manifest.get("declarative_net_request")
        resources = [r for r in dnr.get("rule_resources") or [] if isinstance(r, dict)] if isinstance(dnr, dict) else []
        state = self.c.registry.get(ext_id) or {}
        return state["rulesets"] if isinstance(state.get("rulesets"), list) else [r.get("id") for r in resources if r.get("enabled")]

    def _ruleset(self, root: Path, rel: str, ruleset: str) -> RuleIndex | None:
        path = (root / rel.lstrip("/")).resolve()
        try:
            mtime = path.stat().st_mtime
            if root not in path.parents:
                return None
            cached = self._static.get(str(path))
            if cached is None or cached[0] != mtime:
                cached = self._static[str(path)] = (mtime, RuleIndex(_read_json_file(path), ruleset))
        except (OSError, ValueError) as exc:
            log(f"Couldn't read the rule set {path}: {exc}")
            return None
        return cached[1]

    def extensions(self) -> list[NetExtension]:
        """The enabled extensions with rules, oldest first (a newer one's redirect wins, as in Chrome)."""
        if self._exts is not None:
            return self._exts
        bridge, exts = self.c.bridge, []
        infos = [i for i in self.c._infos() if i.isEnabled() and i.isLoaded()]
        for info in sorted(infos, key=lambda i: (self.c.registry.get(i.id()) or {}).get("installed", 0)):
            ext_id, root = info.id(), Path(info.path()).resolve()
            manifest = self.c._manifest(info.path())
            dnr = bridge.has_permission(ext_id, "declarativeNetRequest")
            if not dnr and not bridge.has_permission(ext_id, "declarativeNetRequestWithHostAccess"):
                continue
            indexes = [RuleIndex(bridge._session_rules.get(ext_id), "_session"),
                       RuleIndex((self.c.registry.get(ext_id) or {}).get("dnr"), "_dynamic")]
            dnr_key = manifest.get("declarative_net_request")
            resources = dnr_key.get("rule_resources") if isinstance(dnr_key, dict) else None
            paths = {r.get("id"): r.get("path") for r in resources if isinstance(r, dict)} if isinstance(resources, list) else {}
            for ruleset in self.enabled_rulesets(ext_id, manifest):
                if isinstance(paths.get(ruleset), str):
                    indexes.append(self._ruleset(root, paths[ruleset], str(ruleset)))
            indexes = [i for i in indexes if i is not None and (i.keyed or i.generic)]
            if indexes:
                exts.append(NetExtension(ext_id, indexes, bridge._hosts(ext_id), not dnr))
        self._exts = exts
        self.tabbed = any(i.tabbed for e in exts for i in e.indexes)
        return exts

    def _request(self, url: QUrl, kind: str, method: str, initiator: QUrl, first_party: QUrl, tab: int | None) -> NetRequest:
        text = url.adjusted(QUrl.UrlFormattingOption.RemoveFragment).toString(QUrl.ComponentFormattingOption.FullyEncoded)
        host, source = url.host().lower(), initiator.host().lower()
        party = source or first_party.host().lower()
        third = kind != "main_frame" and bool(party) and _site(host) != _site(party)
        req = NetRequest(text, kind, method, _suffixes(host), _suffixes(source), third, tab)
        req.keys = (["d:" + h for h in req.hosts] + ["t:" + w for w in set(re.findall(r"[a-z0-9]+", req.head.lower()))]
                    + ["i:" + h for h in req.initiators])
        return req

    def _frame_allowed(self, ext: NetExtension, page: QUrl) -> int | None:
        """The priority of the extension's allowAllRequests rule for the tab's page, if one matched it."""
        key = (ext.id, page.toString())
        if key not in self._documents:
            if len(self._documents) > 500:
                self._documents.clear()
            req = self._request(page, "main_frame", "get", QUrl(), page, -1)
            hits = [r.priority for i in ext.indexes if i.allow_all for r in i.candidates(req.keys)
                    if r.kind == "allowAllRequests" and r.matches(req)]
            self._documents[key] = max(hits) if hits else None
        return self._documents[key]

    def decide(self, ext: NetExtension, req: NetRequest, page: QUrl):
        """(winning rule or None, header rules to apply) - or None if it depends on the tab."""
        best, headers = None, []
        for index in ext.indexes:
            for rule in index.candidates(req.keys):
                hit = rule.matches(req)
                if hit is None:
                    return None
                if hit and rule.kind == "modifyHeaders":
                    headers.append(rule)
                elif hit and (best is None or (rule.priority, -rule.rank) > (best.priority, -best.rank)):
                    best = rule
        if req.type not in ("main_frame", "sub_frame") and page.isValid() and any(i.allow_all for i in ext.indexes):
            allowed = self._frame_allowed(ext, page)
            if allowed is not None and (best is None or best.priority <= allowed):  # the page is allowed: so is this
                return None, [h for h in headers if h.priority > allowed]
        if best is not None and best.kind in ("block", "redirect", "upgradeScheme"):
            return best, []
        return best, [h for h in headers if best is None or h.priority > best.priority]

    def apply(self, info, tab: int | None) -> None:
        url = info.requestUrl()
        if url.scheme() not in ("http", "https", "ws", "wss") or sip.isdeleted(self.c):
            return
        exts = self.extensions()
        if not exts:
            return
        kind = DNR_TYPE_OF.get(info.resourceType(), "other")
        key = (url.toString(), kind)
        if tab is not None:  # a tab's filter only finishes what the profile's left to it
            if not self.tabbed or self._deferred != key:
                return
        self._deferred = None
        initiator, page = info.initiator(), info.firstPartyUrl()
        req = self._request(url, kind, bytes(info.requestMethod()).decode("latin-1").lower(), initiator, page, tab)
        block, redirect, headers = False, None, []
        for ext in exts:
            if initiator.scheme() == "chrome-extension" and initiator.host() != ext.id:  # other extensions' requests: not theirs
                continue
            decided = self.decide(ext, req, page)
            if decided is None:
                if tab is None:
                    self._deferred = key
                return
            best, rules = decided
            action = best.kind if best is not None else ""
            if action in ("block", "upgradeScheme") and (not ext.needs_hosts or ext.may(req.url)):  # no host access needed
                block, redirect = block or action == "block", (ext, best) if action == "upgradeScheme" else redirect
            elif action == "redirect" and ext.may(req.url):
                redirect = (ext, best)
            headers += [r for r in sorted(rules, key=lambda r: r.priority) if ext.may(req.url)]
        if block:
            info.block(True)
            return
        target = self._target(*redirect, req, url) if redirect else None
        if target is not None and target.isValid() and target != url:
            info.redirect(target)
            return
        for rule in headers:
            self._headers(info, rule)

    @staticmethod
    def _target(ext: NetExtension, rule: NetRule, req: NetRequest, url: QUrl) -> QUrl | None:
        if rule.kind == "upgradeScheme":
            target = QUrl(url)
            target.setScheme({"http": "https", "ws": "wss"}.get(url.scheme(), url.scheme()))
            return target
        how = rule.action.get("redirect") if isinstance(rule.action.get("redirect"), dict) else {}
        if isinstance(how.get("url"), str):
            return QUrl(how["url"])
        if isinstance(how.get("extensionPath"), str) and how["extensionPath"].startswith("/"):
            scheme = "chrome-extension" if req.type == "main_frame" else EXT_SCHEME  # Qt refuses chrome-extension:// in pages
            return QUrl(f"{scheme}://{ext.id}{how['extensionPath']}")
        if isinstance(how.get("regexSubstitution"), str) and rule.is_regex and rule.regex is not None:
            m = rule.regex.search(req.regex_head)  # (a prefix of req.url: the positions are the same)
            if m is None:
                return None
            group = lambda d: (m.group(int(d.group(1))) or "") if int(d.group(1)) <= rule.regex.groups else ""
            return QUrl(req.url[:m.start()] + re.sub(r"\\(\d)", group, how["regexSubstitution"]) + req.url[m.end():])
        change = how.get("transform")
        if not isinstance(change, dict):
            return None
        target = QUrl(url)
        for key, setter in (("scheme", target.setScheme), ("host", target.setHost), ("username", target.setUserName),
                            ("password", target.setPassword)):
            if isinstance(change.get(key), str):
                setter(change[key])
        if "port" in change:
            target.setPort(int(change["port"]) if str(change["port"]).isdigit() else -1)
        if isinstance(change.get("path"), str):
            target.setPath(change["path"])
        if isinstance(change.get("query"), str):
            target.setQuery(change["query"][1:] if change["query"].startswith("?") else None)
        elif isinstance(change.get("queryTransform"), dict):
            target.setQuery(NetRules._query(url.query(QUrl.ComponentFormattingOption.FullyEncoded), change["queryTransform"]) or None)
        if isinstance(change.get("fragment"), str):
            target.setFragment(change["fragment"][1:] if change["fragment"].startswith("#") else None)
        return target

    @staticmethod
    def _query(query: str, change: dict) -> str:
        name = lambda pair: pair.split("=", 1)[0]
        doomed = set(_strings(change.get("removeParams")))
        pairs = [p for p in query.split("&") if p and name(p) not in doomed and unquote(name(p)) not in doomed]
        for item in change.get("addOrReplaceParams") or []:
            if isinstance(item, dict) and isinstance(item.get("key"), str):
                pair = f"{quote(item['key'], safe='')}={quote(str(item.get('value', '')), safe='')}"
                at = next((i for i, p in enumerate(pairs) if unquote(name(p)) == item["key"]), None)
                if at is not None:
                    pairs[at] = pair
                elif not item.get("replaceOnly"):
                    pairs.append(pair)
        return "&".join(pairs)

    @staticmethod
    def _headers(info, rule: NetRule) -> None:
        current = {bytes(k).decode("latin-1").lower(): bytes(v) for k, v in info.httpHeaders().items()}
        for change in rule.action.get("requestHeaders") or []:
            name = change.get("header") if isinstance(change, dict) else None
            if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                continue
            value = str(change.get("value", "")).encode("utf-8")
            if change.get("operation") == "set":
                info.setHttpHeader(name.encode(), value)
            elif change.get("operation") == "append":
                old = current.get(name.lower())
                info.setHttpHeader(name.encode(), old + b", " + value if old else value)
            elif change.get("operation") == "remove":
                info.setHttpHeader(name.encode(), b"")  # Qt can't drop a header: an empty one is the closest
            current[name.lower()] = value


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Internal pages (foxglove://newtab)
# ══════════════════════════════════════════════════════════════════════════════════════════
NTP_MAX_SHORTCUTS, NTP_MAX_MOST_VISITED = 10, 8  # as in Chrome
NTP_SHORTCUT_SCHEMES = ("http", "https", "ftp", "file")
# Customize Chrome > Appearance: a theme is made from one of these colours (light and dark variants below)
NTP_COLORS = (("blue", "Blue", "#1a73e8"), ("aqua", "Aqua", "#12a4af"), ("green", "Green", "#1e8e3e"),
              ("viridian", "Viridian", "#0f7b6c"), ("citron", "Citron", "#a8a215"), ("orange", "Orange", "#e8710a"),
              ("apricot", "Apricot", "#e8936b"), ("rose", "Rose", "#d96570"), ("pink", "Pink", "#d0518a"),
              ("fuchsia", "Fuchsia", "#b146c2"), ("violet", "Violet", "#7b5bd6"), ("grey", "Grey", "#6e7681"))
# Chrome's New Tab page colours (CSS variables), light and dark
NTP_LIGHT = {
    "bg": "#ffffff", "text": "#1f1f1f", "text-2": "#474747", "icon": "#5f6368", "logo": "#5f6368", "logo-text": "#1f1f1f",
    "box-bg": "#ffffff", "box-hover": "#ffffff", "box-shadow": "0 1px 6px 0 rgba(32,33,36,.28)",
    "box-shadow-hover": "0 1px 6px 0 rgba(32,33,36,.28), 0 0 0 1px rgba(32,33,36,.08)", "placeholder": "#5f6368",
    "tile-bg": "#f1f3f4", "tile-hover": "rgba(31,31,31,.06)", "tile-text": "#1f1f1f",
    "ogb-text": "#1f1f1f", "ogb-hover": "rgba(31,31,31,.08)", "avatar-bg": "#e3e3e3", "avatar-fg": "#5f6368",
    "menu-bg": "#ffffff", "menu-hover": "rgba(31,31,31,.06)",
    "menu-shadow": "0 1px 2px 0 rgba(60,64,67,.3), 0 2px 6px 2px rgba(60,64,67,.15)",
    "apps-bg": "#e9eef6", "apps-card": "#ffffff", "dialog-bg": "#ffffff", "field-bg": "#f1f3f4", "error": "#b3261e",
    "primary": "#0b57d0", "on-primary": "#ffffff", "outline": "#c7c7c7", "link": "#0b57d0",
    "tonal": "#d3e3fd", "on-tonal": "#041e49", "toast-bg": "#303030", "toast-text": "#f2f2f2", "toast-link": "#a8c7fa",
    "panel-bg": "#ffffff", "card-bg": "#f0f4f9", "divider": "#e3e3e3", "focus": "#0b57d0", "scrim": "rgba(0,0,0,.32)",
}
NTP_DARK = {
    "bg": "#202124", "text": "#e8eaed", "text-2": "#bdc1c6", "icon": "#9aa0a6", "logo": "#ffffff", "logo-text": "#ffffff",
    "box-bg": "#303134", "box-hover": "#3c4043", "box-shadow": "0 1px 6px 0 rgba(0,0,0,.28)",
    "box-shadow-hover": "0 1px 6px 0 rgba(0,0,0,.28)", "placeholder": "#9aa0a6",
    "tile-bg": "#303134", "tile-hover": "rgba(255,255,255,.1)", "tile-text": "#e8eaed",
    "ogb-text": "#e8eaed", "ogb-hover": "rgba(255,255,255,.1)", "avatar-bg": "#3c4043", "avatar-fg": "#c4c7c5",
    "menu-bg": "#303134", "menu-hover": "rgba(255,255,255,.08)",
    "menu-shadow": "0 1px 2px 0 rgba(0,0,0,.3), 0 2px 6px 2px rgba(0,0,0,.15)",
    "apps-bg": "#282a2c", "apps-card": "#1f1f1f", "dialog-bg": "#303134", "field-bg": "#202124", "error": "#f2b8b5",
    "primary": "#a8c7fa", "on-primary": "#062e6f", "outline": "#5f6368", "link": "#a8c7fa",
    "tonal": "#004a77", "on-tonal": "#c2e7ff", "toast-bg": "#e3e3e3", "toast-text": "#1f1f1f", "toast-link": "#0b57d0",
    "panel-bg": "#292a2d", "card-bg": "#202124", "divider": "#3c4043", "focus": "#a8c7fa", "scrim": "rgba(0,0,0,.5)",
}


class NtpError(Exception):
    pass


def _mix(a: str, b: str, amount: float) -> str:
    """*amount* of colour *a*, the rest *b*."""
    x, y = QColor(a), QColor(b)
    return QColor(*(round(getattr(x, c)() * amount + getattr(y, c)() * (1 - amount))
                    for c in ("red", "green", "blue"))).name()


def ntp_theme_css(theme: str) -> str:
    """The New Tab page's colours for a theme ("" = Chrome's default): light, dark, and following the system."""
    light, dark = dict(NTP_LIGHT), dict(NTP_DARK)
    seed = next((color for key, _name, color in NTP_COLORS if key == theme), None)
    if seed:
        light.update({"bg": _mix(seed, "#ffffff", .14), "tile-bg": _mix(seed, "#ffffff", .24),
                      "logo": _mix(seed, "#000000", .62), "logo-text": _mix(seed, "#000000", .5),
                      "tonal": _mix(seed, "#ffffff", .3), "on-tonal": _mix(seed, "#000000", .3),
                      "primary": _mix(seed, "#000000", .75), "link": _mix(seed, "#000000", .75), "focus": seed,
                      "card-bg": _mix(seed, "#ffffff", .1)})
        dark.update({"bg": _mix(seed, "#1b1b1b", .2), "box-bg": _mix(seed, "#2a2a2a", .22),
                     "box-hover": _mix(seed, "#363636", .25), "tile-bg": _mix(seed, "#2a2a2a", .3),
                     "tonal": _mix(seed, "#000000", .45), "on-tonal": _mix(seed, "#ffffff", .3),
                     "primary": _mix(seed, "#ffffff", .45), "on-primary": _mix(seed, "#000000", .25),
                     "link": _mix(seed, "#ffffff", .45), "focus": _mix(seed, "#ffffff", .45),
                     "panel-bg": _mix(seed, "#232323", .12), "card-bg": _mix(seed, "#1b1b1b", .18)})

    def block(palette: dict) -> str:
        return "".join(f"--{name}:{value};" for name, value in palette.items())
    return (f":root{{{block(light)}}}:root[data-appearance=dark]{{{block(dark)}}}"
            f"@media (prefers-color-scheme: dark){{:root[data-appearance=system]{{{block(dark)}}}}}")


class NewTabPage(QObject):
    """Google Chrome's New Tab page (foxglove://newtab), filled in for this profile: the search box, shortcuts ("My
    shortcuts", or the most visited sites from history) and Customize Chrome. The page changes them through
    foxglove://newtab/api - and only that page can: foxglove:// is a local scheme, so web pages and extensions can't
    even load it, and the API also wants the request to come from the foxglove://newtab origin with the token this
    browser run put into the page."""

    def __init__(self, settings: Settings, history: HistoryStore, favicons: FaviconCache, parent: QObject | None = None):
        super().__init__(parent)
        self.settings, self.history, self.favicons = settings, history, favicons
        self.token = secrets.token_urlsafe(24)

    # ── what the page shows ──
    def _hidden(self) -> list[str]:
        return [u for u in self.settings.get("ntp_hidden") if isinstance(u, str)]

    def most_visited(self, limit: int = NTP_MAX_MOST_VISITED) -> list[dict]:
        return [{"title": title or display_url(QUrl(url)), "url": url}
                for url, title in self.history.top_sites(limit, self._hidden())]

    def shortcuts(self) -> list[dict]:
        """My shortcuts: the most visited sites until the first change (as in Chrome), then the user's own list."""
        if not self.settings.get("ntp_shortcuts_edited"):
            return self.most_visited()
        return [{"title": str(s.get("title") or ""), "url": s["url"]} for s in self.settings.get("ntp_shortcuts")
                if isinstance(s, dict) and isinstance(s.get("url"), str)][:NTP_MAX_SHORTCUTS]

    def _icon_url(self, url: str) -> str:
        host = QUrl(url).host().lower()
        if not host or not self.favicons._file(host).exists():
            return ""
        return f"/favicon?host={quote(host)}&t={self.token}"

    def state(self) -> dict:
        mode = self.settings.get("ntp_shortcut_mode")
        mode = mode if mode in ("custom", "most_visited") else "custom"
        theme = self.settings.get("ntp_theme")
        theme = theme if any(theme == key for key, _n, _c in NTP_COLORS) else ""
        tiles = self.shortcuts() if mode == "custom" else self.most_visited()
        return {"mode": mode, "show": bool(self.settings.get("ntp_show_shortcuts")),
                "max": NTP_MAX_SHORTCUTS if mode == "custom" else NTP_MAX_MOST_VISITED,
                "tiles": [dict(t, icon=self._icon_url(t["url"])) for t in tiles],
                "theme": theme, "theme_css": ntp_theme_css(theme), "hidden": len(self._hidden())}

    def render(self) -> str:
        engine = self.settings.get("search_engine")
        google = engine == "Google"
        appearance = self.settings.get("website_appearance")
        state = dict(self.state(), token=self.token, engine=engine)
        nonce = secrets.token_urlsafe(16)
        colors = "".join(f'<button type="button" class="chip" data-theme="{key}" title="{name}" aria-label="{name}" '
                         f'style="--chip:{color}"></button>' for key, name, color in NTP_COLORS)
        values = {
            "APPEARANCE": appearance if appearance in ("dark", "light", "system") else "dark",
            "LOGO_MODE": "single" if state["theme"] else "color",
            "THEME_CSS": state["theme_css"],
            "OGB": "" if google else "hidden",
            "LOGO": GOOGLE_LOGO_SVG if google else f'<div class="wordmark">{html.escape(engine)}</div>',
            "PLACEHOLDER": html.escape(f"Search {engine} or type a URL", quote=True),
            "APPS": "".join(f'<a class="app" href="{url}">{NTP_APP_ICONS[key]}<span>{name}</span></a>'
                            for key, name, url in NTP_APPS),
            "COLORS": colors,
            "NONCE": nonce,
            "STATE": json.dumps(state).replace("<", "\\u003c"),
        }
        return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values[m.group(1)], NEWTAB_HTML)

    # ── the page's requests ──
    def trusted(self, job: QWebEngineUrlRequestJob, token: str = "") -> bool:
        """Only the New Tab page itself: its origin, and the token it was served with."""
        initiator = job.initiator()
        if initiator.scheme() != "foxglove" or initiator.host() != "newtab":
            return False
        if not token:
            headers = {bytes(k).decode("latin-1").lower(): bytes(v) for k, v in job.requestHeaders().items()}
            token = headers.get("x-ntp-token", b"").decode("latin-1")
        return hmac.compare_digest(token.encode("utf-8", "replace"), self.token.encode())

    def favicon(self, job: QWebEngineUrlRequestJob) -> bytes | None:
        query = dict(part.split("=", 1) for part in job.requestUrl().query().split("&") if "=" in part)
        if not self.trusted(job, unquote(query.get("t", ""))):
            return None
        host = unquote(query.get("host", "")).lower()
        try:
            return self.favicons._file(host).read_bytes() if host else None
        except OSError:
            return None

    def api(self, job: QWebEngineUrlRequestJob) -> dict | None:
        """Answers a request from the page (None: refused); a change answers with the page's new state."""
        if job.requestMethod() != b"POST" or not self.trusted(job):
            return None
        device = job.requestBody()
        try:
            if device is not None and not device.isOpen():
                device.open(QIODevice.OpenModeFlag.ReadOnly)
            request = json.loads(bytes(device.read(256 * 1024)).decode("utf-8")) if device is not None else None
            if not isinstance(request, dict):
                raise NtpError("Bad request")
            return self._handle(request)
        except NtpError as exc:
            return {"error": str(exc)}
        except (ValueError, UnicodeError):
            return {"error": "Bad request"}

    @staticmethod
    def shortcut_url(text) -> str:
        text = str(text or "").strip()
        if not text:
            raise NtpError("Type a URL")
        if "://" not in text and not text.lower().startswith(("file:", "about:")):
            text = "https://" + text
        url = QUrl(text, QUrl.ParsingMode.StrictMode)
        if (not url.isValid() or url.scheme().lower() not in NTP_SHORTCUT_SCHEMES or len(text) > 4096
                or (url.scheme().lower() != "file" and not url.host())):
            raise NtpError("Type a valid URL")
        return url.toString()

    def _item(self, raw, items: list[dict], skip: int = -1) -> dict:
        raw = raw if isinstance(raw, dict) else {}
        url = self.shortcut_url(raw.get("url"))
        if any(i != skip and item["url"] == url for i, item in enumerate(items)):
            raise NtpError("Shortcut already exists")
        title = " ".join(str(raw.get("title") or "").split())[:200]
        return {"title": title or display_url(QUrl(url)), "url": url}

    @staticmethod
    def _index(value, items: list) -> int:
        if type(value) is not int or not 0 <= value < len(items):
            raise NtpError("That shortcut is gone - reload the page")
        return value

    def _handle(self, request: dict) -> dict:
        action, s = request.get("action"), self.settings
        if action == "navigate":
            url = url_from_input(str(request.get("text") or "")[:8192], s.search_template())
            if url.scheme().lower() in ("javascript", "data", "blob", "view-source"):
                url = QUrl(s.search_template().format(quote_plus(str(request.get("text")))))
            return {"url": url.toString(QUrl.ComponentFormattingOption.FullyEncoded)}
        if action == "prefs":
            if request.get("mode") in ("custom", "most_visited"):
                s.set("ntp_shortcut_mode", request["mode"])
            if isinstance(request.get("show"), bool):
                s.set("ntp_show_shortcuts", request["show"])
            if request.get("theme") == "" or any(request.get("theme") == key for key, _n, _c in NTP_COLORS):
                s.set("ntp_theme", request["theme"])
        elif action in ("add", "edit", "remove", "move", "set"):
            items = [dict(t) for t in self.shortcuts()]
            if action == "add":
                if len(items) >= NTP_MAX_SHORTCUTS:
                    raise NtpError(f"You can have up to {NTP_MAX_SHORTCUTS} shortcuts")
                items.append(self._item(request, items))
            elif action == "edit":
                index = self._index(request.get("index"), items)
                items[index] = self._item(request, items, skip=index)
            elif action == "remove":
                del items[self._index(request.get("index"), items)]
            elif action == "move":
                item = items.pop(self._index(request.get("index"), items))
                to = request.get("to") if type(request.get("to")) is int else len(items)
                items.insert(clamp(to, 0, len(items)), item)
            else:  # "set": undoing a change puts the list back
                raw = request.get("items") if isinstance(request.get("items"), list) else []
                items = []
                for entry in raw[:NTP_MAX_SHORTCUTS]:
                    items.append(self._item(entry, items))
            s.set("ntp_shortcuts", items)
            s.set("ntp_shortcuts_edited", True)
        elif action in ("hide", "unhide"):
            url, hidden = str(request.get("url") or ""), self._hidden()
            if action == "hide" and url and url not in hidden:
                s.set("ntp_hidden", (hidden + [url])[-500:])
            elif action == "unhide" and url in hidden:
                s.set("ntp_hidden", [u for u in hidden if u != url])
        elif action == "unhide_all":
            s.set("ntp_hidden", [])
        elif action != "state":
            raise NtpError("Unknown request")
        return self.state()


class InternalPages(QWebEngineUrlSchemeHandler):
    """foxglove://newtab: the New Tab page, its favicons (/favicon) and its API (/api)."""

    def __init__(self, newtab, parent: QObject | None = None):
        """*newtab*: a NewTabPage, or a function returning the page's HTML (tests)."""
        super().__init__(parent)
        self.ntp = newtab if isinstance(newtab, NewTabPage) else None
        self.render_newtab = newtab.render if self.ntp is not None else newtab

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:
        url = job.requestUrl()
        path = url.path() or "/"
        if url.host() != "newtab":
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        # (refused with an empty or error answer: a failed job leaves the page's fetch() waiting for ever)
        if path == "/favicon" and self.ntp is not None:
            self._reply(job, b"image/png", self.ntp.favicon(job) or b"")
            return
        if path == "/api" and self.ntp is not None:
            answer = self.ntp.api(job)
            self._reply(job, b"application/json", json.dumps(answer or {"error": "Not allowed"}).encode("utf-8"))
            return
        if path != "/":
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        try:
            body = self.render_newtab().encode("utf-8")
        except Exception as exc:  # never leave the tab hanging
            log(f"New Tab page: {exc!r}")
            body = f"<!doctype html><title>New Tab</title><pre>{html.escape(str(exc))}</pre>".encode()
        self._reply(job, b"text/html", body)

    @staticmethod
    def _reply(job: QWebEngineUrlRequestJob, mime: bytes, data: bytes) -> None:
        try:  # never inside another page's frame
            job.setAdditionalResponseHeaders({QByteArray(b"X-Frame-Options"): [QByteArray(b"DENY")],
                                              QByteArray(b"Cache-Control"): [QByteArray(b"no-store")]})
        except (AttributeError, TypeError):
            pass
        buffer = QBuffer(job)  # owned by the job, so it lives exactly as long as the request
        buffer.setData(data)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(mime, buffer)


# The Google logo (Chrome shows it in colour, or in one colour in dark mode and with a colour theme)
GOOGLE_LOGO_SVG = """<svg class="glogo" viewBox="0 0 272 92" width="272" height="92" role="img" aria-label="Google">
<path fill="#EA4335" d="M115.75 47.18c0 12.77-9.99 22.18-22.25 22.18s-22.25-9.41-22.25-22.18C71.25 34.32 81.24 25 93.5 25s22.25 9.32 22.25 22.18zm-9.74 0c0-7.98-5.79-13.44-12.51-13.44S80.99 39.2 80.99 47.18c0 7.9 5.79 13.44 12.51 13.44s12.51-5.55 12.51-13.44z"/>
<path fill="#FBBC05" d="M163.75 47.18c0 12.77-9.99 22.18-22.25 22.18s-22.25-9.41-22.25-22.18c0-12.85 9.99-22.18 22.25-22.18s22.25 9.32 22.25 22.18zm-9.74 0c0-7.98-5.79-13.44-12.51-13.44s-12.51 5.46-12.51 13.44c0 7.9 5.79 13.44 12.51 13.44s12.51-5.55 12.51-13.44z"/>
<path fill="#4285F4" d="M209.75 26.34v39.82c0 16.38-9.66 23.07-21.08 23.07-10.75 0-17.22-7.19-19.66-13.07l8.48-3.53c1.51 3.61 5.21 7.87 11.17 7.87 7.31 0 11.84-4.51 11.84-13v-3.19h-.34c-2.18 2.69-6.38 5.04-11.68 5.04-11.09 0-21.25-9.66-21.25-22.09 0-12.52 10.16-22.26 21.25-22.26 5.29 0 9.49 2.35 11.68 4.96h.34v-3.61h9.25zm-8.56 20.92c0-7.81-5.21-13.52-11.84-13.52-6.72 0-12.35 5.71-12.35 13.52 0 7.73 5.63 13.36 12.35 13.36 6.63 0 11.84-5.63 11.84-13.36z"/>
<path fill="#34A853" d="M225 3v65h-9.5V3h9.5z"/>
<path fill="#EA4335" d="M262.02 54.48l7.56 5.04c-2.44 3.61-8.32 9.83-18.48 9.83-12.6 0-22.01-9.74-22.01-22.18 0-13.19 9.49-22.18 20.92-22.18 11.51 0 17.14 9.16 18.98 14.11l1.01 2.52-29.65 12.28c2.27 4.45 5.8 6.72 10.75 6.72 4.96 0 8.4-2.44 10.92-6.14zm-23.27-7.98l19.82-8.23c-1.09-2.77-4.37-4.7-8.23-4.7-4.95 0-11.84 4.37-11.59 12.93z"/>
<path fill="#4285F4" d="M35.29 41.41V32H67c.31 1.64.47 3.58.47 5.68 0 7.06-1.93 15.79-8.15 22.01-6.05 6.3-13.78 9.66-24.02 9.66C16.32 69.35.36 53.89.36 34.91.36 15.93 16.32.47 35.3.47c10.5 0 17.98 4.12 23.6 9.49l-6.64 6.64c-4.03-3.78-9.49-6.72-16.97-6.72-13.86 0-24.7 11.17-24.7 25.03 0 13.86 10.84 25.03 24.7 25.03 8.99 0 14.11-3.61 17.39-6.89 2.66-2.66 4.41-6.46 5.1-11.65l-22.49.01z"/>
</svg>"""
_G_ICON = ('<path fill="#4285F4" d="M45.12 24.5c0-1.56-.14-3.06-.4-4.5H24v8.51h11.84c-.51 2.75-2.06 5.08-4.39 6.64v5.52h7.11'
           'c4.16-3.83 6.56-9.47 6.56-16.17z"/><path fill="#34A853" d="M24 46c5.94 0 10.92-1.97 14.56-5.33l-7.11-5.52c-1.97 '
           '1.32-4.49 2.1-7.45 2.1-5.73 0-10.58-3.87-12.31-9.07H4.34v5.7C7.96 41.07 15.4 46 24 46z"/><path fill="#FBBC05" '
           'd="M11.69 28.18C11.25 26.86 11 25.45 11 24s.25-2.86.69-4.18v-5.7H4.34C2.85 17.09 2 20.45 2 24c0 3.55.85 6.91 '
           '2.34 9.88l7.35-5.7z"/><path fill="#EA4335" d="M24 10.75c3.23 0 6.13 1.11 8.41 3.29l6.31-6.31C34.91 4.18 29.93 2 '
           '24 2 15.4 2 7.96 6.93 4.34 14.12l7.35 5.7c1.73-5.2 6.58-9.07 12.31-9.07z"/>')
_PAGE = 'M12 4h17l9 9v29a2 2 0 0 1-2 2H12a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z'
_PETAL = '<path fill="{}" transform="rotate({} 24 24)" d="M24 24V5a9.5 9.5 0 0 1 0 19z"/>'
# The Google apps menu (the grid button at the top right): simplified drawings of each app's icon
NTP_APP_ICONS = {key: f'<svg viewBox="0 0 48 48" aria-hidden="true">{body}</svg>' for key, body in {
    "account": '<circle cx="24" cy="24" r="21" fill="#c2e7ff"/><circle cx="24" cy="19" r="7" fill="#0b57d0"/>'
               '<path fill="#0b57d0" d="M11 36.2c2.9-4.1 7.6-6.2 13-6.2s10.1 2.1 13 6.2A17 17 0 0 1 24 42a17 17 0 0 1-13-5.8z"/>',
    "search": _G_ICON,
    "maps": '<path fill="#34a853" d="M24 3C16.3 3 10 9.2 10 17c0 10.4 14 28 14 28s14-17.6 14-28C38 9.2 31.7 3 24 3z"/>'
            '<path fill="#4285f4" d="M36.2 10.2 24 22l7.6 9.9C35 26.6 38 21 38 17c0-2.5-.6-4.7-1.8-6.8z"/>'
            '<path fill="#ea4335" d="M24 3c-4.4 0-8.3 2-10.9 5.2L24 22l12.2-11.8C33.8 5.9 29.2 3 24 3z"/>'
            '<path fill="#fbbc04" d="M13.1 8.2A13.9 13.9 0 0 0 10 17c0 3.6 1.7 8 4 12.2L24 22z"/><circle cx="24" cy="17" r="5" fill="#fff"/>',
    "youtube": '<rect x="2" y="10" width="44" height="30" rx="9" fill="#ff0000"/><path fill="#fff" d="M19.5 18v14l12-7z"/>',
    "play": '<path fill="#4285f4" d="M9 5l16 19L9 43z"/><path fill="#34a853" d="M9 5l25 14-9 5z"/>'
            '<path fill="#ea4335" d="M9 43l16-19 9 5z"/><path fill="#fbbc04" d="M34 19l7 4a1.2 1.2 0 0 1 0 2l-7 4-9-5z"/>',
    "news": '<rect x="9" y="5" width="30" height="10" rx="2" fill="#34a853"/><rect x="7" y="8" width="34" height="8" rx="2" fill="#ea4335"/>'
            '<rect x="5" y="12" width="38" height="30" rx="3" fill="#4285f4"/><path fill="#fff" d="M10 19h15v3H10zm0 6h15v3H10zm0 6h11v3H10z"/>'
            '<rect x="29" y="19" width="9" height="15" rx="1" fill="#c2e7ff"/>',
    "gmail": '<path fill="#4285f4" d="M3 16.2l10 7.5V40H6a3 3 0 0 1-3-3z"/><path fill="#34a853" d="M45 16.2l-10 7.5V40h7a3 3 0 0 0 3-3z"/>'
             '<path fill="#ea4335" d="M35 11.2 24 19.45 13 11.2v12.5l11 8.25 11-8.25z"/>'
             '<path fill="#c5221f" d="M3 12.3v3.9l10 7.5V11.2L9.9 8.9A4.3 4.3 0 0 0 3 12.3z"/>'
             '<path fill="#fbbc04" d="M45 12.3v3.9l-10 7.5V11.2l3.1-2.3A4.3 4.3 0 0 1 45 12.3z"/>',
    "meet": '<path fill="#00ac47" d="M4 15a3 3 0 0 1 3-3h22a3 3 0 0 1 3 3v18a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3z"/>'
            '<path fill="#00832d" d="M32 20l10-7v22l-10-7z"/><path fill="#ea4335" d="M4 15a3 3 0 0 1 3-3h7v10H4z"/>'
            '<path fill="#2684fc" d="M14 12h15a3 3 0 0 1 3 3v7H14z"/><path fill="#ffba00" d="M4 26h10v10H7a3 3 0 0 1-3-3z"/>',
    "chat": '<path fill="#34a853" d="M8 6h32a4 4 0 0 1 4 4v22a4 4 0 0 1-4 4H18l-10 8V10a4 4 0 0 1 0-4z"/>'
            '<circle cx="16" cy="21" r="2.6" fill="#fff"/><circle cx="24" cy="21" r="2.6" fill="#fff"/><circle cx="32" cy="21" r="2.6" fill="#fff"/>',
    "contacts": '<circle cx="24" cy="24" r="21" fill="#1a73e8"/><circle cx="24" cy="19" r="6.5" fill="#fff"/>'
                '<path fill="#fff" d="M12 34c2.5-4 7-6 12-6s9.5 2 12 6a15 15 0 0 1-24 0z"/>',
    "drive": '<path fill="#00ac47" d="M16.6 6 3 29.6l7 12L23.6 18z"/><path fill="#ffba00" d="M16.6 6h14L45 29.6H31z"/>'
             '<path fill="#2684fc" d="M10 41.6h28l7-12H17z"/>',
    "calendar": '<rect x="6" y="6" width="36" height="36" rx="5" fill="#fff" stroke="#4285f4" stroke-width="4"/>'
                '<text x="24" y="31.5" text-anchor="middle" font-size="17" font-weight="700" fill="#4285f4" '
                'font-family="Arial, sans-serif">31</text>',
    "translate": '<rect x="16" y="16" width="28" height="28" rx="3" fill="#e3e3e3"/><rect x="4" y="4" width="28" height="28" rx="3" '
                 'fill="#4285f4"/><text x="18" y="25" fill="#fff" font-size="16" font-weight="700" text-anchor="middle" '
                 'font-family="Arial, sans-serif">G</text><text x="36" y="40" fill="#4285f4" font-size="12" text-anchor="middle" '
                 'font-family="sans-serif">文</text>',
    "photos": "".join(_PETAL.format(color, angle) for color, angle in
                      (("#ea4335", 0), ("#4285f4", 90), ("#34a853", 180), ("#fbbc04", 270))),
    "docs": f'<path fill="#4285f4" d="{_PAGE}"/><path fill="#a1c2fa" d="M29 4v9h9z"/>'
            '<path fill="#fff" d="M16 22h16v2.5H16zm0 5h16v2.5H16zm0 5h11v2.5H16z"/>',
    "sheets": f'<path fill="#0f9d58" d="{_PAGE}"/><path fill="#87ceac" d="M29 4v9h9z"/>'
              '<path fill="#fff" fill-rule="evenodd" d="M16 21h16v14H16zm2.5 2.5v3h4.2v-3zm6.8 0v3h4.2v-3zm-6.8 5.5v3.5h4.2V29zm6.8 0v3.5h4.2V29z"/>',
    "slides": f'<path fill="#f4b400" d="{_PAGE}"/><path fill="#fadb80" d="M29 4v9h9z"/>'
              '<path fill="#fff" fill-rule="evenodd" d="M15 21h18v13H15zm2.5 2.5v8h13v-8z"/>',
    "gemini": '<defs><linearGradient id="gem" x1="8" y1="40" x2="40" y2="8" gradientUnits="userSpaceOnUse">'
              '<stop offset="0" stop-color="#1c7df1"/><stop offset=".5" stop-color="#5684d1"/><stop offset="1" stop-color="#a87ffb"/>'
              '</linearGradient></defs><path fill="url(#gem)" d="M24 4c1.2 10.6 9.4 18.8 20 20-10.6 1.2-18.8 9.4-20 20-1.2-10.6-'
              '9.4-18.8-20-20 10.6-1.2 18.8-9.4 20-20z"/>',
}.items()}
NTP_APPS = (("account", "Account", "https://myaccount.google.com/"), ("search", "Search", "https://www.google.com/"),
            ("maps", "Maps", "https://maps.google.com/"), ("youtube", "YouTube", "https://www.youtube.com/"),
            ("play", "Play", "https://play.google.com/"), ("news", "News", "https://news.google.com/"),
            ("gmail", "Gmail", "https://mail.google.com/mail/"), ("meet", "Meet", "https://meet.google.com/"),
            ("chat", "Chat", "https://chat.google.com/"), ("contacts", "Contacts", "https://contacts.google.com/"),
            ("drive", "Drive", "https://drive.google.com/"), ("calendar", "Calendar", "https://calendar.google.com/"),
            ("translate", "Translate", "https://translate.google.com/"), ("photos", "Photos", "https://photos.google.com/"),
            ("docs", "Docs", "https://docs.google.com/document/"), ("sheets", "Sheets", "https://docs.google.com/spreadsheets/"),
            ("slides", "Slides", "https://docs.google.com/presentation/"), ("gemini", "Gemini", "https://gemini.google.com/"))

# Google Chrome's New Tab page, after Chromium's chrome/browser/resources/new_tab_page (app, logo, searchbox,
# most_visited, customize buttons): the same layout, sizes and colours.
NEWTAB_HTML = r"""<!doctype html>
<html lang="en" data-appearance="{{APPEARANCE}}" data-logo="{{LOGO_MODE}}"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-{{NONCE}}'; style-src 'unsafe-inline'; img-src foxglove: data:; connect-src foxglove:; base-uri 'none'; form-action 'none'">
<meta name="color-scheme" content="light dark"><title>New Tab</title>
<style id="theme">{{THEME_CSS}}</style>
<style>
* { box-sizing: border-box; }
html { height: 100%; }
body { margin: 0; min-height: 100%; min-width: fit-content; background: var(--bg); color: var(--text); overflow-x: hidden;
  font: 13px system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
button { font: inherit; color: inherit; }
svg { display: block; }
[hidden] { display: none !important; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.icon { width: 20px; height: 20px; fill: currentColor; }

/* the One Google Bar */
#ogb { position: absolute; top: 0; right: 0; height: 60px; display: flex; align-items: center; gap: 4px; padding: 0 16px 0 8px; z-index: 3; }
#ogb .link { color: var(--ogb-text); text-decoration: none; font-size: 13px; line-height: 24px; padding: 0 8px; }
#ogb .link:hover { text-decoration: underline; }
.round { width: 40px; height: 40px; border: 0; border-radius: 50%; background: none; padding: 0; cursor: pointer;
  display: grid; place-items: center; color: var(--icon); text-decoration: none; }
.round:hover, .round[aria-expanded=true] { background: var(--ogb-hover); }
.round .icon { width: 24px; height: 24px; }
#avatar span { width: 32px; height: 32px; border-radius: 50%; background: var(--avatar-bg); color: var(--avatar-fg); display: grid; place-items: center; }
#avatar .icon { width: 22px; height: 22px; }
#apps-menu { position: fixed; top: 60px; right: 12px; width: 328px; max-height: calc(100vh - 76px); overflow-y: auto; z-index: 20;
  background: var(--apps-bg); border-radius: 28px; padding: 8px; box-shadow: var(--menu-shadow); }
.apps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; background: var(--apps-card); border-radius: 24px; padding: 16px 12px; }
.app { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 10px 4px 8px; border-radius: 16px; color: var(--text);
  text-decoration: none; font-size: 14px; }
.app:hover { background: var(--menu-hover); }
.app svg { width: 40px; height: 40px; }

/* logo, search box, shortcuts */
#content { display: flex; flex-direction: column; align-items: center; padding-top: 56px; min-height: 100vh; position: relative; z-index: 1; }
#logo { flex-shrink: 0; min-height: 168px; display: flex; flex-direction: column; justify-content: flex-end; margin-bottom: 38px; user-select: none; }
#logo svg { width: 272px; height: 92px; }
:root[data-logo=single] .glogo path, :root[data-appearance=dark] .glogo path { fill: var(--logo); }
@media (prefers-color-scheme: dark) { :root[data-appearance=system] .glogo path { fill: var(--logo); } }
.wordmark { font-size: 60px; font-weight: 500; letter-spacing: -1px; line-height: 92px; color: var(--logo-text); }
#searchbox { --box-width: 337px; position: relative; width: var(--box-width); margin: 0 0 16px; }
@media (min-width: 560px) { #searchbox { --box-width: 449px; } }
@media (min-width: 672px) { #searchbox { --box-width: 561px; } }
#q { display: block; width: 100%; height: 48px; border: 0; border-radius: 24px; outline: none; background: var(--box-bg); color: var(--text);
  box-shadow: var(--box-shadow); font: inherit; font-size: 16px; padding: 0 96px 0 52px; }
#q:hover { background: var(--box-hover); box-shadow: var(--box-shadow-hover); }
#q::placeholder { color: var(--placeholder); opacity: 1; }
#searchbox .search-icon { position: absolute; left: 16px; top: 14px; width: 20px; height: 20px; fill: var(--icon); pointer-events: none; }
.box-buttons { position: absolute; right: 8px; top: 4px; display: flex; }
.box-buttons .round { width: 40px; height: 40px; }
.box-buttons .round:hover { background: var(--tile-hover); }
.box-buttons svg { width: 24px; height: 24px; }
#tiles { --columns: 5; display: grid; grid-template-columns: repeat(var(--columns), 112px); justify-content: center; margin-top: 8px; }
.tile { position: relative; width: 112px; height: 112px; border-radius: 4px; display: flex; flex-direction: column; align-items: center;
  color: var(--tile-text); text-decoration: none; cursor: pointer; border: 0; background: none; padding: 0; user-select: none; outline-offset: -2px; }
.tile:hover, .tile:focus-visible { background: var(--tile-hover); }
.tile-icon { margin-top: 16px; width: 48px; height: 48px; flex-shrink: 0; border-radius: 50%; background: var(--tile-bg); display: grid; place-items: center; }
.tile-icon img { width: 24px; height: 24px; }
.tile-icon .icon { width: 24px; height: 24px; }
.monogram { font-size: 18px; font-weight: 500; color: var(--icon); text-transform: uppercase; }
.tile-title { margin-top: 6px; width: 88px; height: 28px; padding: 2px 8px; display: flex; align-items: center; }
.tile-title span { width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: center; font-size: 13px; line-height: 16px; }
.tile-action { position: absolute; top: 4px; right: 4px; width: 28px; height: 28px; border: 0; border-radius: 50%; background: none; padding: 0;
  display: grid; place-items: center; color: var(--icon); cursor: pointer; opacity: 0; }
.tile:hover .tile-action, .tile:focus-within .tile-action, .tile-action:focus-visible { opacity: 1; }
.tile-action:hover { background: var(--tile-hover); }
.tile-action .icon { width: 16px; height: 16px; }
.tile.dragging { opacity: .4; }

/* Customize Chrome */
#customize { position: fixed; right: 16px; bottom: 16px; z-index: 2; height: 32px; border: 0; border-radius: 16px; padding: 0 16px 0 12px;
  display: flex; align-items: center; gap: 8px; background: var(--tonal); color: var(--on-tonal); font-weight: 500; cursor: pointer;
  box-shadow: 0 1px 2px 0 rgba(0,0,0,.3), 0 1px 3px 1px rgba(0,0,0,.15); }
#customize:hover { background-image: linear-gradient(rgba(127,127,127,.12), rgba(127,127,127,.12)); }
#customize .icon { width: 16px; height: 16px; }
#panel { position: fixed; top: 0; right: 0; bottom: 0; width: 360px; max-width: 100vw; z-index: 10; overflow-y: auto; background: var(--panel-bg);
  border-left: 1px solid var(--divider); box-shadow: -2px 0 6px rgba(0,0,0,.12); transform: translateX(105%); transition: transform .2s ease;
  visibility: hidden; padding-bottom: 24px; }
#panel.open { transform: none; visibility: visible; }
.panel-head { display: flex; align-items: center; justify-content: space-between; height: 56px; padding: 0 8px 0 20px; }
.panel-head h2 { margin: 0; font-size: 15px; font-weight: 500; }
.panel-head .round { color: var(--text-2); }
.card { margin: 0 12px 12px; padding: 16px; border-radius: 12px; background: var(--card-bg); }
.card h3 { margin: 0 0 14px; font-size: 13px; font-weight: 500; }
.chips { display: grid; grid-template-columns: repeat(6, 40px); gap: 12px; justify-content: space-between; }
.chip { width: 40px; height: 40px; border-radius: 50%; border: 1px solid var(--divider); cursor: pointer; padding: 0;
  background: linear-gradient(135deg, var(--chip) 50%, color-mix(in srgb, var(--chip) 30%, #fff) 50%); }
.chip.default { --chip: #1a73e8; background: linear-gradient(135deg, #e8f0fe 50%, #1a73e8 50%); }
.chip[aria-pressed=true] { outline: 2px solid var(--primary); outline-offset: 2px; }
.row { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 40px; cursor: pointer; }
.switch { appearance: none; width: 32px; height: 18px; border-radius: 9px; background: var(--outline); position: relative; cursor: pointer; margin: 0; flex-shrink: 0; transition: background .15s; }
.switch::after { content: ""; position: absolute; top: 3px; left: 3px; width: 12px; height: 12px; border-radius: 50%; background: var(--bg); transition: left .15s; }
.switch:checked { background: var(--primary); }
.switch:checked::after { left: 17px; background: var(--on-primary); }
.radio { display: flex; gap: 12px; padding: 10px 0; cursor: pointer; align-items: flex-start; }
.radio input { margin: 2px 0 0; accent-color: var(--primary); width: 16px; height: 16px; }
.radio b { display: block; font-weight: 400; }
.radio small { display: block; color: var(--text-2); font-size: 12px; margin-top: 2px; }
.radios[aria-disabled=true] { opacity: .45; pointer-events: none; }

/* menus, dialogs, toast */
.menu { position: fixed; z-index: 20; min-width: 160px; padding: 8px 0; background: var(--menu-bg); border-radius: 4px; box-shadow: var(--menu-shadow); }
.menu button { display: block; width: 100%; height: 32px; padding: 0 24px; border: 0; background: none; text-align: left; cursor: pointer; color: var(--text); }
.menu button:hover, .menu button:focus-visible { background: var(--menu-hover); outline: none; }
dialog { width: 320px; border: 0; border-radius: 8px; padding: 0; background: var(--dialog-bg); color: var(--text);
  box-shadow: 0 1px 3px 0 rgba(0,0,0,.3), 0 4px 8px 3px rgba(0,0,0,.15); }
dialog::backdrop { background: var(--scrim); }
dialog h2 { margin: 0; padding: 20px 20px 16px; font-size: 15px; font-weight: 400; }
.field { padding: 0 20px 12px; }
.field label { display: block; margin-bottom: 4px; color: var(--text-2); font-size: 12px; }
.field input { width: 100%; height: 32px; border: 0; border-radius: 4px; padding: 0 8px; background: var(--field-bg); color: var(--text); font: inherit; outline: none; }
.field input:focus { box-shadow: inset 0 -2px 0 var(--focus); }
.error { min-height: 16px; margin-top: 4px; color: var(--error); font-size: 12px; }
.buttons { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 20px 20px; }
.btn { height: 32px; padding: 0 16px; border: 1px solid var(--outline); border-radius: 16px; background: none; color: var(--link); font-weight: 500; cursor: pointer; }
.btn.action { background: var(--primary); border-color: transparent; color: var(--on-primary); }
.btn:disabled { opacity: .38; cursor: default; }
#toast { position: fixed; left: 24px; bottom: 24px; z-index: 30; min-height: 48px; max-width: calc(100vw - 48px); display: flex; align-items: center;
  gap: 16px; padding: 6px 8px 6px 16px; border-radius: 8px; background: var(--toast-bg); color: var(--toast-text);
  box-shadow: 0 1px 3px 0 rgba(0,0,0,.3), 0 4px 8px 3px rgba(0,0,0,.15); }
#toast span { flex: 1; }
#toast button { height: 32px; padding: 0 12px; border: 0; border-radius: 16px; background: none; color: var(--toast-link); font-weight: 500; cursor: pointer; }
#toast button:hover { background: rgba(127,127,127,.18); }
</style></head>
<body>
<header id="ogb" {{OGB}}>
  <a class="link" href="https://mail.google.com/mail/&amp;ogbl">Gmail</a>
  <a class="link" href="https://www.google.com/imghp?hl=en&amp;ogbl">Images</a>
  <button id="apps" class="round" type="button" title="Google apps" aria-label="Google apps" aria-expanded="false" aria-haspopup="true">
    <svg class="icon" viewBox="0 0 24 24"><path d="M6 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm6 12c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm-6 0c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0-6c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm6 0c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm4-8c0 1.1.9 2 2 2s2-.9 2-2-.9-2-2-2-2 .9-2 2zm-4 2c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm6 6c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 6c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2z"/></svg>
  </button>
  <a id="avatar" class="round" href="https://accounts.google.com/" title="Google Account" aria-label="Google Account"><span>
    <svg class="icon" viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
  </span></a>
</header>
<div id="apps-menu" role="menu" hidden><div class="apps">{{APPS}}</div></div>
<main id="content">
  <div id="logo">{{LOGO}}</div>
  <form id="searchbox" role="search" autocomplete="off">
    <svg class="search-icon" viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
    <input id="q" type="search" placeholder="{{PLACEHOLDER}}" aria-label="{{PLACEHOLDER}}" spellcheck="false" autocomplete="off" maxlength="2048">
    <div class="box-buttons" {{OGB}}>
      <button id="voice" class="round" type="button" title="Search by voice" aria-label="Search by voice"><svg viewBox="0 0 24 24">
        <path fill="#4285f4" d="M12 15c1.66 0 3-1.31 3-2.97V5.01C15 3.35 13.66 2 12 2S9 3.34 9 5.01v7.02C9 13.69 10.34 15 12 15z"/>
        <path fill="#34a853" d="M11 18.08h2V22h-2z"/>
        <path fill="#fbbc04" d="M7.05 16.87C5.78 15.54 5 14.04 5 12h2c0 1.45.56 2.42 1.47 3.38v.32l-1.15 1.18z"/>
        <path fill="#ea4335" d="M12 16.93a4.97 5.25 0 0 1-3.54-1.55l-1.41 1.49C8.31 18.21 10.07 19 12 19c3.87 0 6.99-2.92 6.99-7H17c0 2.92-2.24 4.93-5 4.93z"/>
      </svg></button>
      <a id="lens" class="round" href="https://lens.google.com/" title="Search by image" aria-label="Search by image"><svg viewBox="0 0 24 24">
        <path fill="#ea4335" d="M3 7.5V7a4 4 0 0 1 4-4h1.5v2.2H7A1.8 1.8 0 0 0 5.2 7v.5z"/>
        <path fill="#fbbc04" d="M3 16.5V17a4 4 0 0 0 4 4h1.5v-2.2H7A1.8 1.8 0 0 1 5.2 17v-.5z"/>
        <path fill="#4285f4" d="M21 7.5V7a4 4 0 0 0-4-4h-1.5v2.2H17A1.8 1.8 0 0 1 18.8 7v.5z"/>
        <circle cx="12" cy="12" r="3.3" fill="none" stroke="#4285f4" stroke-width="2.2"/>
        <circle cx="18.2" cy="18.2" r="2.1" fill="#34a853"/>
      </svg></a>
    </div>
  </form>
  <div id="tiles" role="list" aria-label="Shortcuts"></div>
</main>
<button id="customize" type="button" title="Customize this page" aria-controls="panel" aria-expanded="false">
  <svg class="icon" viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
  <span>Customize Chrome</span>
</button>
<aside id="panel" aria-label="Customize Chrome">
  <div class="panel-head"><h2>Customize Chrome</h2>
    <button id="panel-close" class="round" type="button" title="Close" aria-label="Close"><svg class="icon" viewBox="0 0 24 24"><path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg></button>
  </div>
  <section class="card"><h3>Appearance</h3>
    <div class="chips"><button type="button" class="chip default" data-theme="" title="Default" aria-label="Default"></button>{{COLORS}}</div>
  </section>
  <section class="card"><h3>Shortcuts</h3>
    <label class="row"><span>Show shortcuts</span><input id="show" class="switch" type="checkbox" role="switch"></label>
    <div class="radios" id="modes">
      <label class="radio"><input type="radio" name="mode" value="custom"><span><b>My shortcuts</b><small>Shortcuts are curated by you</small></span></label>
      <label class="radio"><input type="radio" name="mode" value="most_visited"><span><b>Most visited sites</b><small>Shortcuts are suggested based on websites you visit often</small></span></label>
    </div>
  </section>
</aside>
<div id="tile-menu" class="menu" role="menu" hidden>
  <button id="menu-edit" type="button" role="menuitem">Edit shortcut</button>
  <button id="menu-remove" type="button" role="menuitem">Remove</button>
</div>
<dialog id="edit">
  <form method="dialog" id="edit-form">
    <h2 id="edit-title">Add shortcut</h2>
    <div class="field"><label for="name">Name</label><input id="name" maxlength="200" spellcheck="false"></div>
    <div class="field"><label for="url">URL</label><input id="url" maxlength="2048" spellcheck="false"><div id="url-error" class="error"></div></div>
    <div class="buttons"><button id="cancel" class="btn" type="button">Cancel</button><button id="done" class="btn action" type="submit">Done</button></div>
  </form>
</dialog>
<div id="toast" role="status" hidden><span id="toast-text"></span><button id="toast-undo" type="button">Undo</button><button id="toast-restore" type="button">Restore all</button></div>
<template id="i-more"><svg class="icon" viewBox="0 0 24 24"><path d="M12 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z"/></svg></template>
<template id="i-close"><svg class="icon" viewBox="0 0 24 24"><path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg></template>
<template id="i-add"><svg class="icon" viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg></template>
<script type="application/json" id="state">{{STATE}}</script>
<script nonce="{{NONCE}}">
(() => {
"use strict";
const $ = (id) => document.getElementById(id);
const S = JSON.parse($("state").textContent);
const tiles = $("tiles"), q = $("q"), menu = $("tile-menu"), dialog = $("edit"), panel = $("panel"), apps = $("apps-menu");
let menuIndex = -1, editing = -1, dragFrom = -1, undo = null, toastTimer = 0;

async function api(body) {
  const response = await fetch("/api", {method: "POST", body: JSON.stringify(body),
    headers: {"Content-Type": "application/json", "X-NTP-Token": S.token}});
  const answer = await response.json();
  if (answer.error) throw new Error(answer.error);
  return answer;
}
async function change(body) { const state = await api(body); Object.assign(S, state); render(); return state; }
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };
const icon = (name) => $("i-" + name).content.firstElementChild.cloneNode(true);
const nameOf = (t) => t.title || (() => { try { return new URL(t.url).hostname.replace(/^www\./, ""); } catch { return t.url; } })();
const copy = () => S.tiles.map((t) => ({title: t.title, url: t.url}));

function monogram(name) { return el("span", "monogram", (name.replace(/^www\./, "")[0] || "?")); }

function tile(t, index) {
  const a = el("a", "tile"), name = nameOf(t);
  a.href = t.url; a.title = name; a.setAttribute("role", "listitem"); a.draggable = S.mode === "custom";
  const iconBox = el("div", "tile-icon");
  if (t.icon) {
    const img = new Image(); img.alt = ""; img.draggable = false; img.src = t.icon;
    img.addEventListener("error", () => img.replaceWith(monogram(name)));
    iconBox.append(img);
  } else iconBox.append(monogram(name));
  const title = el("div", "tile-title"); title.append(el("span", "", name));
  const action = el("button", "tile-action"); action.type = "button";
  if (S.mode === "custom") {
    action.title = action.ariaLabel = `More actions for ${name} shortcut`; action.append(icon("more"));
    action.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); openMenu(index, action); });
  } else {
    action.title = action.ariaLabel = "Don't show on this page"; action.append(icon("close"));
    action.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); hideSite(t); });
  }
  a.append(iconBox, title, action);
  if (S.mode === "custom") {
    a.addEventListener("dragstart", (e) => { dragFrom = index; a.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/uri-list", t.url); });
    a.addEventListener("dragend", () => { a.classList.remove("dragging"); dragFrom = -1; });
    a.addEventListener("dragover", (e) => { if (dragFrom >= 0) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; } });
    a.addEventListener("drop", (e) => {
      e.preventDefault();
      const from = dragFrom; dragFrom = -1;
      if (from >= 0 && from !== index) change({action: "move", index: from, to: index}).catch(() => {});
    });
  }
  return a;
}

function addTile() {
  const b = el("button", "tile add"); b.type = "button"; b.title = "Add shortcut"; b.setAttribute("role", "listitem");
  const iconBox = el("div", "tile-icon"); iconBox.append(icon("add"));
  const title = el("div", "tile-title"); title.append(el("span", "", "Add shortcut"));
  b.append(iconBox, title);
  b.addEventListener("click", () => openDialog(-1));
  return b;
}

function render() {
  $("theme").textContent = S.theme_css;
  document.documentElement.dataset.logo = S.theme ? "single" : "color";
  tiles.hidden = !S.show;
  tiles.replaceChildren(...S.tiles.map(tile));
  if (S.mode === "custom" && S.tiles.length < S.max) tiles.append(addTile());
  const n = tiles.children.length;
  tiles.style.setProperty("--columns", n <= 5 ? Math.max(n, 1) : Math.ceil(n / 2));
  $("show").checked = S.show;
  $("modes").setAttribute("aria-disabled", String(!S.show));
  for (const radio of document.querySelectorAll("input[name=mode]")) radio.checked = radio.value === S.mode;
  for (const chip of document.querySelectorAll(".chip")) chip.setAttribute("aria-pressed", String(chip.dataset.theme === S.theme));
}

// ── the search box: what the address bar would do with the text ──
$("searchbox").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = q.value.trim();
  if (!text) return;
  try { const {url} = await api({action: "navigate", text}); if (url) location.href = url; } catch (err) { showToast(err.message); }
});
$("voice").addEventListener("click", () => { q.focus(); showToast("Voice search isn't available in this browser"); });

// ── shortcut menu, dialog, toast ──
function closeMenus() {
  menu.hidden = true; apps.hidden = true; $("apps").setAttribute("aria-expanded", "false");
}
function openMenu(index, anchor) {
  closeMenus();
  menuIndex = index;
  const r = anchor.getBoundingClientRect();
  menu.hidden = false;
  menu.style.left = Math.max(8, Math.min(r.left, innerWidth - menu.offsetWidth - 8)) + "px";
  menu.style.top = Math.min(r.bottom + 4, innerHeight - menu.offsetHeight - 8) + "px";
  $("menu-edit").focus();
}
$("menu-edit").addEventListener("click", () => { closeMenus(); openDialog(menuIndex); });
$("menu-remove").addEventListener("click", async () => {
  closeMenus();
  const before = copy();
  try { await change({action: "remove", index: menuIndex}); showToast("Shortcut removed", () => change({action: "set", items: before})); }
  catch (err) { showToast(err.message); }
});
function openDialog(index) {
  editing = index;
  const t = index >= 0 ? S.tiles[index] : {title: "", url: ""};
  $("edit-title").textContent = index >= 0 ? "Edit shortcut" : "Add shortcut";
  $("name").value = index >= 0 ? nameOf(t) : ""; $("url").value = t.url; $("url-error").textContent = "";
  $("done").disabled = !$("url").value.trim();
  dialog.showModal(); $("name").focus();
}
$("url").addEventListener("input", () => { $("done").disabled = !$("url").value.trim(); $("url-error").textContent = ""; });
$("cancel").addEventListener("click", () => dialog.close());
$("edit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const before = copy(), adding = editing < 0;
  try {
    await change({action: adding ? "add" : "edit", index: editing, title: $("name").value, url: $("url").value});
  } catch (err) { $("url-error").textContent = err.message; return; }
  dialog.close();
  showToast(adding ? "Shortcut added" : "Shortcut edited", () => change({action: "set", items: before}));
});
async function hideSite(t) {
  try {
    await change({action: "hide", url: t.url});
    showToast("Shortcut removed", () => change({action: "unhide", url: t.url}), () => change({action: "unhide_all"}));
  } catch (err) { showToast(err.message); }
}
function showToast(text, onUndo, onRestore) {
  $("toast-text").textContent = text;
  undo = onUndo || null;
  $("toast-undo").hidden = !onUndo; $("toast-restore").hidden = !onRestore;
  $("toast-restore").onclick = onRestore ? () => { hideToast(); onRestore().catch(() => {}); } : null;
  $("toast").hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(hideToast, 10000);
}
function hideToast() { $("toast").hidden = true; undo = null; }
function doUndo() { const action = undo; hideToast(); if (action) action().catch((err) => showToast(err.message)); }
$("toast-undo").addEventListener("click", doUndo);

// ── Google apps, Customize Chrome ──
$("apps").addEventListener("click", (e) => {
  e.stopPropagation();
  const open = apps.hidden;
  closeMenus(); apps.hidden = !open; $("apps").setAttribute("aria-expanded", String(open));
});
function setPanel(open) {
  panel.classList.toggle("open", open); $("customize").setAttribute("aria-expanded", String(open));
  if (open) $("panel-close").focus();
}
$("customize").addEventListener("click", (e) => { e.stopPropagation(); setPanel(!panel.classList.contains("open")); });
$("panel-close").addEventListener("click", () => setPanel(false));
$("show").addEventListener("change", () => change({action: "prefs", show: $("show").checked}).catch(() => {}));
for (const radio of document.querySelectorAll("input[name=mode]"))
  radio.addEventListener("change", () => change({action: "prefs", mode: radio.value}).catch(() => {}));
for (const chip of document.querySelectorAll(".chip"))
  chip.addEventListener("click", () => change({action: "prefs", theme: chip.dataset.theme}).catch(() => {}));

document.addEventListener("click", (e) => {
  if (!menu.contains(e.target) && !apps.contains(e.target)) closeMenus();
  if (panel.classList.contains("open") && !panel.contains(e.target) && !dialog.open) setPanel(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeMenus(); setPanel(false); }
  const typing = /^(INPUT|TEXTAREA)$/.test((document.activeElement || {}).tagName || "");
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "z" && undo && !typing) { e.preventDefault(); doUndo(); }
});
render();
})();
</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Web pages and views
# ══════════════════════════════════════════════════════════════════════════════════════════
class WebPage(QWebEnginePage):
    """A page that asks before accepting bad certificates and handles HTTP logins."""

    certificateAccepted = pyqtSignal(str)  # host the user chose to trust despite a certificate problem
    certificateProblem = pyqtSignal(object)  # a deferred QWebEngineCertificateError for the tab to ask about

    def __init__(self, profile: QWebEngineProfile, parent: QObject):
        super().__init__(profile, parent)
        self.certificateError.connect(self._on_certificate_error)
        self.authenticationRequired.connect(self._on_authentication)
        self.proxyAuthenticationRequired.connect(lambda url, auth, host: self._on_authentication(url, auth, proxy=host))

    def javaScriptConsoleMessage(self, level, message, line, source) -> None:  # keep the terminal quiet
        if VERBOSE:
            log(f"console {source}:{line}: {message}")

    def _dialog_parent(self) -> QWidget | None:
        view = QWebEngineView.forPage(self)
        return view.window() if view is not None else QApplication.activeWindow()

    def _on_certificate_error(self, error) -> None:
        error = QWebEngineCertificateError(error)  # PyQt's argument dies with this call; keep a real copy
        if not error.isOverridable() or not error.isMainFrame():
            error.rejectCertificate()  # never offer to bypass HSTS-protected sites or broken sub-resources
            return
        error.defer()
        if self.receivers(self.certificateProblem) > 0:
            self.certificateProblem.emit(error)  # tabs ask with a notification bar (non-blocking, like Firefox)
        else:
            QTimer.singleShot(0, lambda: self._ask_certificate(error))

    def _ask_certificate(self, error) -> None:
        try:
            host = error.url().host() or error.url().toString()
            box = QMessageBox(self._dialog_parent())
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Warning: Potential Security Risk Ahead")
            box.setText(f"<b>{html.escape(host)}</b> has a security problem, so {APP_NAME} stopped loading it.")
            box.setInformativeText(f"{html.escape(error.description())}<br><br>Attackers might be trying to steal your "
                                   "information (passwords, messages or credit cards). Only continue if you trust this network.")
            back = box.addButton("Go Back (Recommended)", QMessageBox.ButtonRole.RejectRole)
            accept = box.addButton("Accept the Risk and Continue", QMessageBox.ButtonRole.DestructiveRole)
            box.setDefaultButton(back)
            box.exec()
            box.deleteLater()
            if box.clickedButton() is accept:
                self.certificateAccepted.emit(host)
                error.acceptCertificate()
            else:
                error.rejectCertificate()
        except RuntimeError:  # the page went away while the dialog was open
            pass

    def _on_authentication(self, url: QUrl, authenticator: QAuthenticator, proxy: str = "") -> None:
        dialog = QDialog(self._dialog_parent())
        dialog.setWindowTitle("Authentication Required")
        layout = QVBoxLayout(dialog)
        where = proxy or url.host() or url.toString()
        realm = authenticator.realm()
        text = f"<b>{html.escape(where)}</b> is requesting your username and password."
        if realm:
            text += f"<br>The site says: “{html.escape(realm)}”"
        layout.addWidget(tone_label(text, wrap=True, rich=True))
        form = QFormLayout()
        user, password = QLineEdit(), QLineEdit()
        password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Username", user)
        form.addRow("Password", password)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Sign in")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(420, dialog.sizeHint().height())
        if run_dialog(dialog):
            authenticator.setUser(user.text())
            authenticator.setPassword(password.text())
        else:
            try:
                sip.assign(authenticator, QAuthenticator())  # a null authenticator cancels the login
            except (AttributeError, TypeError):
                pass


class WebView(QWebEngineView):
    def __init__(self, tab: "Tab"):
        super().__init__()
        self.tab = tab

    def contextMenuEvent(self, event) -> None:
        self.tab.win.show_page_context_menu(self.tab, event.globalPos())


class Tab(QWidget):
    """One browser tab: the web view, optional developer tools and the tab's notification bars."""

    def __init__(self, win: "BrowserWindow"):
        super().__init__()
        self.win = win
        self.view = WebView(self)
        self.page = WebPage(win.profile, self.view)
        self.view.setPage(self.page)
        self.tab_id, self.window_id = next(_TAB_IDS), MAIN_WINDOW_ID  # what chrome.tabs calls this tab
        win.extensions.wire_tab(self.page, self.tab_id)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.view)
        self.bars = QVBoxLayout()
        self.bars.setContentsMargins(0, 0, 0, 0)
        self.bars.setSpacing(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(self.bars)
        layout.addWidget(self.splitter, 1)
        self.devtools: QWebEngineView | None = None
        self.pending: dict | None = None      # saved state of a restored tab that hasn't been opened yet
        self.loading = False
        self.progress = 0
        self.crashed = False
        self.close_requested = False
        self.opener_ref = None                # weakref to the tab that opened this one
        self.return_to_opener = False
        self.https_fallback: QUrl | None = None
        self.typed_text = ""                  # what was typed, in case the guessed address doesn't exist
        self.back_after_error = ""            # URL whose certificate error page we should step back from
        self.last_recorded = ""
        self.webstore_bar: InfoBar | None = None
        self.crash_bar: InfoBar | None = None
        self.permission_bars: list[InfoBar] = []

    # State
    @property
    def opener(self) -> "Tab | None":
        return self.opener_ref() if self.opener_ref is not None else None

    def url(self) -> QUrl:
        if self.pending is not None:
            return QUrl(self.pending.get("url") or NEWTAB)
        url = self.page.url()
        return url if not url.isEmpty() else self.page.requestedUrl()

    def title(self) -> str:
        if self.pending is not None:
            return self.pending.get("title") or display_url(QUrl(self.pending.get("url") or "")) or "New Tab"
        url = self.url()
        if is_newtab(url) or (url.isEmpty() and not self.loading):
            return "New Tab"
        return self.page.title() or display_url(url) or "Untitled"

    def favicon(self) -> QIcon:
        url = self.url()
        if is_newtab(url):
            return icons().logo()
        if self.pending is None and not self.page.icon().isNull():
            return self.page.icon()
        if url.scheme() in ("http", "https"):
            return self.win.favicons.get(url)
        return icon("globe", P.TEXT_3)

    def audio_state(self) -> str:
        if self.pending is not None:
            return "muted" if self.pending.get("muted") else ""
        if self.page.isAudioMuted():
            return "muted"
        return "playing" if self.page.recentlyAudible() else ""

    # Loading
    def load(self, url: QUrl) -> None:
        self.ensure_loaded(navigate=False)
        self.page.load(url)

    def ensure_loaded(self, navigate: bool = True) -> None:
        if self.pending is None:
            return
        entry, self.pending = self.pending, None
        if entry.get("muted"):
            self.page.setAudioMuted(True)
        if not navigate:
            return
        restored = False
        blob = entry.get("history")
        if isinstance(blob, str) and blob:
            try:
                data = QByteArray.fromBase64(blob.encode("ascii"))  # keep a reference: the stream only points at it
                stream = QDataStream(data, QIODevice.OpenModeFlag.ReadOnly)
                stream >> self.page.history()
                restored = stream.status() == QDataStream.Status.Ok and self.page.history().count() > 0
            except (TypeError, ValueError, UnicodeError):
                restored = False
        if not restored:
            url = QUrl(entry.get("url") or "")
            self.page.load(url if url.isValid() and not url.isEmpty() else QUrl(NEWTAB))

    def session_entry(self) -> dict:
        if self.pending is not None:
            return dict(self.pending)
        url = self.url()
        entry: dict = {"url": url.toString() or NEWTAB, "title": self.title()}
        data = QByteArray()
        stream = QDataStream(data, QIODevice.OpenModeFlag.WriteOnly)
        stream << self.page.history()
        if not data.isEmpty() and self.page.history().count() > 0:
            entry["history"] = bytes(data.toBase64()).decode("ascii")
        if self.page.isAudioMuted():
            entry["muted"] = True
        return entry

    # Notification bars
    def add_bar(self, bar: "InfoBar") -> None:
        self.bars.addWidget(bar)
        bar.show()

    # Developer tools
    def toggle_devtools(self) -> None:
        if self.devtools is None:
            self.open_devtools()
        else:
            self.close_devtools()

    def open_devtools(self, inspect: bool = False) -> None:
        self.ensure_loaded()
        if self.devtools is None:
            self.devtools = QWebEngineView()
            devtools_page = QWebEnginePage(self.win.profile, self.devtools)
            self.devtools.setPage(devtools_page)
            self.page.setDevToolsPage(devtools_page)
            devtools_page.windowCloseRequested.connect(self.close_devtools)
            self.splitter.addWidget(self.devtools)
            total = max(400, self.splitter.height())
            self.splitter.setSizes([int(total * 0.62), int(total * 0.38)])
        if inspect:
            self.page.triggerAction(QWebEnginePage.WebAction.InspectElement)

    def close_devtools(self) -> None:
        if self.devtools is not None:
            self.page.setDevToolsPage(None)
            self.devtools.hide()
            self.devtools.deleteLater()
            self.devtools = None

    def shutdown(self) -> None:
        self.page.blockSignals(True)
        self.view.blockSignals(True)
        if self.devtools is not None:
            self.devtools.page().blockSignals(True)
            self.page.setDevToolsPage(None)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Tab strip
# ══════════════════════════════════════════════════════════════════════════════════════════
class Throbber(QObject):
    """Drives the spinning loading indicator shared by every loading tab."""

    def __init__(self) -> None:
        super().__init__()
        self.phase = 0.0
        self.widgets: set[QWidget] = set()
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)

    def watch(self, widget: QWidget, active: bool) -> None:
        if active:
            self.widgets.add(widget)
        else:
            self.widgets.discard(widget)
        if self.widgets and not self.timer.isActive():
            self.timer.start()
        elif not self.widgets:
            self.timer.stop()

    def _tick(self) -> None:
        self.phase = (self.phase + 0.033 / 0.9) % 1.0
        for widget in list(self.widgets):
            if sip.isdeleted(widget):
                self.widgets.discard(widget)
            else:
                widget.update()

    def paint(self, painter: QPainter, rect: QRectF) -> None:
        ring = rect.adjusted(1.5, 1.5, -1.5, -1.5)
        track = QColor(P.ACCENT)
        track.setAlphaF(0.22)
        painter.setPen(QPen(track, 2.0))
        painter.drawEllipse(ring)
        pen = QPen(QColor(P.ACCENT), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(ring, int(-self.phase * 360 * 16), -110 * 16)


THROBBER: Throbber | None = None


class TabLabel(QWidget):
    """Favicon + left-aligned title drawn inside a tab (QTabBar can only centre its own text)."""

    HEIGHT = 24
    ICON_X = 12
    TEXT_X = 36

    def __init__(self, bar: QTabBar):
        super().__init__(bar)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.title = "New Tab"
        self.icon = QIcon()
        self.loading = False
        self.audio = ""
        self.resize(120, self.HEIGHT)

    def set_state(self, title: str, icon_: QIcon, loading: bool, audio: str) -> None:
        self.title, self.icon, self.audio = title, icon_, audio
        if loading != self.loading:
            self.loading = loading
            THROBBER.watch(self, loading)
        self.update()

    def audio_rect(self) -> QRect:
        if not self.audio:
            return QRect()
        return QRect(self.width() - 18, (self.HEIGHT - 16) // 2, 16, 16)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        icon_rect = QRect(self.ICON_X, (self.HEIGHT - 16) // 2, 16, 16)
        if self.loading:
            THROBBER.paint(painter, QRectF(icon_rect))
        elif not self.icon.isNull():
            self.icon.paint(painter, icon_rect)
        right = self.width()
        audio = self.audio_rect()
        if not audio.isNull():
            icon("speaker-muted" if self.audio == "muted" else "speaker", P.TEXT_2).paint(painter, audio)
            right = audio.left() - 4
        text_rect = QRect(self.TEXT_X, 0, max(0, right - self.TEXT_X), self.HEIGHT)
        if text_rect.width() >= 28:  # narrow tabs show just the icon, like Firefox
            painter.setPen(QColor(P.TEXT))
            text = self.fontMetrics().elidedText(self.title, Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


class TabBar(QTabBar):
    newTabRequested = pyqtSignal()
    audioClicked = pyqtSignal(int)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._available = 900  # set first: Qt asks for tab sizes while the setters below run
        self.setObjectName("Tabs")
        self.setDocumentMode(True)
        self.setDrawBase(False)
        self.setExpanding(False)
        self.setMovable(True)
        self.setTabsClosable(True)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setSelectionBehaviorOnRemove(QTabBar.SelectionBehavior.SelectRightTab)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def label(self, index: int) -> TabLabel | None:
        widget = self.tabButton(index, QTabBar.ButtonPosition.LeftSide)
        return widget if isinstance(widget, TabLabel) else None

    def add_tab(self, index: int) -> int:
        index = self.insertTab(index, "")
        self.setTabButton(index, QTabBar.ButtonPosition.LeftSide, TabLabel(self))
        self._size_labels()
        return index

    def set_available_width(self, width: int) -> None:
        width = max(TAB_MIN_WIDTH, width)
        if width != self._available:
            self._available = width
            self.setElideMode(self.elideMode())  # makes QTabBar lay its tabs out again
            self.updateGeometry()

    def tabSizeHint(self, index: int) -> QSize:
        width = clamp(self._available // max(1, self.count()), TAB_MIN_WIDTH, TAB_MAX_WIDTH)
        return QSize(width, TAB_HEIGHT)

    def minimumTabSizeHint(self, index: int) -> QSize:
        return self.tabSizeHint(index)

    def tabLayoutChange(self) -> None:
        super().tabLayoutChange()
        self._size_labels()

    def _size_labels(self) -> None:
        for i in range(self.count()):
            label = self.label(i)
            if label is not None:
                label.resize(max(0, self.tabRect(i).width() - TAB_CLOSE_AREA), TabLabel.HEIGHT)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._size_labels()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            index = self.tabAt(pos)
            label = self.label(index) if index >= 0 else None
            if label is not None and label.audio and label.audio_rect().contains(label.mapFrom(self, pos)):
                self.audioClicked.emit(index)
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0:
                self.tabCloseRequested.emit(index)
                return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.tabAt(event.position().toPoint()) < 0:
            self.newTabRequested.emit()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:  # don't switch tabs while scrolling over the strip (Firefox doesn't)
        event.ignore()


class TabStrip(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("TabStrip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.tabbar = TabBar(self)
        self.new_tab_button = tool_button(icon("plus"), f"Open a new tab ({shortcut_text('Ctrl+T')})")
        self.list_button = tool_button(icon("chevron-down"), "List all tabs")
        self.list_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 6, 0)
        layout.setSpacing(2)
        layout.addWidget(self.tabbar)
        layout.addWidget(self.new_tab_button)
        layout.addStretch(1)
        layout.addWidget(self.list_button)
        self.setFixedHeight(TAB_HEIGHT + 2)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        reserved = self.new_tab_button.width() + self.list_button.width() + 4 + 6 + 3 * 2 + 24
        self.tabbar.set_available_width(self.width() - reserved)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.tabbar.newTabRequested.emit()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Address bar
# ══════════════════════════════════════════════════════════════════════════════════════════
URL_ROLE = Qt.ItemDataRole.UserRole + 1
TITLE_ROLE = Qt.ItemDataRole.UserRole + 2
KIND_ROLE = Qt.ItemDataRole.UserRole + 3


class SuggestionDelegate(QStyledItemDelegate):
    def __init__(self, parent: QObject, engine_name):
        super().__init__(parent)
        self.engine_name = engine_name

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), 34)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(2, 1, -2, -1)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(185, 163, 255, 60))
            painter.drawRoundedRect(QRectF(rect), 4, 4)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(251, 251, 254, 18))
            painter.drawRoundedRect(QRectF(rect), 4, 4)
        decoration = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(decoration, QIcon):
            decoration.paint(painter, QRect(rect.left() + 10, rect.center().y() - 8, 16, 16))
        kind = index.data(KIND_ROLE)
        title = index.data(TITLE_ROLE) or ""
        url = index.data(URL_ROLE) or ""
        x = rect.left() + 38
        available = rect.right() - x - 10
        metrics = option.fontMetrics
        if kind == "search":
            first, second = title, f" — Search with {self.engine_name()}"
            second_color = P.TEXT_3
        else:
            first, second = (title or url), (f" — {url}" if title else "")
            second_color = P.ACCENT
        first = metrics.elidedText(first, Qt.TextElideMode.ElideRight, int(available * (0.6 if second else 1)))
        painter.setPen(QColor(P.TEXT))
        text_rect = QRect(x, rect.top(), available, rect.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, first)
        used = metrics.horizontalAdvance(first)
        if second and available - used > 30:
            painter.setPen(QColor(second_color))
            second = metrics.elidedText(second, Qt.TextElideMode.ElideRight, available - used)
            painter.drawText(QRect(x + used, rect.top(), available - used, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, second)
        painter.restore()


class UrlBar(QLineEdit):
    navigate = pyqtSignal(str)

    def __init__(self, win: "BrowserWindow"):
        super().__init__()
        self.win = win
        self.setObjectName("UrlBar")
        self.setFixedHeight(34)
        self.setClearButtonEnabled(False)
        self.identity = QAction(icon("search", P.TEXT_2), "", self)
        self.addAction(self.identity, QLineEdit.ActionPosition.LeadingPosition)
        self.zoom_action = QAction(self)
        self.zoom_action.setVisible(False)
        self.addAction(self.zoom_action, QLineEdit.ActionPosition.TrailingPosition)
        self.star = QAction(icon("star", P.TEXT_2), "Bookmark this page", self)
        self.addAction(self.star, QLineEdit.ActionPosition.TrailingPosition)
        self.model = QStandardItemModel(self)
        self.completer = QCompleter(self.model, self)
        self.completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.completer.setCompletionRole(URL_ROLE)
        self.completer.setMaxVisibleItems(10)
        # Keep Python references: the popup is created by Qt, and without them PyQt would drop our delegate.
        self.suggestion_view = popup = self.completer.popup()
        self.suggestion_delegate = SuggestionDelegate(popup, lambda: self.win.settings.get("search_engine"))
        popup.setObjectName("Suggestions")
        popup.setItemDelegate(self.suggestion_delegate)
        popup.setMouseTracking(True)
        popup.setUniformItemSizes(True)
        popup.clicked.connect(self._on_suggestion_clicked)
        self.setCompleter(self.completer)
        self.textEdited.connect(self._update_suggestions)
        self.returnPressed.connect(self._on_return)
        self._select_on_click = False

    def set_url_text(self, text: str) -> None:
        self.setText(text)
        self.setModified(False)
        self.setCursorPosition(0)

    def _update_suggestions(self, text: str) -> None:
        self.model.clear()
        query = text.strip()
        if not query:
            return
        search = QStandardItem()
        search.setData(text, URL_ROLE)
        search.setData(query, TITLE_ROLE)
        search.setData("search", KIND_ROLE)
        search.setData(icon("search", P.TEXT_2), Qt.ItemDataRole.DecorationRole)
        self.model.appendRow(search)
        seen: set[str] = set()
        rows: list[tuple[str, str, str]] = [(n["url"], n["title"], "bookmark") for n in self.win.bookmarks.search(query, 4)]
        rows += [(url, title, "history") for url, title in self.win.history.search(query, 10)]
        for url, title, kind in rows:
            if url in seen or len(seen) >= 8:
                continue
            seen.add(url)
            item = QStandardItem()
            item.setData(url, URL_ROLE)
            item.setData(title, TITLE_ROLE)
            item.setData(kind, KIND_ROLE)
            item.setData(self.win.favicons.get(url), Qt.ItemDataRole.DecorationRole)
            self.model.appendRow(item)

    def hide_suggestions(self) -> None:
        if self.completer.popup().isVisible():
            self.completer.popup().hide()
        QTimer.singleShot(0, self._reclaim_activation)

    def _reclaim_activation(self) -> None:
        # Some platforms leave the hidden suggestions pop-up as the "active" window; hand activation back.
        active = QApplication.activeWindow()
        if self.isVisible() and (active is self.completer.popup() or (
                active is None and QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive)):
            self.window().activateWindow()

    def _on_suggestion_clicked(self, index) -> None:
        url = index.data(URL_ROLE)
        if url:
            self.hide_suggestions()
            self.navigate.emit(url)

    def _on_return(self) -> None:
        self.hide_suggestions()
        self.navigate.emit(self.text())

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        reason = event.reason()
        # Select the whole address when the user moves into the bar - but not when focus merely comes back
        # from the suggestions pop-up or another window (that would make the next keystroke replace the text).
        if reason in (Qt.FocusReason.PopupFocusReason, Qt.FocusReason.ActiveWindowFocusReason):
            return
        if reason == Qt.FocusReason.MouseFocusReason:
            self._select_on_click = True
        QTimer.singleShot(0, self.selectAll)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if self._select_on_click:
            self._select_on_click = False
            if not self.hasSelectedText():
                self.selectAll()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:  # like Firefox: 1st Esc closes suggestions, 2nd restores the address
            event.accept()
            if self.completer.popup().isVisible():
                self.hide_suggestions()
                return
            current = display_url(self.win.current_url())
            if self.text() != current:
                self.set_url_text(current)
                self.selectAll()
            else:
                tab = self.win.current_tab()
                if tab is not None:
                    tab.view.setFocus()
            return
        super().keyPressEvent(event)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Panels, bars and bubbles
# ══════════════════════════════════════════════════════════════════════════════════════════
class Panel(QFrame):
    """A Firefox-style arrow panel that drops down below a toolbar button."""

    def __init__(self, parent: QWidget, translucent: bool = True, pinned: bool = False):
        # A pinned panel lives inside the window and stays open until closed; a popup closes as soon as focus leaves.
        super().__init__(parent, Qt.WindowType.Widget if pinned else Qt.WindowType.Popup)
        self.pinned = pinned
        self.setObjectName("Panel")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if pinned:
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            parent.installEventFilter(self)  # follow the window as it resizes
        elif TRANSLUCENT_POPUPS and translucent:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        else:
            self.setProperty("square", True)
        self._anchor: QWidget | None = None

    def eventFilter(self, watched, event) -> bool:
        if self.pinned and event.type() == QEvent.Type.Resize and watched is self.parentWidget():
            self.reposition()
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:
        if self.pinned and event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        # Translucent windows skip their style-sheet background, so paint the rounded panel ourselves.
        painter = QPainter(self)
        option = QStyleOption()
        option.initFrom(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option, painter, self)
        painter.end()
        super().paintEvent(event)

    def popup_at(self, anchor: QWidget) -> None:
        self._anchor = anchor
        self.adjustSize()
        self.reposition()
        self.show()
        if self.pinned:
            self.raise_()
            self.setFocus()
        else:
            self.activateWindow()

    def reposition(self) -> None:
        anchor = self._anchor
        if anchor is None or sip.isdeleted(anchor):
            return
        if self.pinned:
            parent = self.parentWidget()
            corner = anchor.mapTo(parent, QPoint(anchor.width(), anchor.height()))
            self.move(clamp(corner.x() - self.width(), 4, max(4, parent.width() - self.width() - 4)),
                      clamp(corner.y() + 4, 0, max(0, parent.height() - self.height())))
            return
        bottom_right = anchor.mapToGlobal(QPoint(anchor.width(), anchor.height()))
        x = bottom_right.x() - self.width()
        y = bottom_right.y() + 4
        screen = (anchor.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        x = clamp(x, screen.left() + 4, max(screen.left() + 4, screen.right() - self.width() - 4))
        if y + self.height() > screen.bottom():
            y = max(screen.top(), anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 4)
        self.move(x, y)


class InfoBar(QFrame):
    """A Firefox-style notification bar shown at the top of a tab."""

    def __init__(self, icon_: QIcon, text: str, kind: str = "info"):
        super().__init__()
        self.setObjectName("InfoBar")
        self.setProperty("kind", kind)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 6, 6)
        layout.setSpacing(10)
        picture = QLabel()
        picture.setPixmap(icon_.pixmap(QSize(18, 18)))
        layout.addWidget(picture)
        self.text = QLabel(text)
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.text, 1)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(6)
        layout.addLayout(self.buttons)
        close = tool_button(icon("close", P.TEXT_2), "Close", 28)
        close.clicked.connect(self.dismiss)
        layout.addWidget(close)
        self.on_dismiss = None

    def add_button(self, text: str, callback, primary: bool = False) -> QPushButton:
        button = make_button(text, primary)
        button.clicked.connect(lambda *_: callback())
        self.buttons.addWidget(button)
        return button

    def dismiss(self) -> None:
        if self.on_dismiss is not None:
            callback, self.on_dismiss = self.on_dismiss, None
            callback()
        self.hide()
        self.deleteLater()


class StatusBubble(QLabel):
    """Shows the address of the link under the mouse, bottom-left like Firefox."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("StatusBubble")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def show_text(self, text: str) -> None:
        if not text:
            self.hide()
            return
        parent = self.parentWidget()
        self.setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, int(parent.width() * 0.55)))
        self.adjustSize()
        self.move(4, parent.height() - self.height() - 4)
        self.raise_()
        self.show()


class LoadingBar(QWidget):
    """A thin accent-coloured progress line under the toolbars."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.progress = 0
        self.setFixedHeight(2)
        self.hide()

    def set_progress(self, value: int, loading: bool) -> None:
        self.progress = value
        self.setVisible(loading and value < 100)
        self.raise_()
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        width = int(self.width() * clamp(max(self.progress, 8), 0, 100) / 100)
        painter.fillRect(0, 0, width, self.height(), QColor(P.ACCENT))


class Toast(QFrame):
    """A short message that appears under the toolbar and fades away."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("Toast")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(10)
        self.picture = QLabel()
        layout.addWidget(self.picture)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(420)
        layout.addWidget(self.label)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        self.on_click = None
        self.hide()

    def show_message(self, text: str, kind: str = "success", timeout: int = 4500, on_click=None) -> None:
        """kind: "success", "info" or "error". on_click runs if the message is clicked."""
        self.on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor if on_click else Qt.CursorShape.ArrowCursor)
        error = kind == "error"
        self.setProperty("error", error)
        self.style().unpolish(self)
        self.style().polish(self)
        name, color = {"error": ("warning", P.DANGER), "info": ("info", P.ACCENT)}.get(kind, ("check", P.ACCENT))
        self.picture.setPixmap(icon(name, color).pixmap(QSize(18, 18)))
        self.label.setText(text)
        self.adjustSize()
        parent = self.parentWidget()
        self.move(max(8, parent.width() - self.width() - 12), 10)
        self.raise_()
        self.show()
        self.timer.start(timeout if not error else max(timeout, 7000))

    def mousePressEvent(self, _event) -> None:
        callback, self.on_click = self.on_click, None
        self.hide()
        if callback is not None:
            callback()


class ContentArea(QWidget):
    """Holds the tab pages plus the floating overlays (status bubble, loading line, toasts)."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.stack = QStackedWidget(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)
        self.bubble = StatusBubble(self)
        self.loading_bar = LoadingBar(self)
        self.toast = Toast(self)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.loading_bar.setGeometry(0, 0, self.width(), 2)
        if self.bubble.isVisible():
            self.bubble.move(4, self.height() - self.bubble.height() - 4)
        if self.toast.isVisible():
            self.toast.move(max(8, self.width() - self.toast.width() - 12), 10)


class FindBar(QFrame):
    """Find in page (Ctrl+F), docked at the bottom like Firefox."""

    def __init__(self, win: "BrowserWindow"):
        super().__init__(win)
        self.win = win
        self.setObjectName("FindBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 6, 5)
        layout.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Find in page")
        self.edit.setFixedWidth(280)
        self.edit.textChanged.connect(lambda *_: self.find())
        self.edit.installEventFilter(self)
        layout.addWidget(self.edit)
        self.previous_button = tool_button(icon("chevron-up"), "Find the previous occurrence (Shift+Enter)", 30)
        self.previous_button.clicked.connect(lambda *_: self.find(backward=True))
        layout.addWidget(self.previous_button)
        self.next_button = tool_button(icon("chevron-down"), "Find the next occurrence (Enter)", 30)
        self.next_button.clicked.connect(lambda *_: self.find())
        layout.addWidget(self.next_button)
        self.case = QCheckBox("Match Case")
        self.case.toggled.connect(lambda *_: self.find())
        layout.addWidget(self.case)
        self.status = tone_label("", "secondary")
        layout.addWidget(self.status)
        layout.addStretch(1)
        close = tool_button(icon("close", P.TEXT_2), "Close the find bar (Esc)", 30)
        close.clicked.connect(lambda *_: self.close_bar())
        layout.addWidget(close)
        self.hide()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.edit and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.find(backward=bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.close_bar()
                return True
        return super().eventFilter(obj, event)

    def open(self) -> None:
        self.show()
        self.edit.setFocus()
        self.edit.selectAll()
        if self.edit.text():
            self.find()

    def close_bar(self) -> None:
        tab = self.win.current_tab()
        if tab is not None and tab.pending is None:
            tab.page.findText("")
            tab.view.setFocus()
        self.status.setText("")
        self.hide()

    def find(self, backward: bool = False) -> None:
        tab = self.win.current_tab()
        if tab is None or not self.isVisible():
            return
        tab.ensure_loaded()
        text = self.edit.text()
        self._set_not_found(False)
        if not text:
            tab.page.findText("")
            self.status.setText("")
            return
        flags = QWebEnginePage.FindFlag(0)
        if backward:
            flags |= QWebEnginePage.FindFlag.FindBackward
        if self.case.isChecked():
            flags |= QWebEnginePage.FindFlag.FindCaseSensitively
        tab.page.findText(text, flags)

    def show_result(self, result) -> None:
        if not self.isVisible() or not self.edit.text():
            return
        total = result.numberOfMatches()
        if total == 0:
            self.status.setText("Phrase not found")
            self._set_not_found(True)
        else:
            self.status.setText(f"{result.activeMatch()} of {total} match{'es' if total != 1 else ''}")
            self._set_not_found(False)

    def _set_not_found(self, value: bool) -> None:
        if bool(self.edit.property("notfound")) != value:
            self.edit.setProperty("notfound", value)
            self.edit.style().unpolish(self.edit)
            self.edit.style().polish(self.edit)


class BookmarkPanel(Panel):
    """The panel that opens from the star in the address bar."""

    def __init__(self, win: "BrowserWindow", url: str, title: str):
        super().__init__(win)
        self.win = win
        self.url = url
        store = win.bookmarks
        existing = store.for_url(url)
        self.created = not existing
        self.node = existing[0] if existing else store.add_bookmark(title, url, "toolbar")
        self.cancelled = False
        self.removed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addWidget(tone_label("New bookmark" if self.created else "Edit bookmark", "title"))
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.name = QLineEdit(self.node["title"])
        self.name.setMinimumWidth(260)
        form.addRow("Name", self.name)
        self.folder = QComboBox()
        parent = store.parent(self.node["id"])
        for folder_id, folder_title, depth in store.folders():
            self.folder.addItem(("    " * depth) + folder_title, folder_id)
            if parent is not None and folder_id == parent["id"]:
                self.folder.setCurrentIndex(self.folder.count() - 1)
        form.addRow("Folder", self.folder)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        remove = make_button("Remove bookmark" if not self.created else "Remove")
        remove.clicked.connect(lambda *_: self._remove())
        done = make_button("Save", primary=True)
        done.clicked.connect(lambda *_: self.close())
        buttons.addWidget(remove)
        buttons.addWidget(done)
        layout.addLayout(buttons)
        self.name.returnPressed.connect(self.close)
        QTimer.singleShot(0, lambda: (self.name.setFocus(), self.name.selectAll()))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled = True
            self.close()
            return
        super().keyPressEvent(event)

    def _remove(self) -> None:
        self.removed = True
        self.win.bookmarks.remove_url(self.url)
        self.close()

    def closeEvent(self, event) -> None:
        store = self.win.bookmarks
        if not self.removed and not self.cancelled and store.node(self.node["id"]) is not None:
            if self.name.text().strip() != self.node["title"]:
                store.update(self.node["id"], title=self.name.text())
            folder_id = self.folder.currentData()
            parent = store.parent(self.node["id"])
            if folder_id and parent is not None and folder_id != parent["id"]:
                store.move(self.node["id"], folder_id)
        super().closeEvent(event)


class SiteInfoPanel(Panel):
    """Opens from the padlock: connection security, the site's permissions and cookies, and its site settings."""

    def __init__(self, win: "BrowserWindow", url: QUrl):
        super().__init__(win)
        self.win = win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        host = url.host() or url.toString()
        if url.scheme() == "https":
            name, color, heading, detail = "lock", P.TEXT, "Connection secure", "Information you send to this site is encrypted."
            if host in win.certificate_exceptions:
                name, color, heading = "warning", P.WARNING, "You accepted a certificate risk"
                detail = "You chose to trust this site even though its certificate has problems."
        elif url.scheme() == "http":
            name, color, heading, detail = "warning", P.WARNING, "Connection not secure", \
                "Passwords or other information you send to this site could be seen by others."
        else:
            name, color, heading, detail = "info", P.TEXT_2, f"{APP_NAME} page", "This is a page built into the browser or one of its extensions."
        top = QHBoxLayout()
        picture = QLabel()
        picture.setPixmap(icon(name, color).pixmap(QSize(20, 20)))
        top.addWidget(picture)
        top.addWidget(tone_label(elide(host, 48), "title"))
        top.addStretch(1)
        layout.addLayout(top)
        layout.addWidget(tone_label(heading, "heading"))
        layout.addWidget(tone_label(detail, "secondary", wrap=True))
        origin = origin_of(url)
        decisions = [(kind, state) for o, kind, state in win.permission_decisions() if o == origin] if origin else []
        if decisions:
            layout.addWidget(self._line())
            layout.addWidget(tone_label("Permissions", "heading"))
            for kind, state in decisions:
                row = QHBoxLayout()
                row.addWidget(tone_label(f"{'Allowed' if state == 'allow' else 'Blocked'} to {PERMISSION_TEXT[kind]}", "secondary"), 1)
                clear = tool_button(icon("close", P.TEXT_2), "Forget this decision", 26)
                clear.clicked.connect(lambda *_, k=kind, r=row: self._reset(origin, k, r))
                row.addWidget(clear)
                layout.addLayout(row)
        if origin:
            self.site = site_of(url.host())
            layout.addWidget(self._line())
            row = QHBoxLayout()
            self.cookies = tone_label("", "secondary")
            row.addWidget(self.cookies, 1)
            settings = make_button("Site settings…")
            settings.clicked.connect(lambda *_: (self.close(), win.show_site_settings(url)))
            row.addWidget(settings)
            layout.addLayout(row)
            index = CookieIndex.of(win.profile)
            index.changed.connect(self._count_cookies)
            self._count_cookies()
        self.setMinimumWidth(340)

    @staticmethod
    def _line() -> QFrame:
        line = QFrame()
        line.setObjectName("PanelSeparator")
        line.setFixedHeight(1)
        return line

    def _count_cookies(self) -> None:
        if not sip.isdeleted(self):
            count = len(CookieIndex.of(self.win.profile).for_site(self.site))
            self.cookies.setText(f"{count} cookie{'' if count == 1 else 's'} from {self.site}")

    def _reset(self, origin: str, kind, row: QHBoxLayout) -> None:
        self.win.set_permission_state(origin, kind, "ask")
        for i in range(row.count()):
            widget = row.itemAt(i).widget()
            if widget is not None:
                widget.setEnabled(False)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Downloads
# ══════════════════════════════════════════════════════════════════════════════════════════
class DownloadItem(QObject):
    changed = pyqtSignal()
    D = QWebEngineDownloadRequest.DownloadState

    def __init__(self, request: QWebEngineDownloadRequest):
        super().__init__()
        self.request = request
        self.path = Path(request.downloadDirectory()) / request.downloadFileName()
        self.received = 0
        self.total = -1
        self.state = request.state()
        self.started = time.monotonic()
        self.error = ""
        request.receivedBytesChanged.connect(self._refresh)
        request.totalBytesChanged.connect(self._refresh)
        request.stateChanged.connect(self._refresh)
        request.isFinishedChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_args) -> None:
        try:
            self.received = self.request.receivedBytes()
            self.total = self.request.totalBytes()
            self.state = self.request.state()
            if self.state == self.D.DownloadInterrupted:
                self.error = self.request.interruptReasonString()
        except RuntimeError:  # the request object is gone (profile shutting down)
            return
        self.changed.emit()

    @property
    def active(self) -> bool:
        return self.state in (self.D.DownloadRequested, self.D.DownloadInProgress)

    def fraction(self) -> float:
        if self.state == self.D.DownloadCompleted:
            return 1.0
        return self.received / self.total if self.total > 0 else 0.0

    def status_text(self) -> str:
        if self.state == self.D.DownloadCompleted:
            return f"Completed — {human_size(self.received)}"
        if self.state == self.D.DownloadCancelled:
            return "Canceled"
        if self.state == self.D.DownloadInterrupted:
            return f"Failed — {self.error or 'interrupted'}"
        elapsed = max(0.5, time.monotonic() - self.started)
        speed = self.received / elapsed
        if self.total > 0:
            text = f"{human_size(self.received)} of {human_size(self.total)}"
            if speed > 0:
                left = (self.total - self.received) / speed
                text += f" — {int(left)} s left" if left < 90 else f" — {int(left // 60)} min left"
            return text
        return f"{human_size(self.received)} — {human_size(speed)}/s"

    def cancel(self) -> None:
        try:
            self.request.cancel()
        except RuntimeError:
            pass


class DownloadRow(QFrame):
    def __init__(self, item: DownloadItem, win: "BrowserWindow"):
        super().__init__()
        self.item = item
        self.win = win
        self.setObjectName("DownloadRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 6, 6)
        layout.setSpacing(10)
        picture = QLabel()
        picture.setPixmap(icon("file", P.TEXT_2).pixmap(QSize(22, 22)))
        layout.addWidget(picture)
        text = QVBoxLayout()
        text.setSpacing(3)
        self.name = tone_label(elide(item.path.name, 46), "heading")
        self.status = tone_label("", "dim")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        text.addWidget(self.name)
        text.addWidget(self.bar)
        text.addWidget(self.status)
        layout.addLayout(text, 1)
        self.action = tool_button(icon("close", P.TEXT_2), "", 30)
        self.action.clicked.connect(lambda *_: self._act())
        layout.addWidget(self.action)
        item.changed.connect(self.refresh)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh()

    def refresh(self) -> None:
        item = self.item
        self.status.setText(item.status_text())
        self.bar.setVisible(item.active)
        self.bar.setValue(int(item.fraction() * 1000))
        if item.active:
            self.action.setIcon(icon("close", P.TEXT_2))
            self.action.setToolTip("Cancel download")
        elif item.state == DownloadItem.D.DownloadCompleted:
            self.action.setIcon(icon("folder", P.TEXT_2))
            self.action.setToolTip("Show in folder")
        else:
            self.action.setIcon(icon("reload", P.TEXT_2))
            self.action.setToolTip("Retry")

    def _act(self) -> None:
        item = self.item
        if item.active:
            item.cancel()
        elif item.state == DownloadItem.D.DownloadCompleted:
            reveal_in_file_manager(item.path)
        else:
            try:
                self.win.current_page_download(item.request.url())
            except RuntimeError:
                pass

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.item.state == DownloadItem.D.DownloadCompleted:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.item.path)))
            panel = self.window()
            if isinstance(panel, Panel):
                panel.close()
        super().mouseReleaseEvent(event)


class DownloadsPanel(Panel):
    def __init__(self, win: "BrowserWindow"):
        super().__init__(win)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(4)
        layout.addWidget(tone_label("  Downloads", "title"))
        items = list(reversed(win.downloads))[:12]
        if not items:
            empty = tone_label("No downloads for this session.", "dim")
            empty.setContentsMargins(8, 12, 8, 12)
            layout.addWidget(empty)
        for item in items:
            layout.addWidget(DownloadRow(item, win))
        footer = QHBoxLayout()
        open_folder = make_button("Show Downloads Folder")
        open_folder.clicked.connect(lambda *_: (QDesktopServices.openUrl(QUrl.fromLocalFile(str(win.settings.downloads_dir()))), self.close()))
        clear = make_button("Clear List")
        clear.setEnabled(any(not i.active for i in win.downloads))
        clear.clicked.connect(lambda *_: (win.clear_finished_downloads(), self.close()))
        footer.addWidget(open_folder)
        footer.addStretch(1)
        footer.addWidget(clear)
        layout.addSpacing(4)
        layout.addLayout(footer)
        self.setFixedWidth(420)


class DownloadButton(QToolButton):
    """The toolbar button; draws a small progress bar while downloads are running."""

    def __init__(self) -> None:
        super().__init__()
        self.setIcon(icon("download"))
        self.setIconSize(QSize(16, 16))
        self.setFixedSize(32, 32)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Downloads")
        self.progress = -1.0
        self.attention = False

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.progress >= 0:
            track = QRectF(8, self.height() - 7, self.width() - 16, 3)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(251, 251, 254, 50))
            painter.drawRoundedRect(track, 1.5, 1.5)
            painter.setBrush(QColor(P.ACCENT))
            painter.drawRoundedRect(QRectF(track.x(), track.y(), max(3.0, track.width() * self.progress), 3), 1.5, 1.5)
        elif self.attention:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(P.ACCENT))
            painter.drawEllipse(QRectF(self.width() - 10, 5, 6, 6))


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Extension pop-ups and pop-up windows
# ══════════════════════════════════════════════════════════════════════════════════════════
# The pop-up's preferred size, measured like Chrome's auto-resize: laid out at its narrowest (min-content) width,
# with scroll bars off, so the current viewport can't feed back into the result.
POPUP_MEASURE_JS = """(() => {
  const b = document.body, d = document.documentElement;
  if (!b) return [0, 0];
  const pw = d.style.width, po = d.style.overflow;
  d.style.overflow = 'hidden'; d.style.width = 'min-content';
  const cs = getComputedStyle(b), r = b.getBoundingClientRect();
  const mx = parseFloat(cs.marginLeft) + parseFloat(cs.marginRight), my = parseFloat(cs.marginTop) + parseFloat(cs.marginBottom);
  const w = Math.max(d.getBoundingClientRect().width, r.width + mx, b.scrollWidth + mx);
  let h = Math.max(r.height, b.scrollHeight) + my;
  // A last child's bottom margin can collapse through the body's (no padding or border between): it isn't in the
  // body's box, but the page needs the room.
  const shown = (e) => { for (let c = e.lastElementChild; c; c = c.previousElementSibling) { const s = getComputedStyle(c);
    if (s.display !== 'none' && s.position !== 'absolute' && s.position !== 'fixed') return c; } return null; };
  const last = shown(b);
  let tail = 0;
  for (let e = b, c = last; c; e = c, c = shown(c)) {
    const es = getComputedStyle(e);
    if (parseFloat(es.paddingBottom) || parseFloat(es.borderBottomWidth)) break;
    tail = Math.max(tail, parseFloat(getComputedStyle(c).marginBottom) || 0);
  }
  if (tail) h = Math.max(h, last.getBoundingClientRect().bottom + scrollY + Math.max(tail, parseFloat(cs.marginBottom)));
  if (document.compatMode === 'BackCompat') {  // quirks mode: an auto-height body fills the viewport - use its content
    const range = document.createRange();
    range.selectNodeContents(b);
    h = range.getBoundingClientRect().bottom + scrollY + parseFloat(cs.paddingBottom) + parseFloat(cs.marginBottom);
  }
  d.style.width = pw; d.style.overflow = po;
  return [Math.ceil(w), Math.ceil(h)];
})()"""


class ExtensionPopup(Panel):
    """Shows an extension's toolbar pop-up page, sized to its content like Chrome does."""

    def __init__(self, win: "BrowserWindow", url: QUrl, ext_id: str = ""):
        super().__init__(win, translucent=False)
        self.win = win
        self.ext_id = ext_id or url.host()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        self.view = QWebEngineView(self)
        self.page = WebPage(win.profile, self.view)
        self.view.setPage(self.page)
        self.view.setFixedSize(360, 160)
        layout.addWidget(self.view)
        self.page.windowCloseRequested.connect(self.close)
        self.page.newWindowRequested.connect(self._open_elsewhere)
        self.page.urlChanged.connect(self._on_url)
        self.page.permissionRequested.connect(self._on_permission)
        self.page.loadFinished.connect(lambda *_: self._measure_soon())
        self._resize_timer = QTimer(self)  # content that changes later (data arriving, sections opening) resizes it too
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(100)
        self._resize_timer.timeout.connect(self._measure)
        self.page.contentsSizeChanged.connect(lambda *_: self._resize_timer.start())
        self._poll = QTimer(self)  # contentsSizeChanged never reports shrinking content
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._measure)
        self.page.load(url)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._poll.start()

    def hideEvent(self, event) -> None:
        self._poll.stop()
        super().hideEvent(event)

    def _measure_soon(self) -> None:
        for delay in (0, 250, 800, 2000):
            QTimer.singleShot(delay, self._measure)

    def _measure(self) -> None:
        if not sip.isdeleted(self) and self.isVisible():
            self.page.runJavaScript(POPUP_MEASURE_JS, self._apply_size)

    def _apply_size(self, result) -> None:
        if sip.isdeleted(self) or not isinstance(result, list) or len(result) != 2:
            return
        try:
            width, height = int(result[0]), int(result[1])
        except (TypeError, ValueError):
            return
        if width < 1 or height < 1:
            return
        size = QSize(clamp(width, 25, 800), clamp(height, 25, 600))  # Chrome's limits
        if size != self.view.size():
            self.view.setFixedSize(size)
            self.adjustSize()
            self.reposition()

    def _on_url(self, url: QUrl) -> None:
        # chrome.tabs.update({url}) without the polyfill navigates the pop-up itself: send it to the tab instead.
        if (url.isEmpty() or url.toString() == "about:blank" or (url.scheme() == "chrome-extension" and url.host() == self.ext_id)
                or url.toString().startswith(f"blob:chrome-extension://{self.ext_id}/")):
            return
        self.page.blockSignals(True)
        self.page.triggerAction(QWebEnginePage.WebAction.Stop)
        self.win.open_url(url, "current")
        self.close()

    def _on_permission(self, permission: QWebEnginePermission) -> None:
        permission = QWebEnginePermission(permission)  # PyQt's argument dies with this call
        self.close()  # a modal dialog would dismiss the pop-up anyway; ask on the window
        QTimer.singleShot(0, lambda: self.win.ask_permission_modal(self.win, permission))

    def _open_elsewhere(self, request) -> None:
        self.win.handle_new_window(request, None)
        self.close()


class PrivacyScreen(QWidget):
    """Covers a browser window - toolbars and all - with an opaque grey screen and the logo while Chrome 2 isn't the
    active application (another app in front, swiping between desktops, Mission Control, the app switcher), so what's
    on the pages doesn't show there. It follows the application's state, not the window's: Chrome 2's own dialogs,
    menus and panels don't set it off. Fades in, and goes at once when the app is active again; it never takes a click
    or a key. Settings > Privacy turns it off ("privacy_screen")."""

    FADE_MS = 120
    DELAY_MS = 40  # an app that is inactive for less (a full-screen switch, a system prompt flashing by) isn't covered
    COLOR = "#5f6368"

    def __init__(self, window: QWidget, settings: Settings):
        from PyQt6.QtCore import QVariantAnimation
        super().__init__(window)
        self.settings = settings
        self.covering = False
        self.opacity = 0.0
        self._logo = None
        self.setObjectName("PrivacyScreen")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(self.FADE_MS)
        self._fade.valueChanged.connect(self._set_opacity)
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(self.DELAY_MS)
        self._delay.timeout.connect(self.cover)
        window.installEventFilter(self)
        QGuiApplication.instance().applicationStateChanged.connect(self._on_state)
        settings.changed.connect(self._on_setting)

    def _on_state(self, state) -> None:
        if state == Qt.ApplicationState.ApplicationActive:
            self.uncover()
        elif self.settings.get("privacy_screen") and not self.covering:
            self._delay.start()

    def _on_setting(self, key: str) -> None:
        if key == "privacy_screen" and not self.settings.get("privacy_screen"):
            self.uncover()

    def cover(self) -> None:
        self._delay.stop()
        parent = self.parentWidget()
        if self.covering or parent is None or not self.settings.get("privacy_screen"):
            return
        self.covering = True
        self.setGeometry(parent.rect())
        self.raise_()
        self.show()
        self._fade.stop()
        self._fade.setStartValue(self.opacity)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def uncover(self) -> None:
        self._delay.stop()
        self._fade.stop()
        self.covering = False
        self.opacity = 0.0
        self.hide()

    def _set_opacity(self, value) -> None:
        self.opacity = float(value)
        self.update()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.parentWidget():
            if event.type() == QEvent.Type.Resize:
                self.setGeometry(obj.rect())
            elif event.type() == QEvent.Type.ChildAdded and self.covering:  # stay on top of anything shown meanwhile
                QTimer.singleShot(0, lambda: None if sip.isdeleted(self) or not self.covering else self.raise_())
        return False

    def paintEvent(self, event) -> None:
        size = clamp(min(self.width(), self.height()) // 5, 48, 128)
        scale = self.devicePixelRatioF()
        if self._logo is None or self._logo.width() != round(size * scale):  # one image: fades as a whole
            self._logo = logo_image(round(size * scale))
            self._logo.setDevicePixelRatio(scale)
        painter = QPainter(self)
        painter.setOpacity(self.opacity)
        painter.fillRect(self.rect(), QColor(self.COLOR))
        painter.drawImage(QRectF((self.width() - size) / 2, (self.height() - size) / 2, size, size), self._logo)
        painter.end()


class PopupWindow(QWidget):
    """A small window for pages opened with window.open(..., features) - e.g. "Sign in with…" pop-ups."""

    def __init__(self, win: "BrowserWindow", geometry: QRect):
        super().__init__(win, Qt.WindowType.Window)
        self.win = win
        self.setObjectName("PopupWindow")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.header = QLabel()
        self.header.setTextFormat(Qt.TextFormat.PlainText)
        self.header.setObjectName("PopupUrl")
        self.view = QWebEngineView(self)
        self.page = WebPage(win.profile, self.view)
        self.view.setPage(self.page)
        self.tab_id, self.window_id = next(_TAB_IDS), next(_WINDOW_IDS)  # a window of its own for chrome.windows
        self.pending, self.loading = None, False
        win.extensions.wire_tab(self.page, self.tab_id)
        self.page.loadStarted.connect(lambda: setattr(self, "loading", True))
        self.page.loadFinished.connect(lambda *_: setattr(self, "loading", False))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(self.view, 1)
        self.page.titleChanged.connect(lambda title: self.setWindowTitle(title or APP_NAME))
        self.page.urlChanged.connect(self._on_url)
        self.page.iconChanged.connect(lambda ic: self.setWindowIcon(ic))
        self.page.windowCloseRequested.connect(self.close)
        self.page.newWindowRequested.connect(lambda request: win.handle_new_window(request, None))
        self.page.permissionRequested.connect(lambda permission: win.ask_permission_modal(self, permission))
        self.page.fullScreenRequested.connect(lambda request: request.reject())
        width = geometry.width() if geometry.width() > 100 else 520
        height = geometry.height() if geometry.height() > 100 else 640
        self.resize(width, height + 30)
        if geometry.x() > 0 or geometry.y() > 0:
            self.move(geometry.topLeft())
        else:
            self.move(win.geometry().center() - self.rect().center())
        win.popups.add(self)
        self.destroyed.connect(lambda *_: win.popups.discard(self))
        self.privacy_screen = PrivacyScreen(self, win.settings)

    def _on_url(self, url: QUrl) -> None:
        secure = url.scheme() == "https"
        prefix = "🔒 " if secure else ""
        self.header.setText(prefix + elide(url.toDisplayString(), 90))
        self.win.extensions.bridge.tab_navigated(self, url)
        self.win.site_visited(url)

    def url(self) -> QUrl:
        url = self.page.url()
        return url if not url.isEmpty() else self.page.requestedUrl()

    def title(self) -> str:
        return self.page.title() or display_url(self.url())

    def load(self, url: QUrl) -> None:
        self.page.load(url)

    def closeEvent(self, event) -> None:
        self.page.blockSignals(True)
        super().closeEvent(event)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Dialogs
# ══════════════════════════════════════════════════════════════════════════════════════════
class BookmarkEditDialog(QDialog):
    def __init__(self, parent: QWidget, store: BookmarkStore, title: str, name: str = "", url: str | None = "",
                 folder_id: str = "toolbar"):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.name = QLineEdit(name)
        self.name.setMinimumWidth(340)
        form.addRow("Name", self.name)
        self.url = None
        if url is not None:
            self.url = QLineEdit(url)
            form.addRow("URL", self.url)
        self.folder = QComboBox()
        for fid, ftitle, depth in store.folders():
            self.folder.addItem(("    " * depth) + ftitle, fid)
            if fid == folder_id:
                self.folder.setCurrentIndex(self.folder.count() - 1)
        form.addRow("Folder", self.folder)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        if self.url is not None and not self.url.text().strip():
            self.url.setFocus()
            return
        self.accept()

    def values(self) -> tuple[str, str | None, str]:
        url = None
        if self.url is not None:
            raw = self.url.text().strip()
            url = raw if _SCHEME_RE.match(raw) else QUrl.fromUserInput(raw).toString()
        return self.name.text().strip(), url, self.folder.currentData()


class BookmarkTree(QTreeWidget):
    structureChanged = pyqtSignal()

    def dropEvent(self, event) -> None:
        super().dropEvent(event)
        QTimer.singleShot(0, self.structureChanged.emit)


class BookmarksManager(QDialog):
    """The Library window for organising bookmarks (drag & drop, folders, import/export)."""

    def __init__(self, win: "BrowserWindow"):
        super().__init__(win)
        self.win = win
        self.store = win.bookmarks
        self.setWindowTitle("Library — Bookmarks")
        self.resize(820, 560)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        for text, slot in (("New Bookmark…", self._new_bookmark), ("New Folder…", self._new_folder),
                           ("Edit…", self._edit), ("Delete", self._delete)):
            button = make_button(text)
            button.clicked.connect(lambda *_, s=slot: s())
            top.addWidget(button)
        top.addStretch(1)
        tools = make_button("Import and Backup")
        menu = Menu("", tools)
        menu.addAction("Import Bookmarks from HTML…", win.import_bookmarks)
        menu.addAction("Export Bookmarks to HTML…", win.export_bookmarks)
        tools.setMenu(menu)
        top.addWidget(tools)
        layout.addLayout(top)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search bookmarks")
        self.search.textChanged.connect(lambda *_: self.rebuild())
        layout.addWidget(self.search)
        self.tree = BookmarkTree()
        self.tree.setHeaderLabels(["Name", "Location"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setAnimated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 340)
        self.tree.itemDoubleClicked.connect(self._open_item)
        self.tree.structureChanged.connect(self._sync_structure)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.tree, 1)
        self.store.changed.connect(self.rebuild)
        self.win.favicons.updated.connect(self._refresh_icons)
        self.rebuild()

    def done(self, result: int) -> None:
        try:
            self.store.changed.disconnect(self.rebuild)
            self.win.favicons.updated.disconnect(self._refresh_icons)
        except (TypeError, RuntimeError):
            pass
        super().done(result)

    def _item(self, node: dict) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node["title"] or ("New Folder" if node["type"] == "folder" else node.get("url", "")),
                                node.get("url", "")])
        item.setData(0, Qt.ItemDataRole.UserRole, node["id"])
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if node["type"] == "folder":
            item.setIcon(0, icon("folder", P.TEXT_2))
            if node["id"] in self.store.roots:
                flags |= Qt.ItemFlag.ItemIsDropEnabled
            else:
                flags |= Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        else:
            item.setIcon(0, self.win.favicons.get(node["url"]))
            item.setToolTip(0, node["url"])
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        item.setFlags(flags)
        return item

    def _all_items(self) -> list[QTreeWidgetItem]:
        items: list[QTreeWidgetItem] = []

        def walk(parent) -> None:
            for i in range(parent.childCount()):
                child = parent.child(i)
                items.append(child)
                walk(child)

        walk(self.tree.invisibleRootItem())
        return items

    def _refresh_icons(self) -> None:
        for item in self._all_items():
            node = self.store.node(item.data(0, Qt.ItemDataRole.UserRole))
            if node is not None and node["type"] == "url":
                item.setIcon(0, self.win.favicons.get(node["url"]))

    def rebuild(self, *_args) -> None:
        # Keep the user's place: selection, current item and scroll position survive a refresh.
        selected = set(self._selected_ids())
        current = self.tree.currentItem()
        current_id = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        scroll = self.tree.verticalScrollBar().value()
        expanded = {self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) for i in range(self.tree.topLevelItemCount())}
        expanded |= self._expanded_ids()
        self.tree.clear()
        self._populate(expanded)
        for item in self._all_items():
            node_id = item.data(0, Qt.ItemDataRole.UserRole)
            if node_id == current_id:
                self.tree.setCurrentItem(item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            if node_id in selected:
                item.setSelected(True)
        self.tree.verticalScrollBar().setValue(scroll)

    def _populate(self, expanded: set) -> None:
        query = self.search.text().strip()
        searching = bool(query)
        self.tree.setDragEnabled(not searching)
        self.tree.setAcceptDrops(not searching)
        root = self.tree.invisibleRootItem()
        root.setFlags(root.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
        if searching:
            for node in self.store.search(query, 500):
                item = self._item(node)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
                self.tree.addTopLevelItem(item)
            return

        def add(node: dict, parent_item) -> None:
            item = self._item(node)
            parent_item.addChild(item)
            if node["type"] == "folder":
                for child in node["children"]:
                    add(child, item)
                item.setExpanded(node["id"] in expanded or not expanded)

        for key, _title in BookmarkStore.ROOTS:
            add(self.store.roots[key], root)

    def _expanded_ids(self) -> set[str]:
        ids: set[str] = set()

        def walk(item) -> None:
            for i in range(item.childCount()):
                child = item.child(i)
                if child.isExpanded():
                    ids.add(child.data(0, Qt.ItemDataRole.UserRole))
                walk(child)

        walk(self.tree.invisibleRootItem())
        return ids

    def _sync_structure(self) -> None:
        structure: dict[str, list[str]] = {}

        def walk(item) -> None:
            node_id = item.data(0, Qt.ItemDataRole.UserRole)
            node = self.store.node(node_id)
            if node is None or node["type"] != "folder":
                return
            structure[node_id] = [item.child(i).data(0, Qt.ItemDataRole.UserRole) for i in range(item.childCount())]
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        if not self.store.apply_structure(structure):
            self.rebuild()

    def _selected_ids(self) -> list[str]:
        return [item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()]

    def _target_folder(self) -> str:
        for node_id in self._selected_ids():
            node = self.store.node(node_id)
            if node is not None:
                if node["type"] == "folder":
                    return node_id
                parent = self.store.parent(node_id)
                if parent is not None:
                    return parent["id"]
        return "toolbar"

    def _new_bookmark(self) -> None:
        tab = self.win.current_tab()
        url = display_url(tab.url()) if tab else ""
        dialog = BookmarkEditDialog(self, self.store, "New Bookmark", tab.title() if url else "", url, self._target_folder())
        if run_dialog(dialog):
            name, url, folder = dialog.values()
            if url:
                self.store.add_bookmark(name or url, url, folder)

    def _new_folder(self) -> None:
        dialog = BookmarkEditDialog(self, self.store, "New Folder", "New Folder", None, self._target_folder())
        if run_dialog(dialog):
            name, _url, folder = dialog.values()
            self.store.add_folder(name or "New Folder", folder)

    def _edit(self) -> None:
        ids = self._selected_ids()
        if ids:
            self.win.edit_bookmark(ids[0], self)

    def _delete(self) -> None:
        ids = [i for i in self._selected_ids() if i not in self.store.roots]
        folders = [i for i in ids if (self.store.node(i) or {}).get("type") == "folder"]
        if folders:
            answer = QMessageBox.question(self, "Delete Folders", "Delete the selected folders and everything inside them?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        for node_id in ids:
            self.store.remove(node_id)

    def _open_item(self, item, _column) -> None:
        node = self.store.node(item.data(0, Qt.ItemDataRole.UserRole))
        if node is not None and node["type"] == "url":
            self.win.open_url(QUrl(node["url"]), "tab")

    def _context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        menu = Menu("", self)
        node = self.store.node(item.data(0, Qt.ItemDataRole.UserRole)) if item else None
        if node is not None and node["type"] == "url":
            menu.addAction("Open in New Tab", lambda: self.win.open_url(QUrl(node["url"]), "tab"))
            menu.addSeparator()
        menu.addAction("New Bookmark…", self._new_bookmark)
        menu.addAction("New Folder…", self._new_folder)
        if node is not None and node["id"] not in self.store.roots:
            menu.addSeparator()
            menu.addAction("Edit…", self._edit)
            menu.addAction("Delete", self._delete)
        menu.exec(self.tree.viewport().mapToGlobal(pos))
        menu.deleteLater()


class HistoryDialog(QDialog):
    def __init__(self, win: "BrowserWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Library — History")
        self.resize(860, 560)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search history")
        self.search.textChanged.connect(lambda *_: self.reload())
        top.addWidget(self.search, 1)
        delete = make_button("Delete")
        delete.clicked.connect(lambda *_: self._delete())
        top.addWidget(delete)
        clear = make_button("Clear All History…", danger=True)
        clear.clicked.connect(lambda *_: self._clear())
        top.addWidget(clear)
        layout.addLayout(top)
        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setHeaderLabels(["Title", "Address", "Last Visited"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setColumnWidth(0, 330)
        self.tree.setColumnWidth(1, 330)
        self.tree.itemDoubleClicked.connect(lambda item, _c: win.open_url(QUrl(item.data(0, Qt.ItemDataRole.UserRole)), "tab"))
        layout.addWidget(self.tree, 1)
        self.reload()

    def reload(self) -> None:
        self.tree.clear()
        for url, title, last, _count in self.win.history.recent(self.search.text().strip(), 3000):
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(last))
            item = QTreeWidgetItem([title or url, url, stamp])
            item.setIcon(0, self.win.favicons.get(url))
            item.setData(0, Qt.ItemDataRole.UserRole, url)
            item.setToolTip(1, url)
            self.tree.addTopLevelItem(item)

    def _delete(self) -> None:
        urls = [item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()]
        if urls:
            self.win.history.delete(urls)
            self.reload()

    def _clear(self) -> None:
        if QMessageBox.question(self, "Clear History", "Remove all browsing history?") == QMessageBox.StandardButton.Yes:
            self.win.clear_history_traces()
            self.reload()


class ClearDataDialog(QDialog):
    def __init__(self, win: "BrowserWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Clear Browsing Data")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(tone_label("Choose what to clear:", "heading"))
        self.history = QCheckBox("Browsing history")
        self.cookies = QCheckBox("Cookies and site data (you'll be signed out of websites)")
        self.cache = QCheckBox("Cached images and files")
        self.history.setChecked(True)
        self.cache.setChecked(True)
        for box in (self.history, self.cookies, self.cache):
            layout.addWidget(box)
        layout.addWidget(tone_label("Your open tabs and bookmarks are not affected.", "dim"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Clear Now")
        buttons.accepted.connect(self._clear)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _clear(self) -> None:
        profile, win = self.win.profile, self.win
        if self.cookies.isChecked():  # first: history tells which sites may have stored data
            win.clear_site_data(None, lambda: win.toast("Browsing data cleared."))
        if self.history.isChecked():
            win.clear_history_traces()
        if self.cache.isChecked():
            profile.clearHttpCache()
        self.accept()
        win.toast("Clearing cookies and site data…" if self.cookies.isChecked() else "Browsing data cleared.")


class SiteSettingsDialog(QDialog):
    """Site settings: every site with cookies or permission decisions (searchable), and per site its permissions
    (Allow / Block / Ask), its cookies and a way to clear everything it stored."""
    CHOICES = (("Ask (default)", "ask"), ("Allow", "allow"), ("Block", "block"))

    def __init__(self, win: "BrowserWindow"):
        super().__init__(win)
        self.win = win
        self.index = CookieIndex.of(win.profile)
        self.site, self.origin = "", ""
        self.setWindowTitle("Site Settings")
        self.resize(660, 620)
        layout = QVBoxLayout(self)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._all_page())
        self.stack.addWidget(self._site_page())
        layout.addWidget(self.stack, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self._refresh_timer = QTimer(self)  # cookies change in bursts (a page load sets dozens)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self.refresh)
        self.index.changed.connect(self._refresh_timer.start)
        win.settings.changed.connect(self._settings_changed)

    def _settings_changed(self, key: str) -> None:
        if key == "site_permissions" and not sip.isdeleted(self):
            self._refresh_timer.start()

    # ── every site ──────────────────────────────────────────────────────────────────────
    def _all_page(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(tone_label("All sites", "title"))
        column.addWidget(tone_label("Sites that keep cookies in " + APP_NAME + ", or that you allowed or blocked from "
                                    "something. Cookies are kept when you quit, so you stay signed in.", "dim", wrap=True))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search sites")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda *_: self._fill_sites())
        column.addWidget(self.search)
        self.sites = QTreeWidget()
        self.sites.setRootIsDecorated(False)
        self.sites.setUniformRowHeights(True)
        self.sites.setHeaderLabels(["Site", "Cookies", "Permissions"])
        self.sites.setColumnWidth(0, 360)
        self.sites.itemDoubleClicked.connect(lambda item, _c: self.show_site(item.text(0)))
        column.addWidget(self.sites, 1)
        row = QHBoxLayout()
        details = make_button("Details…")
        details.clicked.connect(lambda *_: self.sites.currentItem() and self.show_site(self.sites.currentItem().text(0)))
        remove = make_button("Remove", danger=True)
        remove.setToolTip("Delete this site's cookies and stored data and forget its permissions")
        remove.clicked.connect(lambda *_: self.sites.currentItem() and self.remove_site(self.sites.currentItem().text(0)))
        remove_all = make_button("Remove All…", danger=True)
        remove_all.clicked.connect(lambda *_: self.remove_all())
        for widget in (details, remove):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(remove_all)
        column.addLayout(row)
        return page

    def site_rows(self) -> dict[str, list[int]]:
        """site -> [cookies, permission decisions]"""
        rows = {site: [count, 0] for site, count in self.index.sites().items()}
        for origin, _kind, _state in self.win.permission_decisions():
            rows.setdefault(site_of(QUrl(origin).host()), [0, 0])[1] += 1
        return rows

    def _fill_sites(self) -> None:
        current = self.sites.currentItem().text(0) if self.sites.currentItem() else ""
        words = self.search.text().lower().split()
        self.sites.clear()
        for site, (cookies, permissions) in sorted(self.site_rows().items()):
            if all(w in site for w in words):
                item = QTreeWidgetItem([site, str(cookies), str(permissions) if permissions else ""])
                item.setIcon(0, self.win.favicons.get(f"https://{site}/"))
                self.sites.addTopLevelItem(item)
                if site == current:
                    self.sites.setCurrentItem(item)

    def show_all(self) -> None:
        self._fill_sites()
        self.stack.setCurrentIndex(0)

    def remove_site(self, site: str) -> None:
        self.win.reset_site_permissions(site)
        self.win.clear_site_data(site, lambda: self.win.toast(f"Removed data for {site}."))
        self._refresh_timer.start()

    def remove_all(self) -> None:
        if ask_question(self, "Remove All Site Data", "Delete every site's cookies and stored data, and forget every "
                        "permission you gave or refused? You'll be signed out of websites.", "Remove All"):
            self.win.reset_site_permissions()
            self.win.clear_site_data(None, lambda: self.win.toast("Removed all site data."))
            self._refresh_timer.start()

    # ── one site ────────────────────────────────────────────────────────────────────────
    def _site_page(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        back = make_button("‹ All Sites")
        back.clicked.connect(lambda *_: self.show_all())
        top.addWidget(back)
        self.title = tone_label("", "title")
        top.addWidget(self.title, 1)
        column.addLayout(top)
        heading = QHBoxLayout()
        heading.addWidget(tone_label("Permissions", "heading"), 1)
        self.origins = QComboBox()
        self.origins.setToolTip("Permissions belong to each address (origin) of the site")
        self.origins.currentTextChanged.connect(lambda text: self._show_permissions(text))
        heading.addWidget(self.origins)
        column.addLayout(heading)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.choices: dict = {}
        for i, (kind, label) in enumerate(SITE_PERMISSIONS):
            box = QComboBox()
            for text, value in self.CHOICES:
                if not (value == "allow" and kind in ASK_ALWAYS):
                    box.addItem(text, value)
            box.currentIndexChanged.connect(lambda _i, k=kind, b=box: self._set_permission(k, b.currentData()))
            grid.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            grid.addWidget(box, i // 2, (i % 2) * 2 + 1)
            self.choices[kind] = box
        column.addLayout(grid)
        column.addSpacing(6)
        self.cookie_heading = tone_label("Cookies", "heading")
        column.addWidget(self.cookie_heading)
        self.cookie_list = QTreeWidget()
        self.cookie_list.setRootIsDecorated(False)
        self.cookie_list.setUniformRowHeights(True)
        self.cookie_list.setHeaderLabels(["Name", "Domain", "Expires"])
        self.cookie_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.cookie_list.setColumnWidth(0, 220)
        self.cookie_list.setColumnWidth(1, 220)
        column.addWidget(self.cookie_list, 1)
        row = QHBoxLayout()
        delete = make_button("Delete")
        delete.clicked.connect(lambda *_: self.delete_cookies(selected=True))
        delete_all = make_button("Delete All Cookies")
        delete_all.clicked.connect(lambda *_: self.delete_cookies())
        reset = make_button("Reset Permissions")
        reset.clicked.connect(lambda *_: (self.win.reset_site_permissions(self.site), self._show_permissions(self.origin)))
        clear = make_button("Clear Data for This Site", danger=True)
        clear.setToolTip("Cookies, local storage, databases, caches and service workers - you'll be signed out")
        clear.clicked.connect(lambda *_: self.clear_site())
        for widget in (delete, delete_all):
            row.addWidget(widget)
        row.addStretch(1)
        for widget in (reset, clear):
            row.addWidget(widget)
        column.addLayout(row)
        column.addWidget(tone_label("Clearing a site's data signs you out of it. Tabs showing the site reload.",
                                    "dim", wrap=True))
        return page

    def show_site(self, site: str, origin: str = "") -> None:
        self.site = site
        self.title.setText(site)
        origins = sorted(self.win.site_origins(site), key=lambda o: (o != origin, not o.startswith("https:"), o))
        if origin and origin not in origins:
            origins.insert(0, origin)
        origins = origins or [f"https://{site}"]
        self.origins.blockSignals(True)
        self.origins.clear()
        self.origins.addItems(origins)
        self.origins.blockSignals(False)
        self.origins.setVisible(len(origins) > 1)
        self._show_permissions(origins[0])
        self._fill_cookies()
        self.stack.setCurrentIndex(1)

    def _show_permissions(self, origin: str) -> None:
        self.origin = origin
        for kind, box in self.choices.items():
            box.blockSignals(True)
            box.setCurrentIndex(max(0, box.findData(self.win.permission_state(origin, kind))))
            box.blockSignals(False)

    def _set_permission(self, kind, state: str) -> None:
        if self.origin:
            self.win.set_permission_state(self.origin, kind, state)

    def _fill_cookies(self) -> None:
        selected = {tuple(i.data(0, Qt.ItemDataRole.UserRole)) for i in self.cookie_list.selectedItems()}
        cookies = self.index.for_site(self.site)
        self.cookie_heading.setText(f"Cookies ({len(cookies)})")
        self.cookie_list.clear()
        for cookie in cookies:
            expires = "Session" if cookie.isSessionCookie() else cookie.expirationDate().toLocalTime().toString("yyyy-MM-dd HH:mm")
            item = QTreeWidgetItem([bytes(cookie.name()).decode("utf-8", "replace"), cookie.domain().lstrip("."), expires])
            if cookie.isSessionCookie():
                item.setToolTip(2, "A session cookie: " + APP_NAME + " keeps it when you quit, so you stay signed in")
            item.setData(0, Qt.ItemDataRole.UserRole, list(CookieIndex.key(cookie)))
            item.setToolTip(0, f"{'Secure · ' if cookie.isSecure() else ''}{'HttpOnly · ' if cookie.isHttpOnly() else ''}"
                               f"path {cookie.path() or '/'}")
            self.cookie_list.addTopLevelItem(item)
            item.setSelected(CookieIndex.key(cookie) in selected)

    def delete_cookies(self, selected: bool = False) -> None:
        keys = [tuple(i.data(0, Qt.ItemDataRole.UserRole)) for i in self.cookie_list.selectedItems()] if selected else \
            [CookieIndex.key(c) for c in self.index.for_site(self.site)]
        lost = self.index.delete([c for c in map(self.index.cookies.get, keys) if c is not None])
        if lost:  # same name on a parent path or domain, value unknown: Chromium deletes those along with it
            self.win.toast("Also deleted: " + ", ".join(sorted({bytes(c.name()).decode("utf-8", "replace") + " on " +
                                                                c.domain().lstrip(".") + (c.path() or "/") for c in lost})))

    def clear_site(self) -> None:
        site = self.site
        self.win.clear_site_data(site, lambda: self.win.toast(f"Cleared data for {site}."))

    def refresh(self) -> None:
        if self.stack.currentIndex() == 0:
            self._fill_sites()
        else:
            self._fill_cookies()
            self._show_permissions(self.origin)


class ExtensionRow(QFrame):
    def __init__(self, entry: ExtensionEntry, win: "BrowserWindow"):
        super().__init__()
        self.setObjectName("ExtensionRow")
        controller = win.extensions
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)
        picture = QLabel()
        picture.setPixmap(entry.icon.pixmap(QSize(32, 32)))
        picture.setFixedSize(36, 36)
        layout.addWidget(picture, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        version = f"  <span style='color:{P.TEXT_3}'>{html.escape(entry.version)}</span>" if entry.version else ""
        name = QLabel(f"<b>{html.escape(entry.name)}</b>{version}")
        name.setTextFormat(Qt.TextFormat.RichText)
        text.addWidget(name)
        description = tone_label(elide(entry.description, 160), "secondary", wrap=True)
        text.addWidget(description)
        layout.addLayout(text, 1)
        controls = QVBoxLayout()
        controls.setSpacing(6)
        enabled = QCheckBox("Enabled")
        enabled.setChecked(entry.enabled)
        enabled.toggled.connect(lambda on: controller.set_enabled(entry.id, on))
        controls.addWidget(enabled)
        pinned = QCheckBox("Show in toolbar")
        pinned.setChecked(entry.pinned)
        pinned.toggled.connect(lambda on: controller.set_pinned(entry.id, on))
        controls.addWidget(pinned)
        if controller.wants_file_access(entry.id):  # off by default, as in Chrome: local files can be private
            files = QCheckBox("Allow access to file URLs")
            files.setChecked(controller.file_access(entry.id))
            files.toggled.connect(lambda on: controller.set_file_access(entry.id, on))
            controls.addWidget(files)
        layout.addLayout(controls)
        buttons = QVBoxLayout()
        buttons.setSpacing(6)
        if not entry.options_url.isEmpty():
            options = make_button("Options")
            options.clicked.connect(lambda *_: win.open_url(entry.options_url, "tab"))
            options.setEnabled(entry.enabled)  # a disabled extension's pages get no chrome.* APIs
            if not entry.enabled:
                options.setToolTip("Turn the extension on to change its options")
            buttons.addWidget(options)
        remove = make_button("Remove", danger=True)
        remove.clicked.connect(lambda *_: win.confirm_remove_extension(entry.id, entry.name))
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)


class ExtensionsDialog(QDialog):
    """The add-ons manager."""

    def __init__(self, win: "BrowserWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Extensions")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(tone_label("Manage your extensions", "title"))
        header.addStretch(1)
        store = make_button("Get Extensions from the Chrome Web Store", primary=True)
        store.clicked.connect(lambda *_: (win.open_url(QUrl(WEBSTORE_HOME), "tab"), self.close()))
        header.addWidget(store)
        layout.addLayout(header)
        actions = QHBoxLayout()
        install_file = make_button("Install from File (.crx / .zip)…")
        install_file.clicked.connect(lambda *_: win.install_extension_file())
        install_folder = make_button("Load Unpacked Folder…")
        install_folder.clicked.connect(lambda *_: win.install_extension_folder())
        actions.addWidget(install_file)
        actions.addWidget(install_folder)
        actions.addStretch(1)
        layout.addLayout(actions)
        note = tone_label(
            "Open an extension's page in the Chrome Web Store and click <b>Add to " + APP_NAME + "</b> in the bar "
            "that appears. Only Manifest V3 extensions are supported. " + APP_NAME + " fills in the extension APIs its "
            "engine lacks (toolbar badges, context menus, notifications, alarms, tabs, scripting), but some still don't "
            "work - ad blockers' network rules, for example, aren't enforced.",
            "dim", wrap=True, rich=True)
        layout.addWidget(note)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        layout.addWidget(self.area, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        # Rebuild on the next event-loop turn: changes often come from a checkbox inside this list, and
        # deleting a widget while it is still handling its own click crashes Qt.
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.timeout.connect(self.rebuild)
        win.extensions.changed.connect(self._schedule_rebuild)
        self.rebuild()

    def _schedule_rebuild(self) -> None:
        self._rebuild_timer.start(0)

    def done(self, result: int) -> None:
        try:
            self.win.extensions.changed.disconnect(self._schedule_rebuild)
        except (TypeError, RuntimeError):
            pass
        super().done(result)

    def rebuild(self) -> None:
        container = QWidget()
        column = QVBoxLayout(container)
        column.setContentsMargins(0, 0, 8, 0)
        column.setSpacing(4)
        if not self.win.extensions.available:
            column.addWidget(tone_label("Extensions need Qt WebEngine 6.10 or newer. Update with:\n"
                                        "python3 -m pip install --upgrade PyQt6 PyQt6-WebEngine", "error", wrap=True))
        else:
            entries = self.win.extensions.entries()
            if not entries:
                empty = tone_label("No extensions installed yet.", "dim")
                empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
                empty.setContentsMargins(0, 40, 0, 40)
                column.addWidget(empty)
            for entry in entries:
                column.addWidget(ExtensionRow(entry, self.win))
        column.addStretch(1)
        old = self.area.takeWidget()
        self.area.setWidget(container)
        if old is not None:
            old.deleteLater()


class SettingsDialog(QDialog):
    def __init__(self, win: "BrowserWindow"):
        super().__init__(win)
        self.win = win
        settings = win.settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        def section(text: str) -> None:
            layout.addSpacing(8)
            layout.addWidget(tone_label(text, "title"))

        section("Startup")
        restore = QCheckBox("Open previous tabs when " + APP_NAME + " starts")
        restore.setChecked(settings.get("restore_session"))
        restore.toggled.connect(lambda on: settings.set("restore_session", on))
        layout.addWidget(restore)
        home_row = QHBoxLayout()
        home_row.addWidget(QLabel("Home page and new windows"))
        self.home = QLineEdit(settings.get("homepage"))
        self.home.setPlaceholderText("New Tab page (leave empty) or a URL")
        self.home.editingFinished.connect(lambda: settings.set("homepage", self.home.text().strip()))
        home_row.addWidget(self.home, 1)
        layout.addLayout(home_row)

        section("Search")
        engine_row = QHBoxLayout()
        engine_row.addWidget(QLabel("Default search engine"))
        engine = QComboBox()
        engine.addItems(list(SEARCH_ENGINES))
        engine.setCurrentText(settings.get("search_engine"))
        engine.currentTextChanged.connect(lambda name: settings.set("search_engine", name))
        engine_row.addWidget(engine, 1)
        layout.addLayout(engine_row)

        section("Appearance")
        appearance_row = QHBoxLayout()
        appearance_row.addWidget(QLabel("Website appearance"))
        appearance = QComboBox()
        for label, value in (("Dark", "dark"), ("Light", "light"), ("Follow the system", "system")):
            appearance.addItem(label, value)
            if value == settings.get("website_appearance"):
                appearance.setCurrentIndex(appearance.count() - 1)
        appearance.currentIndexChanged.connect(lambda _i: (settings.set("website_appearance", appearance.currentData()),
                                                           self.restart_note.show()))
        appearance_row.addWidget(appearance, 1)
        layout.addLayout(appearance_row)
        self.restart_note = tone_label("Restart " + APP_NAME + " to apply the new website appearance.", "dim")
        self.restart_note.hide()
        layout.addWidget(self.restart_note)
        force_dark = QCheckBox("Force dark mode on every website (applies as pages load or reload)")
        force_dark.setChecked(settings.get("force_dark_pages"))
        force_dark.toggled.connect(lambda on: (settings.set("force_dark_pages", on), win.apply_force_dark()))
        layout.addWidget(force_dark)
        bookmarks_bar = QCheckBox("Show the bookmarks toolbar")
        bookmarks_bar.setChecked(settings.get("show_bookmarks_bar"))
        bookmarks_bar.toggled.connect(lambda on: win.set_bookmarks_bar_visible(on))
        layout.addWidget(bookmarks_bar)

        section("Downloads")
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Save files to"))
        self.folder_label = tone_label(self._short_path(settings.downloads_dir()), "secondary")
        self.folder_label.setToolTip(str(settings.downloads_dir()))
        folder_row.addWidget(self.folder_label, 1)
        browse = make_button("Choose…")
        browse.clicked.connect(lambda *_: self._choose_folder())
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)
        ask = QCheckBox("Always ask where to save files")
        ask.setChecked(settings.get("ask_download_location"))
        ask.toggled.connect(lambda on: settings.set("ask_download_location", on))
        layout.addWidget(ask)

        section("Network")
        network_row = QHBoxLayout()
        network_row.addWidget(tone_label(f"VPN / Proxy: {win.vpn.label}" if win.vpn else "VPN / Proxy: off",
                                         "secondary"), 1)
        vpn_button = make_button("VPN / Proxy Settings…")
        vpn_button.clicked.connect(lambda *_: (self.close(), win.show_vpn_panel()))
        network_row.addWidget(vpn_button)
        layout.addLayout(network_row)

        section("Privacy")
        privacy_row = QHBoxLayout()
        sites = make_button("Site Settings and Cookies…")
        sites.clicked.connect(lambda *_: win.show_site_settings(QUrl()))
        privacy_row.addWidget(sites)
        clear = make_button("Clear Browsing Data…")
        clear.clicked.connect(lambda *_: run_dialog(ClearDataDialog(win)))
        privacy_row.addWidget(clear)
        privacy_row.addStretch(1)
        layout.addLayout(privacy_row)
        layout.addWidget(tone_label("Cookies and website data are kept when you quit (including session cookies), so "
                                    "websites keep you signed in.", "dim", wrap=True))
        self.privacy_screen = QCheckBox(f"Hide pages when {APP_NAME} isn't in focus")
        self.privacy_screen.setToolTip(f"While another app is in front (or you switch desktops), {APP_NAME}'s windows "
                                       "turn grey, so nobody sees your pages in the app switcher or over your shoulder")
        self.privacy_screen.setChecked(settings.get("privacy_screen"))
        self.privacy_screen.toggled.connect(lambda on: settings.set("privacy_screen", on))
        layout.addWidget(self.privacy_screen)
        layout.addSpacing(6)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Save Downloads To", str(self.win.settings.downloads_dir()))
        if folder:
            self.win.settings.set("download_dir", folder)
            self.folder_label.setText(self._short_path(Path(folder)))
            self.folder_label.setToolTip(folder)

    @staticmethod
    def _short_path(path: Path) -> str:
        text = str(path)
        home = str(Path.home())
        if text == home or text.startswith(home + os.sep):
            text = "~" + text[len(home):]
        return text if len(text) <= 46 else text[:20] + "…" + text[-25:]

    def done(self, result: int) -> None:
        self.win.settings.set("homepage", self.home.text().strip())
        super().done(result)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  VPN / proxy
# ══════════════════════════════════════════════════════════════════════════════════════════
# A browser can't create a VPN server; it can route all of its traffic through one. Foxglove supports
# Tor and Cloudflare WARP (both free) and any HTTP/SOCKS5 proxy, applies the choice when it starts (the
# only leak-free way with Qt WebEngine), blocks WebRTC from revealing your real IP, and fails closed.
VPN_PRESETS = {
    "tor": {"label": "Tor", "host": "127.0.0.1", "ports": (9050, 9150),
            "help": "Free and anonymous, but slower. Keep Tor Browser open, or install Tor with "
                    "“brew install tor” and start it with “brew services start tor”.",
            "url": "https://www.torproject.org/download/"},
    "warp": {"label": "Cloudflare WARP", "host": "127.0.0.1", "ports": (40000,),
             "help": "Free and fast. Install the Cloudflare WARP app, then in WARP turn on Preferences › Advanced › "
                     "Configure Proxy (port 40000), switch to Local proxy mode and connect.",
             "url": "https://one.one.one.one/"},
}
PROXY_ERROR_CODES = {-111, -115, -120, -121, -127, -130}  # proxy/tunnel/SOCKS connection failures


@dataclass
class VpnEndpoint:
    mode: str
    kind: str  # "socks5" or "http"
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def label(self) -> str:
        name = VPN_PRESETS[self.mode]["label"] if self.mode in VPN_PRESETS else "Your proxy"
        return f"{name} ({self.host}:{self.port})"


def sanitize_vpn(raw) -> dict:
    cfg = {"mode": "off", "type": "socks5", "host": "", "port": 1080, "username": "", "password": ""}
    if isinstance(raw, dict):
        if raw.get("mode") in ("off", "tor", "warp", "custom"):
            cfg["mode"] = raw["mode"]
        if raw.get("type") in ("socks5", "http"):
            cfg["type"] = raw["type"]
        for key in ("host", "username", "password"):
            if isinstance(raw.get(key), str):
                cfg[key] = raw[key].strip() if key == "host" else raw[key]
        port = raw.get("port")
        if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
            cfg["port"] = port
    return cfg


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def vpn_endpoint(cfg: dict, probe: bool = True) -> VpnEndpoint | None:
    """The proxy to use for *cfg*, or None to connect directly (system network settings)."""
    mode = cfg["mode"]
    if mode in VPN_PRESETS:
        preset = VPN_PRESETS[mode]
        port = next((p for p in preset["ports"] if probe and port_open(preset["host"], p)), preset["ports"][0])
        return VpnEndpoint(mode, "socks5", preset["host"], port)
    if mode == "custom" and cfg["host"]:
        http = cfg["type"] == "http"  # Chromium can't log in to SOCKS5 proxies, only to HTTP ones
        return VpnEndpoint(mode, cfg["type"], cfg["host"], cfg["port"],
                           cfg["username"] if http else "", cfg["password"] if http else "")
    return None


class VpnPanel(Panel):
    """Choose how Chrome 2 connects: directly, through Tor, Cloudflare WARP or your own proxy server."""

    def __init__(self, win: "BrowserWindow"):
        super().__init__(win, pinned=True)  # stays open while you copy details from elsewhere
        self.win = win
        self.check_page: QWebEnginePage | None = None
        cfg = sanitize_vpn(win.settings.get("vpn"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.addWidget(tone_label("VPN / Proxy", "title"))
        top.addStretch(1)
        active = win.vpn
        top.addWidget(tone_label(f"On · {VPN_PRESETS[active.mode]['label'] if active.mode in VPN_PRESETS else 'Your proxy'}"
                                 if active else "Off", "accent" if active else "dim"))
        layout.addLayout(top)
        layout.addWidget(tone_label(f"Send {APP_NAME}'s traffic through:", "secondary"))
        self.group = QButtonGroup(self)
        self.radios: dict[str, QRadioButton] = {}
        for mode, title, help_text in (
                ("off", "Off — connect directly", "Uses your normal connection (and system proxy settings)."),
                ("tor", "Tor", VPN_PRESETS["tor"]["help"]),
                ("warp", "Cloudflare WARP", VPN_PRESETS["warp"]["help"]),
                ("custom", "My own proxy server", "An HTTP or SOCKS5 proxy, e.g. from your VPN provider.")):
            radio = QRadioButton(title)
            self.group.addButton(radio)
            self.radios[mode] = radio
            layout.addWidget(radio)
            note = tone_label(help_text, "dim", wrap=True)
            note.setContentsMargins(26, 0, 0, 4)
            layout.addWidget(note)
        self.radios[cfg["mode"]].setChecked(True)
        self.custom = QWidget()
        grid = QGridLayout(self.custom)
        grid.setContentsMargins(26, 0, 0, 4)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        self.kind = QComboBox()
        self.kind.addItem("SOCKS5", "socks5")
        self.kind.addItem("HTTP", "http")
        self.kind.setCurrentIndex(0 if cfg["type"] == "socks5" else 1)
        self.host = QLineEdit(cfg["host"])
        self.host.setPlaceholderText("proxy.example.com")
        self.port = QLineEdit(str(cfg["port"]))
        self.port.setValidator(QIntValidator(1, 65535, self.port))
        self.port.setFixedWidth(72)
        self.user = QLineEdit(cfg["username"])
        self.user.setPlaceholderText("Username (optional)")
        self.password = QLineEdit(cfg["password"])
        self.password.setPlaceholderText("Password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        grid.addWidget(self.kind, 0, 0)
        grid.addWidget(self.host, 0, 1)
        grid.addWidget(self.port, 0, 2)
        grid.addWidget(self.user, 1, 0, 1, 2)
        grid.addWidget(self.password, 1, 2)
        layout.addWidget(self.custom)
        line = QFrame()
        line.setObjectName("PanelSeparator")
        line.setFixedHeight(1)
        layout.addSpacing(4)
        layout.addWidget(line)
        check_row = QHBoxLayout()
        self.check_button = make_button("Check Connection")
        self.check_button.setToolTip("Shows the IP address and country websites see right now")
        self.check_button.clicked.connect(lambda *_: self._check())
        check_row.addWidget(self.check_button)
        check_row.addStretch(1)
        layout.addLayout(check_row)
        self.result = tone_label("", "secondary", wrap=True)
        self.result.hide()
        layout.addWidget(self.result)
        layout.addWidget(tone_label(f"Only {APP_NAME}'s own traffic uses the VPN. Applying restarts {APP_NAME}; your tabs "
                                    "and logins come back.", "dim", wrap=True))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = make_button("Cancel")
        cancel.clicked.connect(lambda *_: self.close())
        self.apply_button = make_button("Apply && Restart", primary=True)
        self.apply_button.clicked.connect(lambda *_: self._apply())
        buttons.addWidget(cancel)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)
        self.group.buttonToggled.connect(lambda *_: self._sync())
        self.kind.currentIndexChanged.connect(lambda *_: self._sync())
        self.setFixedWidth(440)
        self._sync()

    def _sync(self) -> None:
        custom = self.radios["custom"].isChecked()
        self.custom.setEnabled(custom)
        http = self.kind.currentData() == "http"
        for field in (self.user, self.password):
            field.setEnabled(custom and http)
            field.setToolTip("" if http else "Chromium can't log in to SOCKS5 proxies - use an HTTP proxy for that.")

    def _config(self) -> dict:
        mode = next(m for m, radio in self.radios.items() if radio.isChecked())
        port = self.port.text().strip()
        return sanitize_vpn({"mode": mode, "type": self.kind.currentData(), "host": self.host.text(),
                             "port": int(port) if port.isdigit() else 0,
                             "username": self.user.text(), "password": self.password.text()})

    def _show_result(self, text: str, error: bool = False) -> None:
        self.result.setProperty("tone", "error" if error else "secondary")
        self.result.style().unpolish(self.result)
        self.result.style().polish(self.result)
        self.result.setText(text)
        self.result.show()
        self.adjustSize()
        self.reposition()

    def _apply(self) -> None:
        cfg = self._config()
        if cfg["mode"] == "custom" and (not cfg["host"] or not self.port.hasAcceptableInput()):
            self._show_result("Enter your proxy server's address and port.", error=True)
            return
        current = sanitize_vpn(self.win.settings.get("vpn"))
        if cfg == current and (vpn_endpoint(cfg, probe=False) is None) == (self.win.vpn is None):
            self.close()  # nothing changed
            return
        endpoint = vpn_endpoint(cfg, probe=False)
        if endpoint is None:
            self._commit(cfg)
            return
        ports = VPN_PRESETS[cfg["mode"]]["ports"] if cfg["mode"] in VPN_PRESETS else (endpoint.port,)
        self.apply_button.setEnabled(False)
        self._show_result(f"Checking that {endpoint.host} is reachable…")
        relay = _Relay(self)  # dies with the panel, so a closed panel never restarts the browser
        relay.done.connect(lambda ok: self._after_probe(cfg, ok))

        def probe() -> None:
            ok = any(port_open(endpoint.host, port, 3.0) for port in ports)
            try:
                relay.done.emit(ok)
            except RuntimeError:
                pass

        threading.Thread(target=probe, daemon=True).start()

    def _after_probe(self, cfg: dict, reachable: bool) -> None:
        if not reachable:
            self.apply_button.setEnabled(True)
            name = VPN_PRESETS[cfg["mode"]]["label"] if cfg["mode"] in VPN_PRESETS else "your proxy server"
            self._show_result(f"Can't reach {name}. Make sure it's running, then try again.", error=True)
            return
        self._commit(cfg)

    def _commit(self, cfg: dict) -> None:
        self.win.settings.set("vpn", cfg)
        self.close()
        QTimer.singleShot(0, self.win.restart_browser)

    def _check(self) -> None:
        self.check_button.setEnabled(False)
        self._show_result("Checking…")
        if self.check_page is None:
            self.check_page = QWebEnginePage(self.win.profile, self)
            self.check_page.loadFinished.connect(self._on_check_loaded)
        self._check_step = "trace"
        self._check_info: dict = {}
        self.check_page.load(QUrl("https://www.cloudflare.com/cdn-cgi/trace"))
        QTimer.singleShot(30000, self._check_timeout)

    def _check_timeout(self) -> None:
        if not sip.isdeleted(self) and not self.check_button.isEnabled():
            self.check_page.triggerAction(QWebEnginePage.WebAction.Stop)
            self.check_button.setEnabled(True)
            self._show_result("No answer - this connection isn't working right now.", error=True)

    def _on_check_loaded(self, ok: bool) -> None:
        if self.check_button.isEnabled():
            return  # timed out already
        if not ok:
            self.check_button.setEnabled(True)
            self._show_result("Couldn't reach the internet through this connection.", error=True)
            return
        self.check_page.runJavaScript("document.body ? document.body.innerText : ''", self._on_check_text)

    def _on_check_text(self, text) -> None:
        if sip.isdeleted(self) or self.check_button.isEnabled():
            return
        text = text if isinstance(text, str) else ""
        if self._check_step == "trace":
            self._check_info = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            if self.win.vpn is not None and self.win.vpn.mode == "tor":
                self._check_step = "tor"
                self.check_page.load(QUrl("https://check.torproject.org/api/ip"))
                return
        else:
            try:
                self._check_info["tor"] = bool(json.loads(text).get("IsTor"))
            except (ValueError, AttributeError):
                self._check_info["tor"] = False
        self.check_button.setEnabled(True)
        info = self._check_info
        ip = info.get("ip")
        if not ip:
            self._show_result("Couldn't read the answer - try again.", error=True)
            return
        country = QLocale.territoryToString(QLocale.codeToTerritory(info.get("loc", ""))) if info.get("loc") else ""
        lines = [f"Websites see you at {ip}" + (f" in {country}." if country and country != "Unknown" else ".")]
        if info.get("warp") in ("on", "plus"):
            lines.append("Cloudflare WARP is protecting this connection.")
        if "tor" in info:
            lines.append("Tor is protecting this connection." if info["tor"] else "Warning: this traffic is NOT going through Tor.")
        if self.win.vpn is None:
            lines.append("The VPN is off, so this is your real address.")
        self._show_result(" ".join(lines), error="tor" in info and not info["tor"])


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Bookmarks toolbar
# ══════════════════════════════════════════════════════════════════════════════════════════
class BookmarkButton(QToolButton):
    """A bookmarks-toolbar item; middle-click / Ctrl-click opens in a background tab."""

    def __init__(self, win: "BrowserWindow", node: dict):
        super().__init__()
        self.win = win
        self.node = node
        self.setObjectName("BookmarkItem")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(16, 16))
        self.setFixedHeight(26)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        title = node["title"] or node.get("url", "")
        self.setText(self.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, 150).replace("&", "&&"))
        if node["type"] == "folder":
            self.setIcon(icon("folder", P.TEXT_2))
            self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = Menu("", self)
            menu.aboutToShow.connect(lambda: win.fill_bookmark_menu(menu, node["id"]))
            self.setMenu(menu)
            self.setToolTip(title)
        else:
            self.setIcon(win.favicons.get(node["url"]))
            self.setToolTip(f"{title}\n{node['url']}")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: win.bookmark_context_menu(node["id"], self.mapToGlobal(pos)))

    def mouseReleaseEvent(self, event) -> None:
        if self.node["type"] == "url" and self.rect().contains(event.position().toPoint()):
            background = event.button() == Qt.MouseButton.MiddleButton or (
                event.button() == Qt.MouseButton.LeftButton and event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if background:
                self.setDown(False)
                self.win.open_url(QUrl(self.node["url"]), "background")
                return
            if event.button() == Qt.MouseButton.LeftButton:
                super().mouseReleaseEvent(event)
                self.win.open_url(QUrl(self.node["url"]), "current")
                return
        super().mouseReleaseEvent(event)


class BookmarksBar(QWidget):
    def __init__(self, win: "BrowserWindow"):
        super().__init__(win)
        self.win = win
        self.setObjectName("BookmarksBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFixedHeight(32)
        self.layout_ = QHBoxLayout(self)
        self.layout_.setContentsMargins(6, 2, 6, 4)
        self.layout_.setSpacing(2)
        self.buttons: list[BookmarkButton] = []
        self.overflow = tool_button(icon("chevrons-right"), "More bookmarks", 26)
        self.overflow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.overflow_menu = Menu("", self.overflow)
        self.overflow.setMenu(self.overflow_menu)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: win.bookmark_context_menu(None, self.mapToGlobal(pos)))
        win.bookmarks.changed.connect(self.rebuild)
        win.favicons.updated.connect(self._refresh_icons)
        self.rebuild()

    def _refresh_icons(self) -> None:
        for button in self.buttons:
            if button.node["type"] == "url":
                button.setIcon(self.win.favicons.get(button.node["url"]))

    def rebuild(self) -> None:
        while self.layout_.count():
            item = self.layout_.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.overflow:
                widget.deleteLater()
        self.buttons = [BookmarkButton(self.win, node) for node in self.win.bookmarks.children("toolbar")]
        for button in self.buttons:
            self.layout_.addWidget(button)
        self.layout_.addStretch(1)
        self.layout_.addWidget(self.overflow)
        self._fit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        available = self.width() - 12 - self.overflow.sizeHint().width() - 8
        used = 0
        hidden: list[BookmarkButton] = []
        for button in self.buttons:
            width = button.sizeHint().width() + self.layout_.spacing()
            fits = not hidden and used + width <= available
            button.setVisible(fits)
            if fits:
                used += width
            else:
                hidden.append(button)
        self.overflow.setVisible(bool(hidden))
        reset_menu(self.overflow_menu)
        for button in hidden:
            self.win.add_bookmark_node_to_menu(self.overflow_menu, button.node)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  The browser window
# ══════════════════════════════════════════════════════════════════════════════════════════
class ExtensionButton(QToolButton):
    """An extension's toolbar button, with the badge chrome.action gives it."""

    def __init__(self, entry: ExtensionEntry):
        super().__init__()
        self.ext_id, self.name, self.default_icon = entry.id, entry.name, entry.icon
        self.badge, self.badge_colors = "", (QColor(*BADGE_COLOR), QColor("white"))
        self.setIcon(entry.icon)
        self.setIconSize(QSize(16, 16))
        self.setToolTip(plain_tip(entry.name))
        self.setAutoRaise(True)
        self.setFixedSize(32, 32)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_badge(self, text: str, background: QColor, foreground: QColor) -> None:
        if (text, background, foreground) != (self.badge, *self.badge_colors):
            self.badge, self.badge_colors = text, (background, foreground)
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.badge:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPixelSize(9)
        font.setBold(True)
        painter.setFont(font)
        text = self.badge[:4]
        width = min(self.width() - 2, max(13, painter.fontMetrics().horizontalAdvance(text) + 6))
        rect = QRectF(self.width() - width - 1, self.height() - 14, width, 12)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.badge_colors[0])
        painter.drawRoundedRect(rect, 3, 3)
        painter.setPen(self.badge_colors[1])
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


class BrowserWindow(QMainWindow):
    def __init__(self, profile: QWebEngineProfile, settings: Settings, bookmarks: BookmarkStore,
                 history: HistoryStore, favicons: FaviconCache, extensions: ExtensionsController,
                 session_path: Path, startup_urls: list[str], vpn: VpnEndpoint | None = None,
                 restart_request: dict | None = None):
        super().__init__()
        self.vpn = vpn  # the VPN/proxy this run was started with (None = direct)
        self.restart_request = restart_request if restart_request is not None else {"requested": False}
        self._vpn_warned = False
        self.profile = profile
        self.cookie_index = CookieIndex.of(profile)  # (main() made it already, before any page)
        self._cleaners: list[SiteDataCleaner] = []  # clearing site data, still running
        self.settings = settings
        self.bookmarks = bookmarks
        self.history = history
        self.favicons = favicons
        self.extensions = extensions
        self.session_path = session_path
        self.closed_tabs: list[dict] = []
        self.downloads: list[DownloadItem] = []
        self.popups: set[PopupWindow] = set()
        self.certificate_exceptions: set[str] = set()
        self._closing = False
        self._force_close = False
        self._fullscreen_tab: Tab | None = None
        self._state_before_fullscreen = Qt.WindowState.WindowNoState
        self._printer: QPrinter | None = None
        self._dialogs: dict[str, QDialog] = {}
        self._command_actions: list[QAction] = []
        self._last_session = ""
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icons().logo())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        central = QWidget(self)
        self.setCentralWidget(central)
        column = QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.tab_strip = TabStrip(central)
        self.tab_bar = self.tab_strip.tabbar
        self.nav_bar = self._build_nav_bar()
        self.bookmarks_bar = BookmarksBar(self)
        self.separator = QFrame()
        self.separator.setObjectName("ChromeSeparator")
        self.separator.setFixedHeight(1)
        self.content = ContentArea(central)
        self.stack = self.content.stack
        self.find_bar = FindBar(self)
        for widget in (self.tab_strip, self.nav_bar, self.bookmarks_bar, self.separator):
            column.addWidget(widget)
        column.addWidget(self.content, 1)
        column.addWidget(self.find_bar)
        self._sync_bookmarks_bar()
        bookmarks.changed.connect(self._sync_bookmarks_bar)

        self.tab_bar.currentChanged.connect(self._on_current_changed)
        self.tab_bar.tabCloseRequested.connect(lambda index: self.close_tab(self.tab_at(index)))
        self.tab_bar.tabMoved.connect(lambda *_: self.schedule_session_save())
        self.tab_bar.newTabRequested.connect(self.open_new_tab)
        self.tab_bar.audioClicked.connect(lambda index: self.toggle_mute(self.tab_at(index)))
        self.tab_bar.customContextMenuRequested.connect(self._tab_context_menu)
        self.tab_strip.new_tab_button.clicked.connect(lambda *_: self.open_new_tab())
        self.tab_strip.list_button.setMenu(Menu("", self.tab_strip.list_button))
        self.tab_strip.list_button.menu().aboutToShow.connect(self._fill_tab_list)

        self._create_actions()
        if IS_MAC:
            self._build_mac_menubar()
        bookmarks.changed.connect(self._update_star)
        extensions.attach_window(self)
        extensions.changed.connect(self._rebuild_extension_buttons)
        extensions.changed.connect(self._rewire_tabs)
        extensions.message.connect(lambda text, kind: self.toast(text, kind))
        extensions.reloaded.connect(self._on_extension_reloaded)
        extensions.replace_requested.connect(self._confirm_replace_extension)
        extensions.bridge.action_changed.connect(lambda ext_id: self._refresh_extension_buttons(ext_id))
        profile.downloadRequested.connect(self._on_download_requested)
        self._session_timer = QTimer(self)
        self._session_timer.setSingleShot(True)
        self._session_timer.setInterval(1000)
        self._session_timer.timeout.connect(self.save_session)
        self._autosave = QTimer(self)
        self._autosave.setInterval(15_000)
        self._autosave.timeout.connect(self.save_session)
        self._autosave.start()
        self._rebuild_extension_buttons()
        self._restore(startup_urls)
        self.privacy_screen = PrivacyScreen(self, settings)

    # ── construction ────────────────────────────────────────────────────────────────────
    def _build_nav_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("NavBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        bar.setFixedHeight(44)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        self.back_button = tool_button(icon("back"), "Go back one page (right-click or hold to see history)")
        self.forward_button = tool_button(icon("forward"), "Go forward one page (right-click or hold to see history)")
        for button, back in ((self.back_button, True), (self.forward_button, False)):
            menu = Menu("", button)
            menu.aboutToShow.connect(lambda m=menu, b=back: self._fill_history_menu(m, b))
            button.setMenu(menu)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.DelayedPopup)
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(lambda _pos, b=button: b.showMenu())
        self.back_button.clicked.connect(lambda *_: self._page_action(QWebEnginePage.WebAction.Back))
        self.forward_button.clicked.connect(lambda *_: self._page_action(QWebEnginePage.WebAction.Forward))
        self.reload_button = tool_button(icon("reload"), f"Reload current page ({shortcut_text('Ctrl+R')})")
        self.reload_button.clicked.connect(lambda *_: self.reload_or_stop())
        for widget in (self.back_button, self.forward_button, self.reload_button):
            layout.addWidget(widget)
        layout.addSpacing(6)
        self.url_bar = UrlBar(self)
        self.url_bar.navigate.connect(self._navigate_from_url_bar)
        self.url_bar.star.triggered.connect(lambda *_: self.bookmark_current_page())
        self.url_bar.identity.triggered.connect(lambda *_: self._show_site_info())
        self.url_bar.zoom_action.triggered.connect(lambda *_: self.zoom_reset())
        layout.addWidget(self.url_bar, 1)
        layout.addSpacing(6)
        self.vpn_button = tool_button(icon("shield", P.TEXT_2), "VPN / Proxy")
        self.vpn_button.clicked.connect(lambda *_: self.show_vpn_panel(toggle=True))
        layout.addWidget(self.vpn_button)
        self._update_vpn_button()
        self.download_button = DownloadButton()
        self.download_button.clicked.connect(lambda *_: self.show_downloads())
        self.download_button.hide()
        layout.addWidget(self.download_button)
        self.extension_buttons = QHBoxLayout()
        self.extension_buttons.setSpacing(2)
        layout.addLayout(self.extension_buttons)
        self.extensions_button = tool_button(icon("puzzle"), "Extensions")
        self.extensions_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        extensions_menu = Menu("", self.extensions_button)
        extensions_menu.aboutToShow.connect(lambda: self._fill_extensions_menu(extensions_menu))
        self.extensions_button.setMenu(extensions_menu)
        layout.addWidget(self.extensions_button)
        self.menu_button = tool_button(icon("menu"), "Open application menu")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        layout.addWidget(self.menu_button)
        return bar

    def _action(self, text: str, slot, shortcuts=(), role: QAction.MenuRole = QAction.MenuRole.NoRole,
                checkable: bool = False) -> QAction:
        action = QAction(text, self)
        action.setMenuRole(role)
        action.setCheckable(checkable)
        sequences: list[QKeySequence] = []
        for item in shortcuts if isinstance(shortcuts, (list, tuple)) else [shortcuts]:
            if isinstance(item, QKeySequence.StandardKey):
                sequences += QKeySequence.keyBindings(item)
            elif isinstance(item, QKeySequence):
                sequences.append(item)
            elif item:
                sequences.append(QKeySequence(item))
        unique: list[QKeySequence] = []
        for seq in sequences:
            if not seq.isEmpty() and all(seq != other for other in unique):
                unique.append(seq)
        if unique:
            action.setShortcuts(unique)
        action.triggered.connect(lambda *_: slot())
        self.addAction(action)
        return action

    def _create_actions(self) -> None:
        mac = IS_MAC
        ctrl_shift_tab = QKeySequence(Qt.Modifier.CTRL | Qt.Modifier.SHIFT | Qt.Key.Key_Backtab)
        meta_shift_tab = QKeySequence(Qt.Modifier.META | Qt.Modifier.SHIFT | Qt.Key.Key_Backtab)
        a = self._action
        self.act_new_tab = a("New Tab", self.open_new_tab, ["Ctrl+T"])
        self.act_close_tab = a("Close Tab", lambda: self.close_tab(self.current_tab()), ["Ctrl+W"] + ([] if mac else ["Ctrl+F4"]))
        self.act_reopen = a("Reopen Closed Tab", self.reopen_closed_tab, ["Ctrl+Shift+T"])
        self.act_open_file = a("Open File…", self.open_file, ["Ctrl+O"])
        self.act_next_tab = a("Next Tab", lambda: self.cycle_tab(1),
                              ["Meta+Tab", "Ctrl+Alt+Right", "Ctrl+}", "Meta+PgDown"] if mac else ["Ctrl+Tab", "Ctrl+PgDown"])
        self.act_prev_tab = a("Previous Tab", lambda: self.cycle_tab(-1),
                              [meta_shift_tab, "Meta+Shift+Tab", "Ctrl+Alt+Left", "Ctrl+{", "Meta+PgUp"] if mac
                              else [ctrl_shift_tab, "Ctrl+Shift+Tab", "Ctrl+PgUp"])
        for number in range(1, 10):
            a(f"Select Tab {number}", lambda n=number: self.select_tab_number(n), [f"Ctrl+{number}"])
        self.act_focus_url = a("Open Location…", self.focus_url_bar, ["Ctrl+L", "F6", "Ctrl+K"] + ([] if mac else ["Alt+D", "Ctrl+E"]))
        self.act_reload = a("Reload", self.reload, ["Ctrl+R", "F5"])
        self.act_hard_reload = a("Reload (Override Cache)", self.hard_reload, ["Ctrl+Shift+R"] + ([] if mac else ["Ctrl+F5"]))
        self.act_back = a("Back", lambda: self._page_action(QWebEnginePage.WebAction.Back), ["Ctrl+["] if mac else ["Alt+Left"])
        self.act_forward = a("Forward", lambda: self._page_action(QWebEnginePage.WebAction.Forward), ["Ctrl+]"] if mac else ["Alt+Right"])
        self.act_find = a("Find in Page…", self.find_bar.open, ["Ctrl+F"])
        self.act_find_next = a("Find Again", lambda: self._find_again(False), ["Ctrl+G", "F3"])
        self.act_find_prev = a("Find Previous", lambda: self._find_again(True), ["Ctrl+Shift+G", "Shift+F3"])
        self.act_zoom_in = a("Zoom In", self.zoom_in, ["Ctrl++", "Ctrl+="])
        self.act_zoom_out = a("Zoom Out", self.zoom_out, ["Ctrl+-"])
        self.act_zoom_reset = a("Actual Size", self.zoom_reset, ["Ctrl+0"])
        self.act_bookmark = a("Bookmark Current Tab…", self.bookmark_current_page, ["Ctrl+D"])
        self.act_bookmarks_bar = a("Bookmarks Toolbar", lambda: self.set_bookmarks_bar_visible(not self.settings.get("show_bookmarks_bar")),
                                   ["Ctrl+Shift+B"], checkable=True)
        self.act_bookmarks_bar.setChecked(self.settings.get("show_bookmarks_bar"))
        self.act_manage_bookmarks = a("Manage Bookmarks", self.show_bookmarks_manager, ["Ctrl+Shift+O"])
        self.act_history = a("Show All History", self.show_history, ["Ctrl+Shift+H"] + ([] if mac else ["Ctrl+H"]))
        self.act_clear_data = a("Clear Recent History…", lambda: run_dialog(ClearDataDialog(self)),
                                ["Ctrl+Shift+Backspace"] if mac else ["Ctrl+Shift+Del"])
        self.act_downloads = a("Downloads", self.show_downloads, ["Ctrl+Shift+Y"])
        self.act_extensions = a("Extensions and Themes", self.show_extensions, ["Ctrl+Shift+A"])
        self.act_vpn = a("VPN / Proxy…", self.show_vpn_panel)
        self.act_print = a("Print…", self.print_page, ["Ctrl+P"])
        self.act_save = a("Save Page As…", lambda: self._page_action(QWebEnginePage.WebAction.SavePage), ["Ctrl+S"])
        self.act_source = a("View Page Source", self.view_source, ["Ctrl+U"])
        self.act_devtools = a("Web Developer Tools", self.toggle_devtools, ["F12"] + (["Ctrl+Alt+I"] if mac else ["Ctrl+Shift+I"]))
        self.act_fullscreen = a("Full Screen", self.toggle_fullscreen, ["Ctrl+Meta+F"] if mac else ["F11"])
        self.act_exit_fullscreen = a("Exit Full Screen", self._exit_html_fullscreen, ["Esc"])
        self.act_exit_fullscreen.setEnabled(False)
        self.act_mute = a("Mute Tab", lambda: self.toggle_mute(self.current_tab()), [] if mac else ["Ctrl+M"])
        self.act_settings = a("Settings", self.show_settings, ["Ctrl+,"], role=QAction.MenuRole.PreferencesRole)
        self.act_site_settings = a("Site Settings and Cookies…", lambda: self.show_site_settings())
        self.act_about = a(f"About {APP_NAME}", self.show_about, [], role=QAction.MenuRole.AboutRole)
        self.act_shortcuts = a("Keyboard Shortcuts", self.show_shortcuts)
        self.act_quit = a(f"Quit {APP_NAME}" if mac else "Exit", self.close, [QKeySequence.StandardKey.Quit, "Ctrl+Q"],
                          role=QAction.MenuRole.QuitRole)
        self.menu_button.setMenu(self._build_app_menu())

    def _build_app_menu(self) -> QMenu:
        menu = Menu("", self.menu_button)
        menu.addAction(self.act_new_tab)
        menu.addAction(self.act_reopen)
        menu.addSeparator()
        bookmarks = menu.submenu("Bookmarks")
        bookmarks.aboutToShow.connect(lambda: self._fill_bookmarks_menu(bookmarks))
        history = menu.submenu("History")
        history.aboutToShow.connect(lambda: self._fill_history_app_menu(history))
        menu.addAction(self.act_downloads)
        menu.addAction(self.act_extensions)
        menu.addAction(self.act_vpn)
        menu.addSeparator()
        menu.addAction(self.act_print)
        menu.addAction(self.act_save)
        menu.addAction(self.act_find)
        zoom_row = QWidgetAction(menu)
        zoom_row.setDefaultWidget(self._zoom_widget())
        menu.addAction(zoom_row)
        menu.addSeparator()
        menu.addAction(self.act_settings)
        menu.addAction(self.act_site_settings)
        tools = menu.submenu("More Tools")
        tools.addAction(self.act_devtools)
        tools.addAction(self.act_source)
        tools.addAction(self.act_clear_data)
        help_menu = menu.submenu("Help")
        help_menu.addAction(self.act_shortcuts)
        help_menu.addAction(self.act_about)
        menu.addSeparator()
        menu.addAction(self.act_quit)
        menu.aboutToShow.connect(self._update_zoom_widget)
        return menu

    def _zoom_widget(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(19, 2, 10, 2)
        layout.addWidget(QLabel("Zoom"))
        layout.addStretch(1)
        out = tool_button(QIcon(), "Zoom out", 30)
        out.setText("−")
        out.clicked.connect(lambda *_: self.zoom_out())
        self.zoom_label = QPushButton("100%")
        self.zoom_label.setFlat(True)
        self.zoom_label.setFixedWidth(64)
        self.zoom_label.setToolTip("Reset zoom")
        self.zoom_label.setStyleSheet("QPushButton { background: transparent; min-width: 0; padding: 4px; }"
                                      "QPushButton:hover { background: rgba(251,251,254,0.1); }")
        self.zoom_label.clicked.connect(lambda *_: self.zoom_reset())
        zoom_in = tool_button(QIcon(), "Zoom in", 30)
        zoom_in.setText("+")
        zoom_in.clicked.connect(lambda *_: self.zoom_in())
        full = tool_button(icon("fullscreen"), "Full screen", 30)
        full.clicked.connect(lambda *_: (self.menu_button.menu().close(), self.toggle_fullscreen()))
        for widget in (out, self.zoom_label, zoom_in, full):
            layout.addWidget(widget)
        return row

    def _build_mac_menubar(self) -> None:
        self.mac_menubar = QMenuBar(None)  # the global macOS menu bar
        file_menu = self.mac_menubar.addMenu("File")
        for item in (self.act_new_tab, self.act_open_file, None, self.act_close_tab, self.act_reopen, None,
                     self.act_save, self.act_print, None, self.act_quit):
            file_menu.addSeparator() if item is None else file_menu.addAction(item)
        view_menu = self.mac_menubar.addMenu("View")
        for item in (self.act_bookmarks_bar, None, self.act_reload, self.act_hard_reload, None, self.act_zoom_in,
                     self.act_zoom_out, self.act_zoom_reset, None, self.act_fullscreen, None, self.act_source,
                     self.act_devtools):
            view_menu.addSeparator() if item is None else view_menu.addAction(item)
        history_menu = self.mac_menubar.addMenu("History")
        history_menu.aboutToShow.connect(lambda: self._fill_history_app_menu(history_menu, include_navigation=True))
        bookmarks_menu = self.mac_menubar.addMenu("Bookmarks")
        bookmarks_menu.aboutToShow.connect(lambda: self._fill_bookmarks_menu(bookmarks_menu))
        tools_menu = self.mac_menubar.addMenu("Tools")
        for item in (self.act_downloads, self.act_extensions, self.act_vpn, None, self.act_find, self.act_find_next, None,
                     self.act_clear_data, self.act_site_settings, self.act_settings):
            tools_menu.addSeparator() if item is None else tools_menu.addAction(item)
        window_menu = self.mac_menubar.addMenu("Window")
        window_menu.addAction(self.act_next_tab)
        window_menu.addAction(self.act_prev_tab)
        help_menu = self.mac_menubar.addMenu("Help")
        help_menu.addAction(self.act_shortcuts)
        help_menu.addAction(self.act_about)

    # ── tabs ────────────────────────────────────────────────────────────────────────────
    def tab_at(self, index: int) -> Tab | None:
        if 0 <= index < self.tab_bar.count():
            tab = self.tab_bar.tabData(index)
            return tab if isinstance(tab, Tab) else None
        return None

    def tabs(self) -> list[Tab]:
        return [t for t in (self.tab_at(i) for i in range(self.tab_bar.count())) if t is not None]

    def index_of(self, tab: Tab | None) -> int:
        for i in range(self.tab_bar.count()):
            if self.tab_bar.tabData(i) is tab:
                return i
        return -1

    def current_tab(self) -> Tab | None:
        return self.tab_at(self.tab_bar.currentIndex())

    def current_url(self) -> QUrl:
        tab = self.current_tab()
        return tab.url() if tab is not None else QUrl()

    def new_tab(self, url: QUrl | None = None, background: bool = False, index: int | None = None,
                entry: dict | None = None, opener: Tab | None = None, activate: bool = True) -> Tab:
        tab = Tab(self)
        self._wire_tab(tab)
        self.stack.addWidget(tab)
        if opener is not None:
            tab.opener_ref = weakref.ref(opener)
            tab.return_to_opener = not background
        position = self.tab_bar.count() if index is None else clamp(index, 0, self.tab_bar.count())
        position = self.tab_bar.add_tab(position)
        self.tab_bar.setTabData(position, tab)
        if entry is not None:
            tab.pending = dict(entry)
        elif url is not None:
            tab.load(url)
        self._refresh_tab(tab)
        bridge = self.extensions.bridge
        bridge.tab_event("tabs.onCreated", tab, lambda ext_id: [bridge.tab_info(ext_id, tab)])
        if activate and (not background or self.tab_bar.count() == 1):
            self.tab_bar.setCurrentIndex(position)
            self._on_current_changed(position)
        self.schedule_session_save()
        return tab

    def open_new_tab(self) -> None:
        self.new_tab(self._home_url())
        self.focus_url_bar()

    def _home_url(self) -> QUrl:
        home = self.settings.get("homepage").strip()
        if home:
            url = url_from_input(home, self.settings.search_template())
            if url.isValid():
                return url
        override = self.extensions.newtab_override()  # a New Tab extension (chrome_url_overrides)
        return override if override is not None else QUrl(NEWTAB)

    def open_url(self, url: QUrl, where: str = "current", opener: Tab | None = None) -> None:
        if url.isEmpty():
            return
        if where == "current" and self.current_tab() is not None:
            tab = self.current_tab()
            tab.load(url)
            tab.view.setFocus()
        else:
            index = self._insert_position(opener or self.current_tab())
            self.new_tab(url, background=(where == "background"), index=index, opener=opener or self.current_tab())

    def _insert_position(self, opener: Tab | None) -> int | None:
        index = self.index_of(opener)
        if index < 0:
            return None
        position = index + 1
        while position < self.tab_bar.count() and getattr(self.tab_at(position), "opener", None) is opener:
            position += 1
        return position

    def close_tab(self, tab: Tab | None) -> None:
        if tab is None:
            return
        if tab.pending is not None or tab.crashed or tab.close_requested or self._closing:
            self._remove_tab(tab)
            return
        # Let the page run its "leave page?" check (onbeforeunload); it answers via windowCloseRequested.
        tab.close_requested = True
        tab.page.triggerAction(QWebEnginePage.WebAction.RequestClose)
        QTimer.singleShot(3000, lambda: self._reset_close_request(tab))

    @staticmethod
    def _reset_close_request(tab: Tab) -> None:
        if not sip.isdeleted(tab):
            tab.close_requested = False

    def _remove_tab(self, tab: Tab) -> None:
        index = self.index_of(tab)
        if index < 0:
            return
        if tab is self._fullscreen_tab:
            self._leave_html_fullscreen()
        entry = tab.session_entry()
        if display_url(QUrl(entry.get("url", ""))) or entry.get("history"):
            self.closed_tabs.append(entry)
            del self.closed_tabs[:-MAX_CLOSED_TABS]
        if self.tab_bar.count() == 1:  # keep the window open with a fresh New Tab, like Firefox can
            self.new_tab(self._home_url())
            index = self.index_of(tab)
        elif index == self.tab_bar.currentIndex() and tab.return_to_opener:
            opener = tab.opener
            if opener is not None and self.index_of(opener) >= 0:
                self.tab_bar.setCurrentIndex(self.index_of(opener))
        tab.shutdown()
        label = self.tab_bar.label(index)
        if label is not None:
            THROBBER.watch(label, False)
        self.tab_bar.removeTab(index)
        self.stack.removeWidget(tab)
        bridge = self.extensions.bridge
        bridge.tab_closed(tab)
        bridge.tab_event("tabs.onRemoved", tab, lambda _ext: [tab.tab_id, {"windowId": MAIN_WINDOW_ID, "isWindowClosing": self._closing}])
        tab.deleteLater()
        current = self.current_tab()
        if current is not None:
            self._activate(current)
        self.schedule_session_save()

    def reopen_closed_tab(self) -> None:
        if self.closed_tabs:
            entry = self.closed_tabs.pop()
            tab = self.new_tab(entry=entry)
            tab.ensure_loaded()

    def duplicate_tab(self, tab: Tab) -> None:
        entry = tab.session_entry()
        new = self.new_tab(entry=entry, index=self.index_of(tab) + 1)
        new.ensure_loaded()

    def cycle_tab(self, step: int) -> None:
        count = self.tab_bar.count()
        if count > 1:
            self.tab_bar.setCurrentIndex((self.tab_bar.currentIndex() + step) % count)

    def select_tab_number(self, number: int) -> None:
        count = self.tab_bar.count()
        if count:
            self.tab_bar.setCurrentIndex(count - 1 if number == 9 else min(number - 1, count - 1))

    def toggle_mute(self, tab: Tab | None) -> None:
        if tab is None:
            return
        tab.ensure_loaded()
        tab.page.setAudioMuted(not tab.page.isAudioMuted())
        self._refresh_tab(tab)
        self.schedule_session_save()

    def _wire_tab(self, tab: Tab) -> None:
        page = tab.page
        page.titleChanged.connect(lambda title, t=tab: self._on_title_changed(t, title))
        page.iconChanged.connect(lambda ic, t=tab: self._on_icon_changed(t, ic))
        page.urlChanged.connect(lambda url, t=tab: self._on_url_changed(t, url))
        page.loadStarted.connect(lambda *_, t=tab: self._on_load_started(t))
        page.loadProgress.connect(lambda value, t=tab: self._on_load_progress(t, value))
        page.loadFinished.connect(lambda ok, t=tab: self._on_load_finished(t, ok))
        page.loadingChanged.connect(lambda info, t=tab: self._on_loading_changed(t, info))
        page.linkHovered.connect(lambda url, t=tab: self._on_link_hovered(t, url))
        page.newWindowRequested.connect(lambda request, t=tab: self.handle_new_window(request, t))
        page.permissionRequested.connect(lambda permission, t=tab: self._on_permission(t, permission))
        page.fullScreenRequested.connect(lambda request, t=tab: self._on_fullscreen_request(t, request))
        page.recentlyAudibleChanged.connect(lambda *_, t=tab: self._refresh_tab(t))
        page.audioMutedChanged.connect(lambda *_, t=tab: self._refresh_tab(t))
        page.windowCloseRequested.connect(lambda *_, t=tab: self._remove_tab(t))
        page.iconUrlChanged.connect(lambda url, t=tab: self._tab_updated(t, {"favIconUrl": url.toString()}) if not url.isEmpty() else None)
        page.recentlyAudibleChanged.connect(lambda audible, t=tab: self._tab_updated(t, {"audible": audible}))
        page.audioMutedChanged.connect(lambda muted, t=tab: self._tab_updated(t, {"mutedInfo": {"muted": muted}}))
        page.renderProcessTerminated.connect(lambda status, _code, t=tab: self._on_crashed(t, status))
        page.findTextFinished.connect(lambda result, t=tab: self._on_find_result(t, result))
        page.certificateAccepted.connect(self._on_certificate_accepted)
        page.certificateProblem.connect(lambda error, t=tab: self._on_certificate_problem(t, error))
        tab.view.printFinished.connect(lambda ok: self._on_print_finished(ok))

    def _on_current_changed(self, index: int) -> None:
        tab = self.tab_at(index)
        if tab is not None:
            self._activate(tab)
            self._refresh_extension_buttons()
            self.extensions.bridge.tab_event("tabs.onActivated", tab, lambda _ext: [{"tabId": tab.tab_id, "windowId": MAIN_WINDOW_ID}])

    def _activate(self, tab: Tab) -> None:
        if self._closing:
            return
        if self._fullscreen_tab is not None and self._fullscreen_tab is not tab:
            self._exit_html_fullscreen()
        for other in self.tabs():
            if other is not tab and other.return_to_opener and other.opener is not tab:
                other.return_to_opener = False
        if self.stack.currentWidget() is not tab:
            if self.find_bar.isVisible():
                self.find_bar.close_bar()
            self.stack.setCurrentWidget(tab)
        tab.ensure_loaded()
        self.content.bubble.hide()
        self._sync_chrome(tab)
        if not self.url_bar.text() and not self.url_bar.hasFocus():
            self.url_bar.setFocus()
        elif not self.url_bar.hasFocus() or not self.url_bar.isModified():
            tab.view.setFocus()
        self.schedule_session_save()

    def _sync_chrome(self, tab: Tab) -> None:
        """Update toolbar, address bar and window title for the current tab."""
        if not self.url_bar.isModified() or not self.url_bar.hasFocus():
            self.url_bar.set_url_text(display_url(tab.url()))
        self.url_bar.setPlaceholderText(f"Search with {self.settings.get('search_engine')} or enter address")
        self._update_nav_buttons(tab)
        self._update_star()
        self._update_identity(tab)
        self._update_zoom_indicator()
        self.content.loading_bar.set_progress(tab.progress, tab.loading)
        title = tab.title()
        self.setWindowTitle(APP_NAME if is_newtab(tab.url()) else f"{title} — {APP_NAME}")

    def _refresh_tab(self, tab: Tab) -> None:
        index = self.index_of(tab)
        if index < 0:
            return
        label = self.tab_bar.label(index)
        if label is not None:
            label.set_state(tab.title(), tab.favicon(), tab.loading, tab.audio_state())
        url_text = display_url(tab.url())
        self.tab_bar.setTabToolTip(index, tab.title() + (f"\n{url_text}" if url_text else ""))

    def _update_nav_buttons(self, tab: Tab) -> None:
        history = tab.page.history()
        self.back_button.setEnabled(tab.pending is None and history.canGoBack())
        self.forward_button.setEnabled(tab.pending is None and history.canGoForward())
        if tab.loading:
            self.reload_button.setIcon(icon("stop"))
            self.reload_button.setToolTip("Stop loading this page (Esc)")
        else:
            self.reload_button.setIcon(icon("reload"))
            self.reload_button.setToolTip(f"Reload current page ({shortcut_text('Ctrl+R')})")

    def _update_identity(self, tab: Tab) -> None:
        url = tab.url()
        if not display_url(url):
            self.url_bar.identity.setIcon(icon("search", P.TEXT_2))
            self.url_bar.identity.setToolTip("")
        elif url.scheme() == "https":
            if url.host() in self.certificate_exceptions:
                self.url_bar.identity.setIcon(icon("warning", P.WARNING))
                self.url_bar.identity.setToolTip("You accepted a certificate risk for this site")
            else:
                self.url_bar.identity.setIcon(icon("lock", P.TEXT_2))
                self.url_bar.identity.setToolTip("Connection secure")
        elif url.scheme() == "http":
            self.url_bar.identity.setIcon(icon("warning", P.TEXT_2))
            self.url_bar.identity.setToolTip("Connection not secure")
        else:
            self.url_bar.identity.setIcon(icon("info", P.TEXT_2))
            self.url_bar.identity.setToolTip(f"{APP_NAME} page")

    def _update_star(self) -> None:
        tab = self.current_tab()
        url = tab.url() if tab else QUrl()
        visible = bool(display_url(url))
        self.url_bar.star.setVisible(visible)
        if self.bookmarks.is_bookmarked(url.toString()):
            self.url_bar.star.setIcon(icon("star-filled", P.ACCENT))
            self.url_bar.star.setToolTip(f"Edit this bookmark ({shortcut_text('Ctrl+D')})")
        else:
            self.url_bar.star.setIcon(icon("star", P.TEXT_2))
            self.url_bar.star.setToolTip(f"Bookmark this page ({shortcut_text('Ctrl+D')})")

    # ── page events ─────────────────────────────────────────────────────────────────────
    def _tab_updated(self, tab: Tab, change: dict) -> None:
        """tabs.onUpdated - url, title and icon only for extensions that may see them."""
        bridge = self.extensions.bridge

        def args(ext_id: str):
            info = bridge.tab_info(ext_id, tab)
            visible = {k: v for k, v in change.items() if k not in ("url", "title", "favIconUrl") or k in info}
            return [tab.tab_id, visible, info] if visible else None
        bridge.tab_event("tabs.onUpdated", tab, args)

    def _navigation_event(self, tab: Tab, name: str, url: QUrl) -> None:
        details = {"tabId": tab.tab_id, "url": url.toString(), "frameId": 0, "parentFrameId": -1, "processId": -1,
                   "timeStamp": time.time() * 1000, "documentLifecycle": "active", "frameType": "outermost_frame"}
        if name in ("onCommitted", "onHistoryStateUpdated"):
            details.update(transitionType="link", transitionQualifiers=[])
        self.extensions.bridge.tab_event("webNavigation." + name, tab, lambda _ext: [details])

    def _on_title_changed(self, tab: Tab, title: str) -> None:
        self._refresh_tab(tab)
        self._tab_updated(tab, {"title": title})
        if tab is self.current_tab():
            self.setWindowTitle(APP_NAME if is_newtab(tab.url()) else f"{tab.title()} — {APP_NAME}")
        url = tab.page.url()
        if title and HistoryStore.recordable(url):
            self.history.set_title(url.toString(), title)
        self.schedule_session_save()

    def _on_icon_changed(self, tab: Tab, icon_: QIcon) -> None:
        if not icon_.isNull() and tab.page.url().scheme() in ("http", "https"):
            self.favicons.store(tab.page.url(), icon_)
        self._refresh_tab(tab)

    def _on_url_changed(self, tab: Tab, url: QUrl) -> None:
        self.extensions.bridge.tab_navigated(tab, url)
        self._tab_updated(tab, {"url": url.toString()})
        self._navigation_event(tab, "onCommitted" if tab.loading else "onHistoryStateUpdated", url)
        if tab is self.current_tab():
            self._sync_chrome(tab)
        self._apply_site_zoom(tab)
        self._update_webstore_bar(tab, url)
        for bar in list(tab.permission_bars):
            if sip.isdeleted(bar) or bar.property("origin") != f"{url.scheme()}://{url.authority()}":
                tab.permission_bars.remove(bar)
                if not sip.isdeleted(bar):
                    bar.dismiss()
        if tab.loading:
            self.site_visited(url)
        if not tab.loading and HistoryStore.recordable(url) and url.toString() != tab.last_recorded:
            tab.last_recorded = url.toString()
            self.history.add_visit(url.toString(), tab.page.title())
        self._refresh_tab(tab)
        self.schedule_session_save()

    def _on_load_started(self, tab: Tab) -> None:
        tab.loading, tab.progress, tab.crashed = True, 0, False
        self._tab_updated(tab, {"status": "loading"})
        self._navigation_event(tab, "onBeforeNavigate", tab.page.requestedUrl())
        if tab.crash_bar is not None and not sip.isdeleted(tab.crash_bar):
            tab.crash_bar.dismiss()
        tab.crash_bar = None
        self._refresh_tab(tab)
        if tab is self.current_tab():
            self._update_nav_buttons(tab)
            self.content.loading_bar.set_progress(0, True)

    def _on_load_progress(self, tab: Tab, value: int) -> None:
        tab.progress = value
        if tab is self.current_tab():
            self.content.loading_bar.set_progress(value, tab.loading)

    def _on_load_finished(self, tab: Tab, ok: bool) -> None:
        tab.loading, tab.progress = False, 100
        self._refresh_tab(tab)
        self._tab_updated(tab, {"status": "complete"})
        if ok:
            self._navigation_event(tab, "onDOMContentLoaded", tab.page.url())
            self._navigation_event(tab, "onCompleted", tab.page.url())
        url = tab.page.url()
        if ok and HistoryStore.recordable(url):
            tab.last_recorded = url.toString()
            self.history.add_visit(url.toString(), tab.page.title())
        self._apply_site_zoom(tab)
        if tab is self.current_tab():
            self._sync_chrome(tab)
        self.schedule_session_save()

    def _on_loading_changed(self, tab: Tab, info: QWebEngineLoadingInfo) -> None:
        status = info.status()
        if status == QWebEngineLoadingInfo.LoadStatus.LoadFailedStatus and info.errorCode() in PROXY_ERROR_CODES:
            self._warn_vpn_unreachable(tab)
        if tab.back_after_error and status != QWebEngineLoadingInfo.LoadStatus.LoadStartedStatus:
            target, tab.back_after_error = tab.back_after_error, ""
            if status == QWebEngineLoadingInfo.LoadStatus.LoadFailedStatus and info.url().toString() == target:
                QTimer.singleShot(0, lambda: tab.page.history().canGoBack()
                                  and tab.page.triggerAction(QWebEnginePage.WebAction.Back))
        fallback = tab.https_fallback
        if (fallback is None or info.url().host() != fallback.host()
                or status not in (QWebEngineLoadingInfo.LoadStatus.LoadFailedStatus,
                                  QWebEngineLoadingInfo.LoadStatus.LoadSucceededStatus)):
            return
        tab.https_fallback = None
        typed, tab.typed_text = tab.typed_text, ""
        url = info.url()
        if (status == QWebEngineLoadingInfo.LoadStatus.LoadFailedStatus and url.scheme() == "https"
                and info.errorDomain() == QWebEngineLoadingInfo.ErrorDomain.ConnectionErrorDomain):
            if info.errorCode() in (-105, -137) and typed:  # ERR_NAME_NOT_RESOLVED: it wasn't a web address
                tab.page.load(QUrl(self.settings.search_template().format(quote_plus(typed))))
                return
            insecure = QUrl(url)
            insecure.setScheme("http")
            tab.page.load(insecure)  # the site doesn't speak HTTPS: fall back like Firefox's HTTPS-First mode

    def _on_link_hovered(self, tab: Tab, url: str) -> None:
        if tab is self.current_tab():
            self.content.bubble.show_text(QUrl(url).toDisplayString() if url else "")

    def _on_crashed(self, tab: Tab, status) -> None:
        if status == QWebEnginePage.RenderProcessTerminationStatus.NormalTerminationStatus or self._closing:
            return
        tab.crashed, tab.loading = True, False
        self._refresh_tab(tab)
        if tab.crash_bar is None or sip.isdeleted(tab.crash_bar):
            bar = InfoBar(icon("warning", P.WARNING), "<b>This tab crashed.</b> Reload it to try again.", "warning")
            bar.add_button("Reload Tab", lambda: (bar.dismiss(), tab.page.triggerAction(QWebEnginePage.WebAction.Reload)), True)
            tab.crash_bar = bar
            tab.add_bar(bar)

    def _on_certificate_accepted(self, host: str) -> None:
        self.certificate_exceptions.add(host)
        tab = self.current_tab()
        if tab is not None:
            self._update_identity(tab)

    def _on_certificate_problem(self, tab: Tab, error) -> None:
        url = error.url()
        host = url.host() or url.toString()
        bar = InfoBar(icon("warning", P.WARNING),
                      f"<b>Warning: Potential Security Risk Ahead.</b> {APP_NAME} didn't open "
                      f"<b>{html.escape(host)}</b>: {html.escape(error.description().rstrip('. '))}. "
                      "Attackers might be trying to steal your information.", "warning")
        bar.setProperty("origin", f"{url.scheme()}://{url.authority()}")
        decided = {"done": False}

        def decide(accept: bool) -> None:
            if decided["done"]:
                return
            decided["done"] = True
            try:
                if accept:
                    self._on_certificate_accepted(host)
                    error.acceptCertificate()
                else:
                    error.rejectCertificate()
            except RuntimeError:
                pass

        def go_back() -> None:
            tab.back_after_error = url.toString()  # Chromium commits an error page first; step back past it
            decide(False)
            bar.dismiss()

        bar.add_button("Go Back (Recommended)", go_back, primary=True)
        bar.add_button("Accept the Risk and Continue", lambda: (decide(True), bar.dismiss()))
        bar.on_dismiss = lambda: decide(False)
        tab.permission_bars.append(bar)
        tab.add_bar(bar)

    def _on_find_result(self, tab: Tab, result) -> None:
        if tab is self.current_tab():
            self.find_bar.show_result(result)

    def handle_new_window(self, request: QWebEngineNewWindowRequest, source: Tab | None) -> None:
        destination = request.destination()
        kinds = QWebEngineNewWindowRequest.DestinationType
        if destination == kinds.InNewDialog:
            popup = PopupWindow(self, request.requestedGeometry())
            request.openIn(popup.page)
            popup.show()
            return
        background = destination == kinds.InNewBackgroundTab
        index = self._insert_position(source) if source is not None else None
        tab = self.new_tab(None, background=background, index=index, opener=source)
        request.openIn(tab.page)

    def _update_webstore_bar(self, tab: Tab, url: QUrl) -> None:
        match = WEBSTORE_RE.match(url.toString())
        current_id = tab.webstore_bar.property("ext_id") if tab.webstore_bar and not sip.isdeleted(tab.webstore_bar) else None
        if match is None or match.group(1) != current_id:
            if tab.webstore_bar is not None and not sip.isdeleted(tab.webstore_bar):
                tab.webstore_bar.dismiss()
            tab.webstore_bar = None
        if match is None or tab.webstore_bar is not None:
            return
        ext_id = match.group(1)
        installed = any(e.id == ext_id for e in self.extensions.entries())
        text = (f"This extension is installed in {APP_NAME}." if installed
                else f"Install this extension in {APP_NAME}? The Chrome Web Store's own button only works in Chrome.")
        bar = InfoBar(icon("puzzle", P.ACCENT), text)
        bar.setProperty("ext_id", ext_id)

        def install() -> None:
            name = re.sub(r"\s*[-–—]\s*Chrome Web Store\s*$", "", tab.page.title() or "").strip()
            self.extensions.install_from_webstore(ext_id, name)
            bar.dismiss()

        bar.add_button("Reinstall / Update" if installed else f"Add to {APP_NAME}", install, primary=not installed)
        tab.webstore_bar = bar
        tab.add_bar(bar)

    # ── permissions & full screen ───────────────────────────────────────────────────────
    def _on_permission(self, tab: Tab, permission: QWebEnginePermission) -> None:
        # PyQt's argument is only valid during this call, and the answer comes later: keep a real copy.
        permission = QWebEnginePermission(permission)
        text = PERMISSION_TEXT.get(permission.permissionType())
        if text is None:
            permission.deny()
            return
        remembered = self.remembered_decision(permission)  # set in site settings, or answered before
        if remembered is not None:
            permission.grant() if remembered else permission.deny()
            return
        origin = permission.origin()
        bar = InfoBar(icon("info", P.ACCENT), f"Allow <b>{html.escape(origin.host() or origin.toString())}</b> to {text}?")
        bar.setProperty("origin", f"{origin.scheme()}://{origin.authority()}")
        decided = {"done": False}

        def decide(allow: bool, remember: bool = False) -> None:
            if not decided["done"]:
                decided["done"] = True
                permission.grant() if allow else permission.deny()
                if remember:
                    self.remember_decision(permission, allow)

        bar.add_button("Block", lambda: (decide(False, True), bar.dismiss()))
        bar.add_button("Allow", lambda: (decide(True, True), bar.dismiss()), primary=True)
        bar.on_dismiss = lambda: decide(False)
        tab.permission_bars.append(bar)
        tab.add_bar(bar)

    def ask_permission_modal(self, parent: QWidget, permission: QWebEnginePermission) -> None:
        text = PERMISSION_TEXT.get(permission.permissionType())
        remembered = self.remembered_decision(permission) if text is not None else False
        if remembered is not None:
            permission.grant() if remembered else permission.deny()
            return
        host = permission.origin().host() or permission.origin().toString()
        answer = QMessageBox.question(parent, "Permission Request", f"Allow {host} to {text}?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        permission.grant() if answer == QMessageBox.StandardButton.Yes else permission.deny()

    def _on_fullscreen_request(self, tab: Tab, request) -> None:
        if request.toggleOn():
            if tab is not self.current_tab():
                request.reject()
                return
            request.accept()
            if self._fullscreen_tab is None:
                self._state_before_fullscreen = self.windowState()
            self._fullscreen_tab = tab
            for widget in (self.tab_strip, self.nav_bar, self.bookmarks_bar, self.separator, self.find_bar):
                widget.hide()
            self.act_exit_fullscreen.setEnabled(True)
            self.showFullScreen()
            self.toast("Press Esc to exit full screen.", "info")
        else:
            request.accept()
            self._leave_html_fullscreen()

    def _exit_html_fullscreen(self) -> None:
        tab = self._fullscreen_tab
        if tab is not None and not sip.isdeleted(tab):
            tab.page.triggerAction(QWebEnginePage.WebAction.ExitFullScreen)
        self._leave_html_fullscreen()

    def _leave_html_fullscreen(self) -> None:
        if self._fullscreen_tab is None:
            return
        self._fullscreen_tab = None
        self.act_exit_fullscreen.setEnabled(False)
        self.content.toast.hide()  # the "Press Esc to exit full screen" hint
        for widget in (self.tab_strip, self.nav_bar, self.separator):
            widget.show()
        self._sync_bookmarks_bar()
        self.setWindowState(self._state_before_fullscreen)

    def toggle_fullscreen(self) -> None:
        if self._fullscreen_tab is not None:
            self._exit_html_fullscreen()
        elif self.isFullScreen():
            self.setWindowState(self._state_before_fullscreen & ~Qt.WindowState.WindowFullScreen)
        else:
            self._state_before_fullscreen = self.windowState()
            self.showFullScreen()

    # ── navigation ──────────────────────────────────────────────────────────────────────
    def _navigate_from_url_bar(self, text: str) -> None:
        modifiers = QApplication.keyboardModifiers()
        stripped = text.strip()
        if modifiers & Qt.KeyboardModifier.ControlModifier and re.fullmatch(r"[\w-]+", stripped):
            stripped = f"www.{stripped}.com"
        url = url_from_input(stripped, self.settings.search_template())
        if url.isEmpty():
            return
        tab = self.current_tab()
        if modifiers & Qt.KeyboardModifier.AltModifier or tab is None:
            tab = self.new_tab(url, opener=tab, index=self._insert_position(tab))
        else:
            tab.load(url)
        # "example.com" was upgraded to https:// - remember it so we can fall back to http:// if the site has
        # no HTTPS, or to a web search if the name doesn't exist at all
        upgraded = url == QUrl("https://" + stripped)
        tab.https_fallback = url if upgraded else None
        tab.typed_text = stripped if upgraded else ""
        self.url_bar.set_url_text(display_url(url))
        tab.view.setFocus()

    def _page_action(self, action: QWebEnginePage.WebAction) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.ensure_loaded()
            tab.page.triggerAction(action)

    def reload(self) -> None:
        self._page_action(QWebEnginePage.WebAction.Reload)

    def hard_reload(self) -> None:
        self._page_action(QWebEnginePage.WebAction.ReloadAndBypassCache)

    def reload_or_stop(self) -> None:
        tab = self.current_tab()
        if tab is not None and tab.loading:
            tab.page.triggerAction(QWebEnginePage.WebAction.Stop)
        else:
            self.reload()

    def focus_url_bar(self) -> None:
        if self._fullscreen_tab is None:
            self.url_bar.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.url_bar.selectAll()

    def _find_again(self, backward: bool) -> None:
        if self.find_bar.isVisible() and self.find_bar.edit.text():
            self.find_bar.find(backward)
        else:
            self.find_bar.open()

    def view_source(self) -> None:
        tab = self.current_tab()
        if tab is not None and display_url(tab.url()):
            self.open_url(QUrl("view-source:" + tab.url().toString()), "tab", opener=tab)

    def toggle_devtools(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.toggle_devtools()

    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open File", str(Path.home()),
                                              "Web pages and documents (*.html *.htm *.xhtml *.svg *.pdf *.txt *.png *.jpg *.jpeg *.gif *.webp);;All files (*)")
        if path:
            self.open_url(QUrl.fromLocalFile(path), "tab")

    def current_page_download(self, url: QUrl) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.page.download(url)

    def _fill_history_menu(self, menu: QMenu, back: bool) -> None:
        reset_menu(menu)
        tab = self.current_tab()
        if tab is None or tab.pending is not None:
            return
        history = tab.page.history()
        items = list(reversed(history.backItems(15))) if back else history.forwardItems(15)
        for item in items:
            action = menu.addAction(self.favicons.get(item.url()), menu_text(elide(item.title() or item.url().toString(), 60)))
            action.triggered.connect(lambda *_, h=history, it=item: h.goToItem(it))

    # ── zoom ────────────────────────────────────────────────────────────────────────────
    def _site_zoom(self, url: QUrl) -> float:
        value = self.settings.get("site_zoom").get(url.host(), 1.0)
        return float(value) if isinstance(value, (int, float)) else 1.0

    def _apply_site_zoom(self, tab: Tab) -> None:
        if tab.pending is None and tab.page.url().host():  # files and data: pages keep their own zoom
            factor = self._site_zoom(tab.page.url())
            if abs(tab.view.zoomFactor() - factor) > 0.001:
                tab.view.setZoomFactor(factor)

    def _set_zoom(self, factor: float) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        tab.ensure_loaded()
        factor = round(clamp(factor, ZOOM_LEVELS[0], ZOOM_LEVELS[-1]), 2)
        host = tab.page.url().host()
        if host:
            zooms = dict(self.settings.get("site_zoom"))
            if abs(factor - 1.0) < 0.001:
                zooms.pop(host, None)
            else:
                zooms[host] = factor
            self.settings.set("site_zoom", zooms)
            for other in self.tabs():
                if other.pending is None and other.page.url().host() == host:
                    other.view.setZoomFactor(factor)
        else:
            tab.view.setZoomFactor(factor)
        self._update_zoom_indicator()

    def zoom_in(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            current = tab.view.zoomFactor()
            self._set_zoom(next((z for z in ZOOM_LEVELS if z > current + 0.001), ZOOM_LEVELS[-1]))

    def zoom_out(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            current = tab.view.zoomFactor()
            self._set_zoom(next((z for z in reversed(ZOOM_LEVELS) if z < current - 0.001), ZOOM_LEVELS[0]))

    def zoom_reset(self) -> None:
        self._set_zoom(1.0)

    def _update_zoom_indicator(self) -> None:
        tab = self.current_tab()
        factor = tab.view.zoomFactor() if tab is not None and tab.pending is None else 1.0
        percent = int(round(factor * 100))
        if percent == 100:
            self.url_bar.zoom_action.setVisible(False)
        else:
            self.url_bar.zoom_action.setIcon(self._text_icon(f"{percent}%"))
            self.url_bar.zoom_action.setToolTip(f"Zoom {percent}% — click to reset")
            self.url_bar.zoom_action.setVisible(True)
        self._update_zoom_widget()

    def _update_zoom_widget(self) -> None:
        tab = self.current_tab()
        factor = tab.view.zoomFactor() if tab is not None and tab.pending is None else 1.0
        if hasattr(self, "zoom_label"):
            self.zoom_label.setText(f"{int(round(factor * 100))}%")

    @staticmethod
    def _text_icon(text: str) -> QIcon:
        scale = 2
        font = QFont(QApplication.font())
        font.setPixelSize(11 * scale)
        font.setBold(True)
        pixmap = QPixmap(46 * scale, 20 * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(185, 163, 255, 70))
        painter.drawRoundedRect(QRectF(0, 1 * scale, 46 * scale, 18 * scale), 9 * scale, 9 * scale)
        painter.setPen(QColor(P.TEXT))
        painter.setFont(font)
        painter.drawText(QRect(0, 0, 46 * scale, 20 * scale), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        return QIcon(pixmap)

    # ── bookmarks ───────────────────────────────────────────────────────────────────────
    def bookmark_current_page(self) -> None:
        tab = self.current_tab()
        if tab is None or not display_url(tab.url()):
            return
        if not self.nav_bar.isVisible():
            return
        panel = BookmarkPanel(self, tab.url().toString(), tab.title())
        panel.destroyed.connect(lambda *_: self._update_star())
        anchor = self.url_bar
        panel.popup_at(anchor)

    def add_bookmark_dialog(self, url: str, title: str) -> None:
        dialog = BookmarkEditDialog(self, self.bookmarks, "New Bookmark", title, url, "toolbar")
        if run_dialog(dialog):
            name, new_url, folder = dialog.values()
            if new_url:
                self.bookmarks.add_bookmark(name or new_url, new_url, folder)
                self.toast("Bookmark saved.")

    def edit_bookmark(self, node_id: str, parent: QWidget | None = None) -> None:
        node = self.bookmarks.node(node_id)
        if node is None or node_id in self.bookmarks.roots:
            return
        folder = self.bookmarks.parent(node_id)
        is_url = node["type"] == "url"
        dialog = BookmarkEditDialog(parent or self, self.bookmarks, "Edit Bookmark" if is_url else "Edit Folder",
                                    node["title"], node.get("url") if is_url else None, folder["id"] if folder else "toolbar")
        if run_dialog(dialog):
            name, url, folder_id = dialog.values()
            self.bookmarks.update(node_id, title=name, url=url)
            current_parent = self.bookmarks.parent(node_id)
            if current_parent is not None and folder_id != current_parent["id"]:
                if not self.bookmarks.move(node_id, folder_id):
                    self.toast("A folder can't be moved into itself.", "error")

    def set_bookmarks_bar_visible(self, visible: bool) -> None:
        self.settings.set("show_bookmarks_bar", bool(visible))
        self._sync_bookmarks_bar()
        self.act_bookmarks_bar.setChecked(bool(visible))

    def _sync_bookmarks_bar(self) -> None:
        """Shown when the user wants it and there is something on it (an empty bar is hidden)."""
        if self._fullscreen_tab is None:
            self.bookmarks_bar.setVisible(bool(self.settings.get("show_bookmarks_bar") and self.bookmarks.children("toolbar")))

    def add_bookmark_node_to_menu(self, menu: QMenu, node: dict) -> None:
        if node["type"] == "folder":
            sub = Menu(menu_text(node["title"] or "Folder"), menu)
            sub.setIcon(icon("folder", P.TEXT_2))
            sub.aboutToShow.connect(lambda: self.fill_bookmark_menu(sub, node["id"]))
            menu.addMenu(sub)
        else:
            action = menu.addAction(self.favicons.get(node["url"]), menu_text(elide(node["title"] or node["url"], 60)))
            action.setToolTip(node["url"])
            action.triggered.connect(lambda *_, u=node["url"]: self.open_url(QUrl(u), "current"))

    def fill_bookmark_menu(self, menu: QMenu, folder_id: str) -> None:
        reset_menu(menu)
        children = self.bookmarks.children(folder_id)
        for node in children:
            self.add_bookmark_node_to_menu(menu, node)
        urls = [n["url"] for n in children if n["type"] == "url"]
        if not children:
            menu.addAction("(Empty)").setEnabled(False)
        if len(urls) > 1:
            menu.addSeparator()
            menu.addAction("Open All in Tabs", lambda: [self.open_url(QUrl(u), "background") for u in urls])

    def _fill_bookmarks_menu(self, menu: QMenu) -> None:
        reset_menu(menu)
        menu.addAction(self.act_bookmark)
        menu.addAction(self.act_bookmarks_bar)
        menu.addAction(self.act_manage_bookmarks)
        menu.addSeparator()
        for key, title in BookmarkStore.ROOTS:
            sub = Menu(title, menu)
            sub.setIcon(icon("folder", P.TEXT_2))
            sub.aboutToShow.connect(lambda s=sub, k=key: self.fill_bookmark_menu(s, k))
            menu.addMenu(sub)
        menu.addSeparator()
        menu.addAction("Import Bookmarks from HTML…", self.import_bookmarks)
        menu.addAction("Export Bookmarks to HTML…", self.export_bookmarks)

    def bookmark_context_menu(self, node_id: str | None, global_pos: QPoint) -> None:
        menu = Menu("", self)
        node = self.bookmarks.node(node_id) if node_id else None
        if node is not None and node["type"] == "url":
            url = QUrl(node["url"])
            menu.addAction("Open", lambda: self.open_url(url, "current"))
            menu.addAction("Open in New Tab", lambda: self.open_url(url, "background"))
            menu.addSeparator()
        if node is not None:
            menu.addAction("Edit…", lambda: self.edit_bookmark(node["id"]))
            menu.addAction("Delete", lambda: self.bookmarks.remove(node["id"]))
            menu.addSeparator()
        tab = self.current_tab()
        if tab is not None and display_url(tab.url()):
            menu.addAction("Bookmark This Page Here", lambda: (self.bookmarks.add_bookmark(tab.title(), tab.url().toString(), "toolbar"),
                                                               self.toast("Bookmark added to the toolbar.")))
        menu.addAction("New Folder…", self._new_toolbar_folder)
        menu.addSeparator()
        menu.addAction("Manage Bookmarks", self.show_bookmarks_manager)
        menu.addAction("Hide Bookmarks Toolbar", lambda: self.set_bookmarks_bar_visible(False))
        menu.exec(global_pos)
        menu.deleteLater()

    def _new_toolbar_folder(self) -> None:
        dialog = BookmarkEditDialog(self, self.bookmarks, "New Folder", "New Folder", None, "toolbar")
        if run_dialog(dialog):
            name, _url, folder = dialog.values()
            self.bookmarks.add_folder(name or "New Folder", folder)

    def import_bookmarks(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Bookmarks", str(Path.home()), "Bookmark files (*.html *.htm);;All files (*)")
        if not path:
            return
        try:
            count = self.bookmarks.import_html(path)
        except (OSError, UnicodeError, ValueError) as exc:
            self.toast(f"Couldn't import bookmarks: {exc}", "error")
            return
        self.toast(f"Imported {count} bookmark{'s' if count != 1 else ''}.")

    def export_bookmarks(self) -> None:
        default = str(Path.home() / f"{APP_NAME.lower().replace(' ', '-')}-bookmarks-{time.strftime('%Y-%m-%d')}.html")
        path, _ = QFileDialog.getSaveFileName(self, "Export Bookmarks", default, "Bookmark files (*.html)")
        if not path:
            return
        try:
            self.bookmarks.export_html(path)
            self.toast("Bookmarks exported.")
        except OSError as exc:
            self.toast(f"Couldn't export bookmarks: {exc}", "error")

    # ── extensions ──────────────────────────────────────────────────────────────────────
    def _rebuild_extension_buttons(self) -> None:
        while self.extension_buttons.count():
            widget = self.extension_buttons.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        for entry in self.extensions.entries():
            if not (entry.enabled and entry.pinned):
                continue
            button = ExtensionButton(entry)
            button.clicked.connect(lambda *_, e=entry.id, b=button: self.open_extension(e, b))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(lambda pos, e=entry.id, n=entry.name, b=button: self._extension_button_menu(e, n, b.mapToGlobal(pos)))
            self.extension_buttons.addWidget(button)
        self._refresh_extension_buttons()
        self._rebuild_extension_commands()
        tab = self.current_tab()
        if tab is not None:
            self._update_webstore_bar(tab, tab.url())

    def _rebuild_extension_commands(self) -> None:
        """Extensions' keyboard shortcuts (manifest "commands") - never one Foxglove already uses."""
        for action in self._command_actions:
            self.removeAction(action)
            action.deleteLater()
        self._command_actions = []
        taken = {seq.toString() for action in self.actions() for seq in action.shortcuts()}
        for entry in self.extensions.entries():
            for name, command in self.extensions.commands(entry.id).items() if entry.enabled else ():
                sequence = QKeySequence(command_shortcut(command))
                if sequence.isEmpty() or sequence.count() != 1 or sequence.toString() in taken:
                    continue
                taken.add(sequence.toString())
                action = QAction(self)
                action.setShortcut(sequence)
                action.triggered.connect(lambda *_, e=entry.id, n=name: self.run_extension_command(e, n))
                self.addAction(action)
                self._command_actions.append(action)

    def run_extension_command(self, ext_id: str, name: str) -> None:
        if name in ("_execute_action", "_execute_browser_action", "_execute_page_action"):
            self.open_extension(ext_id)
            return
        tab = self.current_tab()
        bridge = self.extensions.bridge
        bridge.grant_active_tab(ext_id, tab)
        bridge.emit(ext_id, "commands.onCommand", [name, bridge.tab_info(ext_id, tab)] if tab is not None else [name])

    def extension_button(self, ext_id: str) -> ExtensionButton | None:
        for i in range(self.extension_buttons.count()):
            button = self.extension_buttons.itemAt(i).widget()
            if isinstance(button, ExtensionButton) and button.ext_id == ext_id:
                return button
        return None

    def _refresh_extension_buttons(self, ext_id: str | None = None) -> None:
        """Badge, title, icon and on/off state from chrome.action, for the current tab."""
        tab = self.current_tab()
        tab_id = tab.tab_id if tab is not None else None
        bridge = self.extensions.bridge
        for i in range(self.extension_buttons.count()):
            button = self.extension_buttons.itemAt(i).widget()
            if not isinstance(button, ExtensionButton) or (ext_id and button.ext_id != ext_id):
                continue
            state = {key: bridge.action_value(button.ext_id, key, tab_id)
                     for key in ("title", "enabled", "badgeText", "badgeBackground", "badgeTextColor")}
            title = state["title"] or bridge.default_title(button.ext_id) or button.name
            button.setToolTip(plain_tip(str(title)))
            icon_ = bridge.action_icon(button.ext_id, tab_id) or button.default_icon
            enabled = state["enabled"] is not False
            button.setIcon(icon_ if enabled else QIcon(icon_.pixmap(QSize(16, 16), QIcon.Mode.Disabled)))
            button.set_badge(str(state["badgeText"] or ""), QColor(*(state["badgeBackground"] or BADGE_COLOR)),
                             QColor(*(state["badgeTextColor"] or [255, 255, 255, 255])))

    def _extension_button_menu(self, ext_id: str, name: str, pos: QPoint) -> None:
        menu = Menu("", self)
        entry = self.extensions.entry(ext_id)
        tab = self.current_tab()
        items = [m for m in self.extensions.bridge.menus(ext_id) if m.get("visible") is not False]
        fits = lambda m: bool(set(_strings(m.get("contexts"))) & {"action", "browser_action", "page_action"})
        top = [m for m in items if m.get("parentId") is None and fits(m)]
        if top and entry is not None:  # the extension's own items for its button (contexts: ["action"])
            info = {"editable": False, **({"pageUrl": tab.url().toString()} if tab is not None else {})}
            self._add_extension_menu_items(menu, entry, items, top, fits, info, tab)
            menu.addSeparator()
        if entry is not None and entry.enabled and not entry.options_url.isEmpty():
            menu.addAction("Options", lambda: self.open_url(entry.options_url, "tab"))
        menu.addAction("Unpin from Toolbar", lambda: self.extensions.set_pinned(ext_id, False))
        menu.addAction("Manage Extensions", self.show_extensions)
        menu.addSeparator()
        menu.addAction("Remove Extension…", lambda: self.confirm_remove_extension(ext_id, name))
        menu.exec(pos)
        menu.deleteLater()

    def open_extension(self, ext_id: str, anchor: QWidget | None = None, invoked: bool = True) -> bool:
        """The extension's toolbar button: its pop-up, else chrome.action.onClicked, else its options page.
        *invoked*: by the user (button, menu, shortcut), which grants activeTab; chrome.action.openPopup() isn't."""
        entry = self.extensions.entry(ext_id)
        if entry is None:
            return False
        bridge = self.extensions.bridge
        tab = self.current_tab()
        tab_id = tab.tab_id if tab is not None else None
        if bridge.action_value(ext_id, "enabled", tab_id) is False:
            return False
        popup = bridge.action_value(ext_id, "popup", tab_id)
        popup_url = entry.popup_url if popup is None else QUrl(popup) if popup else QUrl()
        if not invoked and popup_url.isEmpty():
            return False
        if invoked:
            bridge.grant_active_tab(ext_id, tab)  # the user invoked it on this tab: activeTab
        if not popup_url.isEmpty():
            self.close_extension_popups()
            if anchor is None or not anchor.isVisible():
                anchor = self.extension_button(ext_id)
            panel = ExtensionPopup(self, popup_url, ext_id)
            panel.popup_at(anchor if anchor is not None and anchor.isVisible() else self.extensions_button)
        elif bridge.listens(ext_id, "action.onClicked"):
            bridge.action_clicked(ext_id, tab)
        elif not entry.options_url.isEmpty():
            self.open_url(entry.options_url, "tab")
        else:
            self.toast(f"“{entry.name}” has no pop-up — it works on web pages automatically.", "info")
        return True

    def close_extension_popups(self, ext_id: str | None = None) -> None:
        for popup in self.findChildren(ExtensionPopup):
            if not sip.isdeleted(popup) and popup.isVisible() and (ext_id is None or popup.ext_id == ext_id):
                popup.close()

    def _rewire_tabs(self) -> None:
        """Installed extensions changed: every tab's tab-id script must know the current ones (from the next load)."""
        for tab in [*self.tabs(), *(p for p in list(self.popups) if not sip.isdeleted(p))]:
            self.extensions.wire_tab(tab.page, tab.tab_id)

    def _on_extension_reloaded(self, ext_id: str) -> None:
        """Pages of an extension opened while it was off (or an older version) have no chrome.* APIs: reload them."""
        self.close_extension_popups(ext_id)
        for tab in self.tabs():
            url = tab.url()
            if tab.pending is None and url.scheme() == "chrome-extension" and url.host() == ext_id:
                tab.page.triggerAction(QWebEnginePage.WebAction.Reload)

    def _confirm_replace_extension(self, job: dict) -> None:
        def ask() -> None:
            source = job.get("source_path") or "a downloaded file"
            accepted = ask_question(self, "Replace Extension",
                                    f"Replace “{job.get('existing_name')}” with “{job.get('name')}” from {source}?\n\n"
                                    "The new version gets access to everything the installed one has saved.", "Replace")
            self.extensions.resolve_replace(job, accepted)
        QTimer.singleShot(0, ask)  # not inside the install callback

    def show_extension_notification(self, ext_id: str, note_id: str, options: dict) -> None:
        """chrome.notifications: a notice in the window; clicking it is notifications.onClicked."""
        entry = self.extensions.entry(ext_id)
        text = " — ".join(str(options.get(k)).strip() for k in ("title", "message") if str(options.get(k) or "").strip())
        self.content.toast.show_message(f"{entry.name if entry else 'An extension'}: {text or 'Notification'}", "info",
                                        timeout=8000, on_click=lambda: self.extensions.bridge.notification_clicked(ext_id, note_id))

    def _extension_menu_items(self, menu: QMenu, tab: Tab, request: QWebEngineContextMenuRequest) -> None:
        """chrome.contextMenus items that fit what was right-clicked."""
        MT = QWebEngineContextMenuRequest.MediaType
        link, media, selected = QUrl(request.linkUrl()), QUrl(request.mediaUrl()), request.selectedText()
        kinds = {MT.MediaTypeImage: "image", MT.MediaTypeVideo: "video", MT.MediaTypeAudio: "audio"}
        contexts = {c for c, on in (("link", not link.isEmpty()), ("selection", bool(selected.strip())),
                                    ("editable", request.isContentEditable()), (kinds.get(request.mediaType()), True)) if c and on}
        contexts = contexts or {"page"}
        page_url, target = tab.url().toString(), (link if not link.isEmpty() else media).toString()
        info = {"editable": request.isContentEditable(), "pageUrl": page_url, "frameUrl": page_url}
        if not link.isEmpty():
            info["linkUrl"] = link.toString()
        if request.mediaType() in kinds:
            info.update(srcUrl=media.toString(), mediaType=kinds[request.mediaType()])
        if selected.strip():
            info["selectionText"] = selected

        def fits(item: dict) -> bool:
            wanted = set(_strings(item.get("contexts")) or ["page"])
            if not (wanted & contexts or "all" in wanted):
                return False
            documents, targets = _strings(item.get("documentUrlPatterns")), _strings(item.get("targetUrlPatterns"))
            if documents and not any(match_pattern(p, page_url) for p in documents):
                return False
            return not (targets and target and not any(match_pattern(p, target) for p in targets))

        added = False
        for entry in self.extensions.entries():
            items = [m for m in self.extensions.bridge.menus(entry.id) if m.get("visible") is not False] if entry.enabled else []
            top = [m for m in items if m.get("parentId") is None and fits(m)]
            if not top:
                continue
            if not added:
                menu.addSeparator()
                added = True
            if len(top) > 1:  # several items: grouped under the extension, like Chrome
                self._add_extension_menu_items(menu.submenu(menu_text(entry.name), entry.icon), entry, items, top, fits, info, tab)
            else:
                self._add_extension_menu_items(menu, entry, items, top, fits, info, tab, entry.icon)

    def _add_extension_menu_items(self, menu: QMenu, entry: ExtensionEntry, items: list, level: list, fits, info: dict,
                                  tab, icon_: QIcon | None = None) -> None:
        selection = " ".join(str(info.get("selectionText", "")).split())
        for item in level:
            kind = item.get("type") or "normal"
            if kind == "separator":
                menu.addSeparator()
                continue
            title = menu_text(str(item.get("title") or "").replace("%s", elide(selection, 32)))
            children = [m for m in items if m.get("parentId") == item.get("id") and fits(m)]
            if children:
                sub = Menu(title, menu)
                if icon_ is not None:
                    sub.setIcon(icon_)
                menu.addMenu(sub)
                self._add_extension_menu_items(sub, entry, items, children, fits, info, tab)
                continue
            action = menu.addAction(title)
            if icon_ is not None:
                action.setIcon(icon_)
            action.setEnabled(item.get("enabled") is not False)
            if kind in ("checkbox", "radio"):
                action.setCheckable(True)
                action.setChecked(bool(item.get("checked")))
            details = {**info, "menuItemId": item.get("id")}
            if item.get("parentId") is not None:
                details["parentMenuItemId"] = item["parentId"]
            action.triggered.connect(lambda *_, e=entry.id, it=item, d=details: self.extensions.bridge.menu_clicked(e, it, dict(d), tab))

    def _fill_extensions_menu(self, menu: QMenu) -> None:
        reset_menu(menu)
        entries = [e for e in self.extensions.entries() if e.enabled]
        for entry in entries:
            action = menu.addAction(entry.icon, menu_text(entry.name))
            action.triggered.connect(lambda *_, e=entry.id: self.open_extension(e, self.extensions_button))
        if not entries:
            menu.addAction("No extensions enabled").setEnabled(False)
        menu.addSeparator()
        menu.addAction(self.act_extensions)
        menu.addAction("Find More Extensions (Chrome Web Store)", lambda: self.open_url(QUrl(WEBSTORE_HOME), "tab"))
        menu.addAction("Install Extension from File…", self.install_extension_file)
        menu.addAction("Load Unpacked Extension Folder…", self.install_extension_folder)

    def install_extension_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Install Extension", str(Path.home() / "Downloads"),
                                              "Chrome extensions (*.crx *.zip);;All files (*)")
        if path:
            self.extensions.install_from_path(path)

    def install_extension_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Load Unpacked Extension (folder with manifest.json)", str(Path.home()))
        if path:
            self.extensions.install_from_path(path)

    def confirm_remove_extension(self, ext_id: str, name: str) -> None:
        if ask_question(self, "Remove Extension", f"Remove “{name}” from {APP_NAME}?", "Remove"):
            self.extensions.uninstall(ext_id)

    # ── downloads ───────────────────────────────────────────────────────────────────────
    def _on_download_requested(self, request: QWebEngineDownloadRequest) -> None:
        if self._closing:
            request.cancel()
            return
        if request.isSavePageDownload():
            base = safe_filename(request.page().title() if request.page() else "", "page")
            path, chosen = QFileDialog.getSaveFileName(
                self, "Save Page As", str(self.settings.downloads_dir() / f"{base}.html"),
                "Web Page, complete (*.html);;Web Archive, single file (*.mhtml);;Web Page, HTML only (*.html)")
            if not path:
                request.cancel()
                return
            formats = QWebEngineDownloadRequest.SavePageFormat
            if "single file" in chosen:
                request.setSavePageFormat(formats.MimeHtmlSaveFormat)
            elif "HTML only" in chosen:
                request.setSavePageFormat(formats.SingleHtmlSaveFormat)
            else:
                request.setSavePageFormat(formats.CompleteHtmlSaveFormat)
            target = Path(path)
        elif self.settings.get("ask_download_location"):
            suggested = self.settings.downloads_dir() / safe_filename(request.downloadFileName())
            path, _ = QFileDialog.getSaveFileName(self, "Save File", str(suggested))
            if not path:
                request.cancel()
                return
            target = Path(path)
        else:
            directory = self.settings.downloads_dir()
            target = unique_path(directory, safe_filename(request.downloadFileName()))
        request.setDownloadDirectory(str(target.parent))
        request.setDownloadFileName(target.name)
        request.accept()
        item = DownloadItem(request)
        item.changed.connect(self._update_download_button)
        self.downloads.append(item)
        self.download_button.show()
        self._update_download_button()
        if len(self.downloads) == 1 or not any(i.active for i in self.downloads[:-1]):
            QTimer.singleShot(150, self.show_downloads)

    def _update_download_button(self) -> None:
        active = [i for i in self.downloads if i.active]
        if active:
            total = sum(i.total for i in active if i.total > 0)
            received = sum(i.received for i in active if i.total > 0)
            self.download_button.progress = received / total if total > 0 else 0.0
        else:
            if self.download_button.progress >= 0:
                self.download_button.attention = True
            self.download_button.progress = -1.0
        self.download_button.update()

    def show_downloads(self) -> None:
        self.download_button.attention = False
        self.download_button.show()
        self.download_button.update()
        panel = DownloadsPanel(self)
        panel.popup_at(self.download_button if self.download_button.isVisible() else self.menu_button)

    def clear_finished_downloads(self) -> None:
        self.downloads = [i for i in self.downloads if i.active]
        if not self.downloads:
            self.download_button.hide()

    # ── context menus ───────────────────────────────────────────────────────────────────
    def show_page_context_menu(self, tab: Tab, global_pos: QPoint) -> None:
        request = tab.view.lastContextMenuRequest()
        if request is None:
            return
        page = tab.page
        WA = QWebEnginePage.WebAction
        MT = QWebEngineContextMenuRequest.MediaType
        link = QUrl(request.linkUrl())
        media = QUrl(request.mediaUrl())
        media_type = request.mediaType()
        selected = " ".join(request.selectedText().split())
        editable = request.isContentEditable()
        clipboard = QGuiApplication.clipboard()
        menu = Menu("", tab.view)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if not link.isEmpty() and link.isValid():
            menu.addAction("Open Link in New Tab", lambda: self.open_url(link, "background", opener=tab))
            menu.addAction("Open Link in This Tab", lambda: tab.load(link))
            menu.addSeparator()
            link_text = request.linkText().strip()
            menu.addAction("Bookmark Link…", lambda: self.add_bookmark_dialog(link.toString(), link_text or link.toString()))
            menu.addAction("Save Link As…", lambda: page.triggerAction(WA.DownloadLinkToDisk))
            menu.addAction("Copy Link", lambda: clipboard.setText(link.toString()))
            menu.addSeparator()
        if media_type == MT.MediaTypeImage:
            menu.addAction("Open Image in New Tab", lambda: self.open_url(media, "background", opener=tab))
            menu.addAction("Save Image As…", lambda: page.triggerAction(WA.DownloadImageToDisk))
            menu.addAction("Copy Image", lambda: page.triggerAction(WA.CopyImageToClipboard))
            menu.addAction("Copy Image Link", lambda: clipboard.setText(media.toString()))
            menu.addSeparator()
        elif media_type in (MT.MediaTypeVideo, MT.MediaTypeAudio):
            noun = "Video" if media_type == MT.MediaTypeVideo else "Audio"
            menu.addAction("Play / Pause", lambda: page.triggerAction(WA.ToggleMediaPlayPause))
            menu.addAction("Mute / Unmute", lambda: page.triggerAction(WA.ToggleMediaMute))
            menu.addAction("Loop", lambda: page.triggerAction(WA.ToggleMediaLoop))
            menu.addAction("Show Controls", lambda: page.triggerAction(WA.ToggleMediaControls))
            menu.addSeparator()
            menu.addAction(f"Save {noun} As…", lambda: page.triggerAction(WA.DownloadMediaToDisk))
            menu.addAction(f"Copy {noun} Link", lambda: clipboard.setText(media.toString()))
            menu.addSeparator()
        if editable:
            for action, text in ((WA.Undo, "Undo"), (WA.Redo, "Redo"), (None, ""), (WA.Cut, "Cut"), (WA.Copy, "Copy"),
                                 (WA.Paste, "Paste"), (WA.PasteAndMatchStyle, "Paste Without Formatting"), (None, ""),
                                 (WA.SelectAll, "Select All")):
                if action is None:
                    menu.addSeparator()
                    continue
                entry = menu.addAction(text, lambda a=action: page.triggerAction(a))
                entry.setEnabled(page.action(action).isEnabled())
        elif selected:
            menu.addAction("Copy", lambda: page.triggerAction(WA.Copy))
            engine = self.settings.get("search_engine")
            query = selected
            menu.addAction(menu_text(f"Search {engine} for “{elide(query, 28)}”"),
                           lambda: self.open_url(QUrl(self.settings.search_template().format(quote_plus(query))), "tab", opener=tab))
        if not (editable or selected or not link.isEmpty() or media_type != MT.MediaTypeNone):
            history = page.history()
            menu.addAction("Back", lambda: page.triggerAction(WA.Back)).setEnabled(history.canGoBack())
            menu.addAction("Forward", lambda: page.triggerAction(WA.Forward)).setEnabled(history.canGoForward())
            menu.addAction("Reload", lambda: page.triggerAction(WA.Reload))
            menu.addSeparator()
            if display_url(tab.url()):
                menu.addAction("Bookmark Page…", lambda: self.add_bookmark_dialog(tab.url().toString(), tab.title()))
            menu.addAction("Save Page As…", lambda: page.triggerAction(WA.SavePage))
            menu.addAction("Select All", lambda: page.triggerAction(WA.SelectAll))
            menu.addSeparator()
            menu.addAction("View Page Source", self.view_source)
        self._extension_menu_items(menu, tab, request)
        menu.addSeparator()
        menu.addAction("Inspect", lambda: tab.open_devtools(inspect=True))
        menu.popup(global_pos)

    def _tab_context_menu(self, pos: QPoint) -> None:
        index = self.tab_bar.tabAt(pos)
        tab = self.tab_at(index)
        menu = Menu("", self)
        menu.addAction(self.act_new_tab)
        if tab is not None:
            menu.addSeparator()
            menu.addAction("Reload Tab", lambda: (tab.ensure_loaded(), tab.page.triggerAction(QWebEnginePage.WebAction.Reload)))
            muted = tab.audio_state() == "muted"
            menu.addAction("Unmute Tab" if muted else "Mute Tab", lambda: self.toggle_mute(tab))
            menu.addAction("Duplicate Tab", lambda: self.duplicate_tab(tab))
            if display_url(tab.url()):
                menu.addAction("Bookmark Tab…", lambda: self.add_bookmark_dialog(tab.url().toString(), tab.title()))
            menu.addSeparator()
            menu.addAction("Close Tab", lambda: self.close_tab(tab))
            others = [t for t in self.tabs() if t is not tab]
            close_others = menu.addAction("Close Other Tabs", lambda: [self.close_tab(t) for t in others])
            close_others.setEnabled(bool(others))
            right = [self.tab_at(i) for i in range(index + 1, self.tab_bar.count())]
            close_right = menu.addAction("Close Tabs to the Right", lambda: [self.close_tab(t) for t in right if t is not None])
            close_right.setEnabled(bool(right))
        menu.addSeparator()
        menu.addAction(self.act_reopen)
        self.act_reopen.setEnabled(bool(self.closed_tabs))
        menu.exec(self.tab_bar.mapToGlobal(pos))
        menu.deleteLater()
        self.act_reopen.setEnabled(True)

    def _fill_tab_list(self) -> None:
        menu = self.tab_strip.list_button.menu()
        reset_menu(menu)
        current = self.current_tab()
        for tab in self.tabs():
            action = menu.addAction(tab.favicon(), menu_text(elide(tab.title(), 70)))
            action.setCheckable(True)
            action.setChecked(tab is current)
            action.triggered.connect(lambda *_, t=tab: self.tab_bar.setCurrentIndex(self.index_of(t)))

    def _fill_history_app_menu(self, menu: QMenu, include_navigation: bool = False) -> None:
        reset_menu(menu)
        if include_navigation:
            menu.addAction(self.act_back)
            menu.addAction(self.act_forward)
            menu.addSeparator()
        menu.addAction(self.act_history)
        menu.addAction(self.act_clear_data)
        menu.addSeparator()
        closed = Menu("Recently Closed Tabs", menu)
        for entry in reversed(self.closed_tabs[-15:]):
            action = closed.addAction(self.favicons.get(entry.get("url", "")),
                                      menu_text(elide(entry.get("title") or entry.get("url", ""), 60)))
            action.triggered.connect(lambda *_, e=entry: self._reopen_entry(e))
        closed.setEnabled(bool(self.closed_tabs))
        menu.addMenu(closed)
        menu.addSeparator()
        for url, title, _last, _count in self.history.recent(limit=15):
            action = menu.addAction(self.favicons.get(url), menu_text(elide(title or url, 60)))
            action.triggered.connect(lambda *_, u=url: self.open_url(QUrl(u), "current"))

    def _reopen_entry(self, entry: dict) -> None:
        if entry in self.closed_tabs:
            self.closed_tabs.remove(entry)
        self.new_tab(entry=entry).ensure_loaded()

    # ── windows & dialogs ───────────────────────────────────────────────────────────────
    def _single_dialog(self, key: str, factory) -> None:
        dialog = self._dialogs.get(key)
        if dialog is None or sip.isdeleted(dialog):
            dialog = factory()
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._dialogs[key] = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def show_bookmarks_manager(self) -> None:
        self._single_dialog("bookmarks", lambda: BookmarksManager(self))

    def show_history(self) -> None:
        self._single_dialog("history", lambda: HistoryDialog(self))

    def show_extensions(self) -> None:
        self._single_dialog("extensions", lambda: ExtensionsDialog(self))

    def show_settings(self) -> None:
        self._single_dialog("settings", lambda: SettingsDialog(self))

    def apply_force_dark(self) -> None:
        # Takes effect as pages load; open tabs aren't reloaded so nothing typed into them is lost.
        self.profile.settings().setAttribute(QWebEngineSettings.WebAttribute.ForceDarkMode, self.settings.get("force_dark_pages"))

    def clear_history_traces(self) -> None:
        """Clear history everywhere it lives: the database, closed tabs, back/forward lists and site icons."""
        self.history.clear()
        self.profile.clearAllVisitedLinks()
        self.closed_tabs.clear()
        self.favicons.clear()
        for tab in self.tabs():
            if tab.pending is not None:
                tab.pending.pop("history", None)
            else:
                tab.page.history().clear()
        current = self.current_tab()
        if current is not None:
            self._update_nav_buttons(current)
        self.save_session()

    # ── VPN / proxy ─────────────────────────────────────────────────────────────────────
    def _update_vpn_button(self) -> None:
        if self.vpn is not None:
            self.vpn_button.setIcon(icon("shield-on", P.ACCENT))
            self.vpn_button.setToolTip(f"VPN on: {self.vpn.label}")
        else:
            self.vpn_button.setIcon(icon("shield", P.TEXT_2))
            self.vpn_button.setToolTip("VPN off - click to set one up")

    def show_vpn_panel(self, toggle: bool = False) -> None:
        panel = self.findChild(VpnPanel, options=Qt.FindChildOption.FindDirectChildrenOnly)
        if panel is not None and not sip.isdeleted(panel) and panel.isVisible():
            if toggle:
                panel.close()
            else:
                panel.raise_()
            return
        VpnPanel(self).popup_at(self.vpn_button if self.vpn_button.isVisible() else self.menu_button)

    def restart_browser(self) -> None:
        """Restart Foxglove (how VPN changes are applied); session restore brings every tab back."""
        self.restart_request["requested"] = True
        if not self.close():  # e.g. the user chose to keep running downloads
            self.restart_request["requested"] = False

    def turn_vpn_off(self) -> None:
        cfg = sanitize_vpn(self.settings.get("vpn"))
        cfg["mode"] = "off"
        self.settings.set("vpn", cfg)
        self.restart_browser()

    def _warn_vpn_unreachable(self, tab: Tab) -> None:
        if self._vpn_warned or self.vpn is None:
            return
        self._vpn_warned = True
        bar = InfoBar(icon("shield", P.WARNING),
                      f"<b>Can't connect through the VPN.</b> {html.escape(self.vpn.label)} isn't answering, so "
                      "pages won't load (your real connection is never used instead). Make sure it's running.",
                      "warning")
        bar.add_button("VPN Settings", lambda: (bar.dismiss(), self.show_vpn_panel()))
        bar.add_button("Turn VPN Off", lambda: (bar.dismiss(), self.turn_vpn_off()), primary=True)
        tab.add_bar(bar)

    def _show_site_info(self) -> None:
        tab = self.current_tab()
        if tab is not None and display_url(tab.url()):
            SiteInfoPanel(self, tab.url()).popup_at(self.url_bar)
        else:
            self.focus_url_bar()

    # ── site settings: permissions, cookies and stored data, per site ───────────────────
    def show_site_settings(self, url: QUrl | None = None) -> None:
        """Site settings for *url*'s site (default: the current tab's, if it shows a website), else every site."""
        if url is None:
            tab = self.current_tab()
            url = tab.url() if tab is not None else QUrl()
        self._single_dialog("sites", lambda: SiteSettingsDialog(self))
        dialog = self._dialogs["sites"]
        origin = origin_of(url)
        dialog.show_site(site_of(url.host()), origin) if origin else dialog.show_all()

    def permission_state(self, origin: str, kind) -> str:
        """"allow", "block" or "ask": what Foxglove does when *origin* ("https://host[:port]") asks for *kind*."""
        if QWebEnginePermission.isPersistent(kind):
            state = self.profile.queryPermission(QUrl(origin), kind).state()
            return {QWebEnginePermission.State.Granted: "allow", QWebEnginePermission.State.Denied: "block"}.get(state, "ask")
        entry = self.settings.get("site_permissions").get(origin)
        state = entry.get(kind.name) if isinstance(entry, dict) else None
        return state if state == "block" or (state == "allow" and kind not in ASK_ALWAYS) else "ask"

    def set_permission_state(self, origin: str, kind, state: str) -> None:
        if QWebEnginePermission.isPersistent(kind):  # Qt keeps these (on disk: StoreOnDisk)
            permission = self.profile.queryPermission(QUrl(origin), kind)
            {"allow": permission.grant, "block": permission.deny}.get(state, permission.reset)()
            return
        decisions = {o: dict(d) for o, d in self.settings.get("site_permissions").items() if isinstance(d, dict)}
        entry = decisions.setdefault(origin, {})
        if state == "block" or (state == "allow" and kind not in ASK_ALWAYS):
            entry[kind.name] = state
        else:
            entry.pop(kind.name, None)
        if not entry:
            del decisions[origin]
        self.settings.set("site_permissions", decisions)

    def remembered_decision(self, permission: QWebEnginePermission) -> bool | None:
        """Your earlier answer to a request Qt doesn't remember itself (camera, microphone...), or None to ask."""
        kind, origin = permission.permissionType(), origin_of(permission.origin())
        if not origin or QWebEnginePermission.isPersistent(kind):
            return None
        states = {self.permission_state(origin, part) for part in PERMISSION_PARTS.get(kind, (kind,))}
        return False if "block" in states else True if states == {"allow"} else None

    def remember_decision(self, permission: QWebEnginePermission, allow: bool) -> None:
        """Keep an Allow/Block answer for camera, microphone and pointer lock, as Qt keeps the others (never screen sharing)."""
        kind, origin = permission.permissionType(), origin_of(permission.origin())
        if origin and not QWebEnginePermission.isPersistent(kind) and kind not in ASK_ALWAYS:
            for part in PERMISSION_PARTS.get(kind, (kind,)):
                self.set_permission_state(origin, part, "allow" if allow else "block")

    def permission_decisions(self) -> list[tuple[str, object, str]]:
        """Every remembered answer: (origin, permission type, "allow" or "block")."""
        found = []
        for permission in self.profile.listAllPermissions():
            state, kind = permission.state(), permission.permissionType()
            if state != QWebEnginePermission.State.Ask and kind in PERMISSION_TEXT and origin_of(permission.origin()):
                found.append((origin_of(permission.origin()), kind, "allow" if state == QWebEnginePermission.State.Granted else "block"))
        for origin in self.settings.get("site_permissions"):
            for kind, _label in SITE_PERMISSIONS:
                if not QWebEnginePermission.isPersistent(kind) and (state := self.permission_state(origin, kind)) != "ask":
                    found.append((origin, kind, state))
        return found

    def reset_site_permissions(self, site: str | None = None) -> None:
        for origin, kind, _state in self.permission_decisions():
            if site is None or site_of(QUrl(origin).host()) == site:
                self.set_permission_state(origin, kind, "ask")

    def site_origins(self, site: str | None = None, stored: bool = False) -> list[str]:
        """The origins of *site* (None: of every site) Foxglove knows of - open pages, history, permissions, cookies and,
        if *stored*, Chromium's storage folders. Stored data belongs to origins, and Qt can't list the ones that have some."""
        urls = [t.url() for t in self.tabs()] + [p.url() for p in self.popups]
        urls += [QUrl(row[0]) for row in self.history.recent(limit=5000)]
        found = dict.fromkeys(origin_of(url) for url in urls)
        found.update(dict.fromkeys(origin for origin, _kind, _state in self.permission_decisions()))
        found.update(dict.fromkeys(f"https://{c.domain().lstrip('.')}" for c in self.cookie_index.cookies.values()))
        if stored and not self.profile.isOffTheRecord():
            found.update(dict.fromkeys(stored_origins(Path(self.profile.persistentStoragePath()))))
        return [o for o in found if o and (site is None or site_of(QUrl(o).host()) == site)]

    def clear_site_data(self, site: str | None = None, done=None, origins: list[str] | None = None,
                        thorough: bool = True) -> SiteDataCleaner:
        """Delete *site*'s cookies and stored data (None: every site's), or finish clearing *origins* (their cookies went
        already; *thorough*: service workers too). For one site its service workers go too. Tabs showing what was
        cleared reload, to let go of what they hold. Clearing every site takes a while: origins you load again
        meanwhile are left alone (what they store now is new), and if Foxglove quits first, the next start finishes."""
        index, everything = self.cookie_index, site is None and origins is None
        if origins is None:
            origins = self.site_origins(site, stored=True)  # (the open tabs' first)
            index.delete_all() if site is None else index.delete(index.for_site(site))
        cleaner = SiteDataCleaner(self.profile, origins, thorough=not everything and (site is not None or thorough),
                                  parent=self, skip_visited=everything)
        cleaner.everything = everything
        self._cleaners.append(cleaner)

        def finished() -> None:
            if cleaner in self._cleaners:
                self._cleaners.remove(cleaner)
            for tab in self.tabs():
                origin = origin_of(tab.url())
                if tab.pending is None and origin in cleaner.done and not (everything and origin in cleaner.visited):
                    tab.page.triggerAction(QWebEnginePage.WebAction.Reload)
            cleaner.deleteLater()
            if done is not None:
                done()

        cleaner.finished.connect(finished)
        cleaner.start()
        return cleaner

    def site_visited(self, url: QUrl) -> None:
        if self._cleaners and (origin := origin_of(url)):
            for cleaner in self._cleaners:
                cleaner.visit(origin)

    def save_pending_clearing(self) -> None:
        """Clearing still running is finished at the next start (see finish_clearing)."""
        running = [c for c in self._cleaners if not sip.isdeleted(c)]
        if running:
            path = self.session_path.with_name(PENDING_CLEAR)
            data = read_json(path, {})
            data = data if isinstance(data, dict) else {}
            old = {key: [o for o in data.get(key) or [] if isinstance(o, str)] for key in ("origins", "storage")}
            wipe = any(c.everything and not c.visited for c in running)  # nothing new to keep: delete it all
            write_json(path, {"all": bool(data.get("all")) or wipe,
                              "origins": list(dict.fromkeys(old["origins"] + [o for c in running if not c.everything
                                                                              for o in c.pending()])),
                              "storage": list(dict.fromkeys(old["storage"] + [o for c in running if c.everything
                                                                              for o in c.pending()]))})

    def show_about(self) -> None:
        QMessageBox.about(self, f"About {APP_NAME}",
                          f"<h3>{APP_NAME} {APP_VERSION}</h3><p>A Chrome-style browser written in Python.</p>"
                          f"<p>Qt {QT_VERSION_STR} · Chromium {qWebEngineChromiumVersion()}<br>"
                          f"Python {sys.version.split()[0]}</p>")

    def show_shortcuts(self) -> None:
        rows = [
            ("New tab / close tab", "Ctrl+T", "Ctrl+W"), ("Reopen closed tab", "Ctrl+Shift+T", None),
            ("Next / previous tab", self.act_next_tab.shortcut(), self.act_prev_tab.shortcut()),
            ("Address bar", "Ctrl+L", None), ("Reload / hard reload", "Ctrl+R", "Ctrl+Shift+R"),
            ("Back / forward", self.act_back.shortcut(), self.act_forward.shortcut()),
            ("Find in page", "Ctrl+F", None), ("Zoom in / out / reset", "Ctrl++", "Ctrl+-"),
            ("Bookmark page", "Ctrl+D", None), ("Bookmarks toolbar / manager", "Ctrl+Shift+B", "Ctrl+Shift+O"),
            ("History / downloads", "Ctrl+Shift+H", "Ctrl+Shift+Y"), ("Extensions", "Ctrl+Shift+A", None),
            ("Developer tools", self.act_devtools.shortcut(), None), ("Full screen", self.act_fullscreen.shortcut(), None),
        ]
        body = "".join(
            f"<tr><td style='padding:3px 18px 3px 0'>{html.escape(name)}</td><td><b>{html.escape(shortcut_text(a))}</b>"
            + (f" / <b>{html.escape(shortcut_text(b))}</b>" if b else "") + "</td></tr>"
            for name, a, b in rows)
        QMessageBox.information(self, "Keyboard Shortcuts", f"<table>{body}</table>")

    def toast(self, text: str, kind: str = "success") -> None:
        toast = self.content.toast
        if self._closing or sip.isdeleted(toast):  # (a message that comes as the window goes - an answer to a question it asked)
            return
        toast.show_message(text, kind)

    # ── printing ────────────────────────────────────────────────────────────────────────
    def print_page(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        if self._printer is not None:
            self.toast("Still printing the previous page…", "info")
            return
        tab.ensure_loaded()
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        if not run_dialog(QPrintDialog(printer, self)):
            return
        self._printer = printer
        # If the tab closes mid-print its "finished" signal never comes; free the printer with the tab.
        tab.destroyed.connect(self._release_printer)
        tab.view.print(printer)

    def _release_printer(self, *_args) -> None:
        self._printer = None

    def _on_print_finished(self, ok: bool) -> None:
        self._release_printer()
        if not ok:
            self.toast("Printing failed.", "error")

    # ── session ─────────────────────────────────────────────────────────────────────────
    def schedule_session_save(self) -> None:
        if not self._closing:
            self._session_timer.start()

    def session_data(self) -> dict:
        return {
            "version": 1,
            "saved": time.time(),
            "current": self.tab_bar.currentIndex(),
            "tabs": [tab.session_entry() for tab in self.tabs()],
            "closed_tabs": self.closed_tabs[-MAX_CLOSED_TABS:],
            "geometry": bytes(self.saveGeometry().toBase64()).decode("ascii"),
        }

    def save_session(self) -> None:
        if not self.tab_bar.count():
            return
        data = self.session_data()
        fingerprint = json.dumps({k: v for k, v in data.items() if k != "saved"}, sort_keys=True)
        if fingerprint != self._last_session and write_json(self.session_path, data):
            self._last_session = fingerprint  # skip rewriting an unchanged session every autosave

    def _restore(self, startup_urls: list[str]) -> None:
        data = read_json(self.session_path, {})
        data = data if isinstance(data, dict) else {}
        geometry = data.get("geometry")
        restored_geometry = False
        if isinstance(geometry, str):
            restored_geometry = self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        if not restored_geometry:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            self.resize(min(1440, int(screen.width() * 0.86)), min(920, int(screen.height() * 0.9)))
            self.move(screen.center() - self.rect().center())
        closed = data.get("closed_tabs")
        self.closed_tabs = [e for e in closed if isinstance(e, dict)] if isinstance(closed, list) else []
        entries = data.get("tabs") if self.settings.get("restore_session") else None
        entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
        if entries:
            current = clamp(int(data.get("current", 0)) if str(data.get("current", 0)).lstrip("-").isdigit() else 0,
                            0, len(entries) - 1)
            for entry in entries:  # restored tabs stay unloaded until you open them, like Firefox
                self.new_tab(entry=entry, background=True, activate=False)
            self.tab_bar.setCurrentIndex(current)
            self._on_current_changed(current)
        for text in startup_urls:
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", text) and os.path.exists(text):
                url = QUrl.fromLocalFile(os.path.abspath(text))  # e.g. "python3 foxglove.py page.html"
            else:
                url = url_from_input(text, self.settings.search_template())
            if url.isValid() and not url.isEmpty():
                self.new_tab(url)
        if not self.tab_bar.count():
            self.new_tab(self._home_url())

    # ── quitting ────────────────────────────────────────────────────────────────────────
    def close_now(self) -> None:
        self._force_close = True
        self.close()

    def closeEvent(self, event) -> None:
        if self._closing:
            event.accept()
            return
        active = [i for i in self.downloads if i.active]
        if active and not self._force_close:
            answer = QMessageBox.question(
                self, "Downloads in Progress",
                f"{len(active)} download{'s are' if len(active) > 1 else ' is'} still in progress. Quit and cancel "
                f"{'them' if len(active) > 1 else 'it'}?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if self._fullscreen_tab is not None:
            self._leave_html_fullscreen()
        self.save_session()  # the important part: tabs + history are written before anything is torn down
        self.save_pending_clearing()
        self._closing = True
        self._session_timer.stop()
        self._autosave.stop()
        self.bookmarks.flush()
        self.settings.save()
        self.extensions.save()
        for popup in list(self.popups):
            popup.close()
        for dialog in self._dialogs.values():
            if not sip.isdeleted(dialog):
                dialog.close()
        for tab in self.tabs():
            tab.shutdown()
        event.accept()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Start-up
# ══════════════════════════════════════════════════════════════════════════════════════════
def register_url_schemes() -> None:
    """foxglove:// (internal pages) and foxglove-ext:// (the extension bridge) - must happen before QApplication."""
    F = QWebEngineUrlScheme.Flag
    # foxglove: local (no other scheme may load it), fetch() for the New Tab page's own API
    for name, flags in ((b"foxglove", F.SecureScheme | F.LocalScheme | F.LocalAccessAllowed | F.FetchApiAllowed),
                        # service workers may only fetch() schemes flagged ServiceWorkersAllowed; LocalScheme would block it
                        (EXT_SCHEME.encode(), F.SecureScheme | F.CorsEnabled | F.FetchApiAllowed | F.ServiceWorkersAllowed)):
        if QWebEngineUrlScheme.schemeByName(name).name().isEmpty():
            scheme = QWebEngineUrlScheme(name)
            scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
            scheme.setFlags(flags)
            QWebEngineUrlScheme.registerScheme(scheme)


def set_application_names() -> Path:
    """Qt's application name stays DATA_NAME, so the data folder (and Qt WebEngine's profile and extension folders
    in it) are found where they always were; people see APP_NAME (QApplication.setApplicationDisplayName).
    Returns the data folder."""
    QCoreApplication.setApplicationName(DATA_NAME)
    QCoreApplication.setOrganizationName("")
    QCoreApplication.setApplicationVersion(APP_VERSION)
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))


def set_macos_app_name(name: str) -> None:
    """Show *name* ("Chrome 2") instead of "Python" in the macOS menu bar (needs to run before QApplication)."""
    if not IS_MAC:
        return
    try:
        import ctypes
        import ctypes.util
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
        send_str = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(("objc_msgSend", objc))
        send_set = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
        bundle = send(objc.objc_getClass(b"NSBundle"), objc.sel_registerName(b"mainBundle"))
        info = send(bundle, objc.sel_registerName(b"infoDictionary")) if bundle else None
        if not info:
            return
        string_class = objc.objc_getClass(b"NSString")
        utf8 = objc.sel_registerName(b"stringWithUTF8String:")
        value = send_str(string_class, utf8, name.encode("utf-8"))
        key = send_str(string_class, utf8, b"CFBundleName")
        send_set(info, objc.sel_registerName(b"setObject:forKey:"), value, key)
    except Exception:  # purely cosmetic
        pass


# ── Looking like the desktop Chrome Foxglove is built on ─────────────────────────────────────────────────────────────
# Google sign-in ("This browser or app may not be secure") and bot checks distrust browsers that look embedded or
# automated. Qt WebEngine is Chrome's engine but by default it announces itself as "Chromium" only (no "Google Chrome"
# brand), sends no Accept-Language header and lacks the chrome.loadTimes/csi/app every Chrome page has. Nothing is
# disguised beyond that: bot checks look hardest for tampering (a wrapped Function.prototype.toString, a patched getter
# that workers don't see), so the stand-ins are plain functions. Nothing here solves CAPTCHAs or automates anything.
CHROME_SCRIPT = "chrome"  # (shows in stack traces as userscript:<name>)
CHROME_SHAPE_JS = r"""(() => {
  if (!/^(https?|file|about|blob|data):$/.test(location.protocol)) return;  // not extension or Foxglove pages
  const chrome = window.chrome || (window.chrome = {});
  if ("loadTimes" in chrome) return;  // already done (or a real Chrome binding)
  // What they use is taken now: page scripts can't step into them later, and their errors start at the caller.
  const perf = performance, origin = perf.timeOrigin, entries = perf.getEntriesByType.bind(perf);
  const named = perf.getEntriesByName.bind(perf), now = perf.now.bind(perf), later = setTimeout;
  const capture = Error.captureStackTrace, alpn = location.protocol === "https:";
  const nav = () => entries("navigation")[0] || {};
  const at = (ms) => ms > 0 ? (origin + ms) / 1000 : 0;
  chrome.loadTimes = function () {
    const n = nav(), proto = n.nextHopProtocol || "http/1.1", paint = named("first-paint")[0], start = origin / 1000;
    return {requestTime: start, startLoadTime: start, commitLoadTime: at(n.responseStart),
      finishDocumentLoadTime: at(n.domContentLoadedEventEnd), finishLoadTime: at(n.loadEventEnd),
      firstPaintTime: at(paint && paint.startTime), firstPaintAfterLoadTime: 0,
      navigationType: {reload: "Reload", back_forward: "BackForward"}[n.type] || "Other",
      wasFetchedViaSpdy: /^h[23]/.test(proto), wasNpnNegotiated: alpn, npnNegotiatedProtocol: alpn ? proto : "unknown",
      wasAlternateProtocolAvailable: false, connectionInfo: proto};
  };
  chrome.csi = function () {
    const n = nav();
    return {startE: Math.round(origin), onloadT: Math.round(at(n.domContentLoadedEventEnd) * 1000), pageT: now(), tran: 15};
  };
  if (!("app" in chrome)) {
    const fail = (name, fn) => {
      const error = new TypeError(`Error in invocation of app.${name}()`);
      if (capture) capture(error, fn);  // like a native function's: no frame of this script
      return error;
    };
    const plain = (name, value) => {
      const fn = {[name](...args) { if (args.length) throw fail(name, fn); return value; }}[name];
      return fn;
    };
    const installState = {installState(callback) {
      if (typeof callback !== "function") throw fail("installState", installState);
      later(callback, 0, "not_installed");
    }}.installState;
    chrome.app = {isInstalled: false, getDetails: plain("getDetails", null), getIsInstalled: plain("getIsInstalled", false),
      installState, runningState: plain("runningState", "cannot_run"),
      InstallState: {DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed"},
      RunningState: {CANNOT_RUN: "cannot_run", READY_TO_RUN: "ready_to_run", RUNNING: "running"}};
  }
})();"""


def chrome_languages() -> str:
    """Chrome's Accept-Language for a fresh profile in the system's language, e.g. "en-US,en;q=0.9"."""
    first = next(iter(QLocale.system().uiLanguages()), "")
    primary = "-".join(QLocale(first).name().split("_")[:2]) if first else ""  # "en" -> "en-US", like Chrome
    if not re.fullmatch(r"[a-z]{2,3}(-[A-Z]{2}|-[0-9]{3})?", primary):  # "C"/POSIX locales
        primary = "en-US"
    base = primary.split("-")[0]
    return primary if base == primary else f"{primary},{base};q=0.9"


def _rosetta() -> bool:
    """True when an Intel build of Python (and so of Qt) runs on Apple Silicon: Chrome itself would run natively."""
    try:
        import ctypes
        sysctl = ctypes.CDLL("/usr/lib/libSystem.B.dylib").sysctlbyname
        sysctl.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        value, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
        return sysctl(b"sysctl.proc_translated", ctypes.byref(value), ctypes.byref(size), None, 0) == 0 and value.value == 1
    except (OSError, AttributeError):
        return False


def apply_browser_identity(profile: QWebEngineProfile) -> str:
    """Present *profile* consistently as the stable desktop Chrome it is built on - user agent, client hints (brands),
    Accept-Language and the window.chrome APIs - in every tab, frame and pop-up. Returns the user agent."""
    user_agent = re.sub(r"\s*QtWebEngine/\S+", "", profile.httpUserAgent())
    profile.setHttpUserAgent(user_agent)  # look like regular Chrome so sites don't serve a degraded version
    if not profile.httpAcceptLanguage():
        profile.setHttpAcceptLanguage(chrome_languages())  # by default Qt sends none at all
    hints = profile.clientHints() if hasattr(profile, "clientHints") else None  # Qt 6.8+
    if hints is not None:
        brands = {name: version for name, version in hints.fullVersionList().items() if isinstance(version, str)}
        chromium = brands.setdefault("Chromium", hints.fullVersion() or qWebEngineChromiumVersion())
        # Qt orders the brands itself: sorted, then shuffled by the major version - not Chrome's order (GREASE, Chromium,
        # Google Chrome placed by major % 6), which no input can produce. Headers and JS still agree with each other.
        brands.setdefault("Google Chrome", chromium)
        hints.setFullVersionList(brands)
        os_token = re.search(r"\((Macintosh|Windows|CrOS|Android|X11|Linux)", user_agent)
        platform = {"Macintosh": "macOS", "Windows": "Windows", "CrOS": "Chrome OS", "Android": "Android"}.get(
            os_token.group(1) if os_token else "", "Linux" if os_token else "")
        if platform and hints.platform() != platform:
            hints.setPlatform(platform)
        if IS_MAC and hints.arch() == "x86" and _rosetta():
            hints.setArch("arm")
    scripts = profile.scripts()
    for old in scripts.find(CHROME_SCRIPT):
        scripts.remove(old)
    script = QWebEngineScript()
    script.setName(CHROME_SCRIPT)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setRunsOnSubFrames(True)
    script.setSourceCode(CHROME_SHAPE_JS)
    scripts.insert(script)
    return user_agent


# ── python3 foxglove.py --install-app: an app to start Chrome 2 from the Dock / app menu ──────────────────────────────
APP_BUNDLE_ID = "local.foxglove.chrome2"
APP_EXECUTABLE = "chrome2"
ICONSET_SIZES = ((16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2))
ICNS_TYPES = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07", 256: b"ic08", 512: b"ic09", 1024: b"ic10"}


def _gui_app() -> QGuiApplication:
    """Rendering icons needs a QGuiApplication: an off-screen one if none is running (no Dock icon for it)."""
    app = QGuiApplication.instance()
    if app is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QGuiApplication([sys.argv[0] or "foxglove.py"])
    return app


def _png(size: int) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    logo_image(size, 0.08 if size >= 64 else 0.0).save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def write_icns(target: Path) -> str:
    """The logo as a macOS icon file, at every iconset size: made with iconutil when there is one (macOS), else written
    directly (PNG-based .icns, which macOS reads too). Returns how it was made."""
    _gui_app()
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for size, scale in ICONSET_SIZES:
            (iconset / f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png").write_bytes(_png(size * scale))
        iconutil = shutil.which("iconutil")
        if iconutil:
            try:
                subprocess.run([iconutil, "-c", "icns", str(iconset), "-o", str(target)], check=True, timeout=60,
                               capture_output=True)
                return "iconutil"
            except (OSError, subprocess.SubprocessError) as exc:
                log(f"iconutil couldn't make the icon ({exc}); writing it directly")
    chunks = b"".join(kind + (8 + len(png)).to_bytes(4, "big") + png
                      for kind, png in ((kind, _png(px)) for px, kind in ICNS_TYPES.items()))
    target.write_bytes(b"icns" + (8 + len(chunks)).to_bytes(4, "big") + chunks)
    return "direct"


def make_app_bundle(bundle: Path, python: str, script: str) -> Path:
    """A macOS app bundle that starts *script* with *python* (the Python - venv - it was made with), logging to
    ~/Library/Logs/Chrome 2.log. Made in a temporary folder first, then put in place of any older one."""
    import plistlib
    import shlex
    bundle.parent.mkdir(parents=True, exist_ok=True)
    staging = bundle.with_name(bundle.name + ".part")
    shutil.rmtree(staging, ignore_errors=True)
    contents = staging / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    info = {
        "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME, "CFBundleIdentifier": APP_BUNDLE_ID,
        "CFBundleExecutable": APP_EXECUTABLE, "CFBundleIconFile": "icon.icns", "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????", "CFBundleVersion": APP_VERSION, "CFBundleShortVersionString": APP_VERSION,
        "CFBundleInfoDictionaryVersion": "6.0", "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
        "NSSupportsAutomaticGraphicsSwitching": True, "LSApplicationCategoryType": "public.app-category.productivity",
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
    launcher = contents / "MacOS" / APP_EXECUTABLE
    launcher.write_text(
        "#!/bin/sh\n"
        f"# Starts {APP_NAME}; made by: python3 foxglove.py --install-app (run that again if Python or the script moves)\n"
        f'LOG="$HOME/Library/Logs/{APP_NAME}.log"\n'
        'mkdir -p "$HOME/Library/Logs"\n'
        f'echo "--- $(date): starting {APP_NAME}" >>"$LOG"\n'
        f'exec {shlex.quote(python)} {shlex.quote(script)} "$@" >>"$LOG" 2>&1\n', encoding="utf-8")
    launcher.chmod(0o755)
    write_icns(contents / "Resources" / "icon.icns")
    shutil.rmtree(bundle, ignore_errors=True)
    staging.rename(bundle)
    return bundle


def install_app(home: Path | None = None) -> int:
    """--install-app: macOS: ~/Applications/Chrome 2.app (for the Dock); Linux: an app-menu launcher."""
    home = home or Path.home()
    python, script = sys.executable, os.path.abspath(__file__)
    if IS_MAC:
        bundle = make_app_bundle(home / "Applications" / f"{APP_NAME}.app", python, script)
        lsregister = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
                          "/Support/lsregister")
        if lsregister.exists():  # so Finder and the Dock show the new icon right away
            subprocess.run([str(lsregister), "-f", str(bundle)], capture_output=True, timeout=60, check=False)
        print(f"Made {bundle}\n\n"
              f"Open it from Finder (Go > Home > Applications), Launchpad or Spotlight (type “{APP_NAME}”).\n"
              f"To keep it in the Dock: drag “{APP_NAME}” from that folder onto the Dock, or right-click its Dock icon "
              "while it runs > Options > Keep in Dock.\n"
              f"It starts {script}\nwith {python} - run --install-app again if either moves.\n"
              f"Its output goes to ~/Library/Logs/{APP_NAME}.log")
        return 0
    if sys.platform.startswith("linux"):
        data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
        icon_path = data / "icons" / "hicolor" / "256x256" / "apps" / "chrome-2.png"
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        _gui_app()
        icon_path.write_bytes(_png(256))
        desktop = data / "applications" / "chrome-2.desktop"
        desktop.parent.mkdir(parents=True, exist_ok=True)
        quoted = " ".join('"' + part.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
                          + '"' for part in (python, script))
        desktop.write_text("[Desktop Entry]\nType=Application\n"
                           f"Name={APP_NAME}\nComment=Web browser\nExec={quoted} %U\nIcon={icon_path}\n"
                           "Terminal=false\nCategories=Network;WebBrowser;\nStartupNotify=true\n"
                           f"StartupWMClass={DATA_NAME}\n", encoding="utf-8")  # (Qt's window class: the app name)
        print(f"--install-app makes a macOS app; on Linux it added {APP_NAME} to your app menu instead:\n  {desktop}")
        return 0
    print(f"--install-app makes a macOS app (~/Applications/{APP_NAME}.app). On this system, start {APP_NAME} with:\n"
          f"  {python} {script}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="foxglove.py", description=f"{APP_NAME} web browser")
    parser.add_argument("urls", nargs="*", help="pages to open")
    parser.add_argument("--profile", default="default", help="profile name (separate tabs, bookmarks and cookies)")
    parser.add_argument("--verbose", action="store_true", help="show Chromium's messages, including extensions' errors")
    parser.add_argument("--install-app", action="store_true",
                        help=f"make ~/Applications/{APP_NAME}.app (macOS) to start {APP_NAME} from the Dock, then quit")
    options, _unknown = parser.parse_known_args(argv[1:])
    if options.install_app:
        return install_app()
    profile_name = re.sub(r"[^A-Za-z0-9_.-]", "_", options.profile) or "default"
    global VERBOSE
    VERBOSE = options.verbose

    data_root = set_application_names()
    profile_dir = data_root / "Profiles" / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    settings_preview = read_json(profile_dir / "settings.json", {})
    settings_preview = settings_preview if isinstance(settings_preview, dict) else {}
    appearance = settings_preview.get("website_appearance", "dark")
    vpn = vpn_endpoint(sanitize_vpn(settings_preview.get("vpn")))

    original_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS")
    flags = (original_flags or "").split()
    if appearance in ("dark", "light") and not any(f.startswith("--blink-settings") for f in flags):
        flags.append(f"--blink-settings=preferredColorScheme={0 if appearance == 'dark' else 1}")
    if not options.verbose and not any(f.startswith("--log-level") for f in flags):
        flags.append("--log-level=3")  # keep Chromium's internal chatter out of the terminal
    if vpn is not None and not any(f.startswith("--force-webrtc-ip-handling-policy") for f in flags):
        # Without this, video-call code (WebRTC) reveals your real IP address even through a proxy.
        flags.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)

    register_url_schemes()
    install_error_guard()
    set_macos_app_name(APP_NAME)
    app = QApplication(argv)
    app.setApplicationDisplayName(APP_NAME)
    app.setStyle("Fusion")
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)  # dark native title bars
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
    apply_dark_palette(app)
    app.setStyleSheet(build_stylesheet())
    app.setWindowIcon(icons().app_icon())  # (the Dock icon on macOS)
    # The VPN/proxy must be in place before the first profile exists: Qt WebEngine reads it once, and
    # switching later lets already-open connections bypass it. Changing it restarts Foxglove.
    if vpn is None:
        QNetworkProxyFactory.setUseSystemConfiguration(True)
    else:
        QNetworkProxyFactory.setUseSystemConfiguration(False)
        kind = QNetworkProxy.ProxyType.Socks5Proxy if vpn.kind == "socks5" else QNetworkProxy.ProxyType.HttpProxy
        QNetworkProxy.setApplicationProxy(QNetworkProxy(kind, vpn.host, vpn.port, vpn.username, vpn.password))

    lock = QLockFile(str(profile_dir / "lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(300):
        QMessageBox.information(None, APP_NAME, f"{APP_NAME} is already running.\n\n"
                                "Switch to the open window (only one copy can use your profile at a time).")
        return 0

    global THROBBER
    THROBBER = Throbber()

    # A disk-based profile: cookies (even session cookies), cache, permissions and extensions persist.
    # Note: we deliberately keep Qt's default storage folder - changing it after creating the profile
    # breaks Qt WebEngine's extension system.
    profile = QWebEngineProfile(profile_name, app)
    leftover = finish_clearing(profile_dir, Path(profile.persistentStoragePath()))  # before anything opens the storage
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    profile.setPersistentPermissionsPolicy(QWebEngineProfile.PersistentPermissionsPolicy.StoreOnDisk)
    user_agent = apply_browser_identity(profile)  # the same in tabs, frames and sign-in pop-ups
    CookieIndex.of(profile)  # before any page: it starts from the cookies saved last time

    settings = Settings(profile_dir / "settings.json")
    favicons = FaviconCache(profile_dir / "favicons")
    bookmarks = BookmarkStore(profile_dir / "bookmarks.json", favicons)
    history = HistoryStore(profile_dir / "history.sqlite")
    profile.setDownloadPath(str(settings.downloads_dir()))
    web_settings = profile.settings()
    attribute = QWebEngineSettings.WebAttribute
    for name, value in (("FullScreenSupportEnabled", True), ("ScrollAnimatorEnabled", True), ("PluginsEnabled", True),
                        ("PdfViewerEnabled", True), ("ScreenCaptureEnabled", True), ("DnsPrefetchEnabled", vpn is None),
                        ("JavascriptCanOpenWindows", True), ("LocalStorageEnabled", True)):
        web_settings.setAttribute(getattr(attribute, name), value)
    web_settings.setAttribute(attribute.ForceDarkMode, settings.get("force_dark_pages"))

    pages = InternalPages(NewTabPage(settings, history, favicons, app), app)
    profile.installUrlSchemeHandler(b"foxglove", pages)
    extensions = ExtensionsController(profile, profile_dir / "extensions.json", profile_dir / "extension-staging", user_agent)

    restart_request = {"requested": False}
    window = BrowserWindow(profile, settings, bookmarks, history, favicons, extensions,
                           profile_dir / "session.json", options.urls, vpn, restart_request)
    window.show()
    for origins, thorough in zip(leftover, (True, False)):
        if origins:
            window.clear_site_data(origins=origins, thorough=thorough)

    # Ctrl+C in the terminal (or a stop request from VS Code) closes the window cleanly so nothing is lost.
    def handle_signal(*_args) -> None:
        if not sip.isdeleted(window):
            QTimer.singleShot(0, window.close_now)

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):  # SIGHUP: the terminal running Foxglove was closed
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handle_signal)
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)  # lets Python notice Ctrl+C while Qt is running
    heartbeat.start(250)

    menubar = getattr(window, "mac_menubar", None)  # now: the window may be gone by the time exec() returns
    code = app.exec()

    # Tear down in a safe order: every web page before the profile, so Chromium shuts down cleanly and
    # flushes cookies to disk.
    heartbeat.stop()
    if not sip.isdeleted(window):  # owns every tab, pop-up, dialog and therefore every web page
        window.close_now()
        if not sip.isdeleted(window):
            sip.delete(window)
    if menubar is not None and not sip.isdeleted(menubar):
        sip.delete(menubar)
    if not sip.isdeleted(extensions):
        sip.delete(extensions)
    if not sip.isdeleted(profile):
        sip.delete(profile)
    history.close()
    # Then shut Chromium down for good: its threads finish writing cookies and site data on the way out. (Before a
    # restart this must not be left to chance - os.execv() would cut them off and lose the latest cookies.)
    sip.delete(app)
    lock.unlock()
    if restart_request["requested"]:
        restart_process(profile_name, original_flags)
    return code


def restart_process(profile_name: str, original_flags: str | None) -> None:
    """Start Chrome 2 again in this same process (same terminal, same Ctrl+C), e.g. to apply a VPN change."""
    if original_flags is None:
        os.environ.pop("QTWEBENGINE_CHROMIUM_FLAGS", None)
    else:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = original_flags
    if _icon_factory is not None:
        shutil.rmtree(_icon_factory.dir, ignore_errors=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), "--profile", profile_name])


if __name__ == "__main__":
    sys.exit(main())

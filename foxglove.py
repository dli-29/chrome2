#!/usr/bin/env python3
"""
Foxglove - a Firefox-inspired web browser written in Python (PyQt6 + Qt WebEngine / Chromium).

  * Firefox "Proton" dark look with a light-purple accent
  * Session restore: quit any time - your tabs (with their back/forward history) and your
    cookies come back next launch, so you stay signed in
  * Bookmarks: star button, bookmarks toolbar with folders, bookmark manager, HTML import/export
  * Chrome extensions (Manifest V3): one-click install from the Chrome Web Store, or from a
    .crx/.zip file or an unpacked folder; toolbar popups, options pages, enable/disable/remove
  * VPN / proxy: route the browser through Tor, Cloudflare WARP or your own HTTP/SOCKS5 proxy,
    with WebRTC leak protection, a connection check and a kill switch (shield button in the toolbar)
  * Smart address bar (history + bookmarks), downloads panel, find in page, per-site zoom,
    developer tools, printing, permission prompts, full-screen video and more

Setup (once):   python3 -m pip install --upgrade PyQt6 PyQt6-WebEngine
Run:            python3 foxglove.py            (optionally followed by URLs to open)

Your data (tabs, bookmarks, cookies, extensions) is kept in
  macOS:   ~/Library/Application Support/Foxglove
  Windows: %APPDATA%\\Foxglove
  Linux:   ~/.local/share/Foxglove
"""
from __future__ import annotations

import argparse
import atexit
import base64
import hashlib
import html
import io
import json
import os
import re
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
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote_plus

try:
    from PyQt6 import sip
    from PyQt6.QtCore import (
        QT_VERSION_STR, QBuffer, QByteArray, QCoreApplication, QDataStream, QEvent, QIODevice, QItemSelectionModel,
        QLocale, QLockFile, QObject, QPoint, QRect, QRectF, QSize, QStandardPaths, Qt, QTimer, QUrl,
        pyqtSignal,
    )
    from PyQt6.QtGui import (
        QAction, QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QIntValidator, QKeySequence, QPainter,
        QPalette, QPen, QPixmap, QStandardItem, QStandardItemModel,
    )
    from PyQt6.QtNetwork import (
        QAuthenticator, QNetworkAccessManager, QNetworkProxy, QNetworkProxyFactory, QNetworkReply, QNetworkRequest,
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
        QWebEngineNewWindowRequest, QWebEnginePage, QWebEnginePermission, QWebEngineProfile,
        QWebEngineSettings, QWebEngineUrlRequestJob, QWebEngineUrlScheme, QWebEngineUrlSchemeHandler,
        qWebEngineChromiumVersion,
    )
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError as exc:  # shown instead of a traceback when the packages are missing
    sys.exit(
        "Foxglove needs PyQt6 and PyQt6-WebEngine (6.8 or newer). Install them with:\n\n"
        "    python3 -m pip install --upgrade PyQt6 PyQt6-WebEngine\n\n"
        f"(details: {exc})"
    )

APP_NAME = "Foxglove"
APP_VERSION = "1.0"
IS_MAC = sys.platform == "darwin"
HAS_EXTENSIONS = hasattr(QWebEngineProfile, "extensionManager")  # Qt WebEngine 6.10+
TRANSLUCENT_POPUPS = sys.platform in ("darwin", "win32")
NEWTAB = "foxglove://newtab"

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
COMPONENT_EXTENSIONS = {"mhjfbmdgcfjbbpaeojofohoefgiehjai", "nkeimhogjdpnpccoofpliimaahmaaome"}

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

LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3a3846"/><stop offset="1" stop-color="#1c1b22"/></linearGradient>
<linearGradient id="fur" x1="0.2" y1="0" x2="0.8" y2="1"><stop offset="0" stop-color="#e2d6ff"/><stop offset="1" stop-color="#9b7ff5"/></linearGradient>
</defs>
<rect x="6" y="6" width="116" height="116" rx="28" fill="url(#bg)"/>
<path d="M26 24 L50 45 H78 L102 24 L105 68 L64 107 L23 68 Z" fill="url(#fur)"/>
<path d="M32 34 L45 46 L35 57 Z" fill="#7a5fd6"/>
<path d="M96 34 L83 46 L93 57 Z" fill="#7a5fd6"/>
<path d="M23 68 L51 73 L64 107 Z" fill="#f6f2ff"/>
<path d="M105 68 L77 73 L64 107 Z" fill="#f6f2ff"/>
<circle cx="49" cy="63" r="4.6" fill="#1c1b22"/>
<circle cx="79" cy="63" r="4.6" fill="#1c1b22"/>
<path d="M57.5 98.5 H70.5 L64 107 Z" fill="#1c1b22"/>
</svg>"""


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
QLabel#BookmarksHint { color: %(text_3)s; padding-left: 6px; }

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


def display_url(url: QUrl) -> str:
    if url.isEmpty() or is_newtab(url) or url.toString() == "about:blank":
        return ""
    return url.toDisplayString()


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


def parse_crx(data: bytes) -> tuple[bytes, bytes | None]:
    """Return (zip archive, developer public key) for a .crx (v2/v3) - or (data, None) for a plain zip."""
    if data[:4] != b"Cr24":
        if data[:2] == b"PK":
            return data, None
        raise InstallError("This file isn't a Chrome extension (.crx or .zip).")
    version = int.from_bytes(data[4:8], "little")
    if version == 2:
        key_len = int.from_bytes(data[8:12], "little")
        sig_len = int.from_bytes(data[12:16], "little")
        return data[16 + key_len + sig_len:], data[16:16 + key_len]
    if version == 3:
        header_len = int.from_bytes(data[8:12], "little")
        header = data[12:12 + header_len]
        keys: list[bytes] = []
        crx_id = None
        for number, wire, value in _proto_fields(header):
            if wire != 2:
                continue
            if number in (2, 3):  # sha256_with_rsa / sha256_with_ecdsa proofs
                keys += [v for n, w, v in _proto_fields(value) if n == 1 and w == 2]
            elif number == 10000:  # signed header data -> crx_id
                crx_id = next((v for n, w, v in _proto_fields(value) if n == 1 and w == 2), None)
        key = next((k for k in keys if crx_id is not None and hashlib.sha256(k).digest()[:16] == crx_id), None)
        return data[12 + header_len:], key
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
    except ValueError:  # Chrome tolerates comments and trailing commas in manifest.json
        data = json.loads(re.sub(r",(\s*[}\]])", r"\1", _strip_json_comments(raw)))
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
    root = target.resolve()
    for member in archive.infolist():
        destination = (root / member.filename).resolve()
        if destination != root and root not in destination.parents:
            raise InstallError("The extension archive contains unsafe file paths.")
    archive.extractall(root)


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
        "vpn": {"mode": "off", "type": "socks5", "host": "", "port": 1080, "username": "", "password": ""},
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


class _Relay(QObject):
    """Carries results from worker threads back to the GUI thread."""
    done = pyqtSignal(object)


class ExtensionsController(QObject):
    """Installs and manages Chrome (Manifest V3) extensions through Qt WebEngine's extension system.

    Qt keeps installed extensions in the profile and reloads them at start-up - always *disabled*,
    so we remember which ones the user enabled and switch them back on.
    """

    changed = pyqtSignal()
    message = pyqtSignal(str, str)  # text, kind ("info", "success" or "error")

    def __init__(self, profile: QWebEngineProfile, registry_path: Path, staging_dir: Path, user_agent: str):
        super().__init__()
        self.manager = profile.extensionManager() if HAS_EXTENSIONS else None
        self.registry_path = registry_path
        raw = read_json(registry_path, {})
        self.registry: dict[str, dict] = raw if isinstance(raw, dict) else {}
        self.staging = staging_dir
        restored = self._recover_interrupted_updates()
        shutil.rmtree(self.staging, ignore_errors=True)
        self.user_agent = user_agent
        self._jobs: dict[str, dict] = {}       # staging folder name -> new install in progress
        self._updates: dict[str, dict] = {}    # extension id -> update in progress
        self._loading: dict[str, dict] = {}    # extension folder -> update waiting for Qt to load it
        self._installing: set[str] = set()     # ids being installed right now (ignore duplicate requests)
        self._manifests: dict[str, tuple[float, dict]] = {}
        self._relays: set[_Relay] = set()
        self._nam = QNetworkAccessManager(self)
        if self.manager is not None:
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

    def save(self) -> None:
        write_json(self.registry_path, self.registry)

    # Queries
    def _infos(self) -> list:
        if self.manager is None:
            return []
        return [i for i in self.manager.extensions() if i.isInstalled() and i.id() not in COMPONENT_EXTENSIONS]

    def _info(self, ext_id: str):
        return next((i for i in self._infos() if i.id() == ext_id), None)

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
            state = self.registry.get(info.id(), {})
            out.append(ExtensionEntry(
                id=info.id(), name=info.name() or "Extension", description=info.description(),
                version=str(manifest.get("version", "")), path=info.path(), enabled=info.isEnabled(),
                pinned=bool(state.get("pinned", True)), popup_url=info.actionPopupUrl(),
                options_url=options_url, icon=self._icon_for(manifest, Path(info.path()))))
        out.sort(key=lambda e: e.name.lower())
        return out

    def entry(self, ext_id: str) -> ExtensionEntry | None:
        return next((e for e in self.entries() if e.id == ext_id), None)

    # Enable / disable / pin / remove
    def _on_load_finished(self, info) -> None:
        job = self._loading.pop(os.path.realpath(info.path()), None) if info.path() else None
        if job is not None:
            self._finish_update(job, info)
            return
        if not info.isLoaded():
            if info.error():
                log(f"An installed extension failed to load ({info.path()}): {info.error()}")
            return
        if info.isInstalled():
            ext_id = info.id()
            QTimer.singleShot(0, lambda: self._apply_enabled(ext_id))

    def _sync_enabled(self) -> None:
        for info in self._infos():
            self._apply_enabled(info.id())

    def _apply_enabled(self, ext_id: str) -> None:
        info = self._info(ext_id)
        if info is None:
            return
        want = bool(self.registry.get(ext_id, {}).get("enabled", True))
        if info.isEnabled() != want:
            self.manager.setExtensionEnabled(info, want)
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
        info = self._info(ext_id)
        if info is None or ext_id in self._updates:
            return
        if info.isEnabled():
            self.manager.setExtensionEnabled(info, False)
        self.manager.uninstallExtension(info)

    def _on_uninstall_finished(self, info) -> None:
        if info.error():
            self.message.emit(f"Couldn't remove the extension: {info.error()}", "error")
        else:
            self.registry.pop(info.id(), None)
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
        url = QUrl("https://clients2.google.com/service/update2/crx?response=redirect"
                   f"&prodversion={qWebEngineChromiumVersion()}&acceptformat=crx2,crx3&x=id%3D{ext_id}%26uc")
        request = QNetworkRequest(url)
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

    def _stage_in_background(self, work, source: str) -> None:
        relay = _Relay()
        self._relays.add(relay)

        def finished(result) -> None:
            self._relays.discard(relay)
            if isinstance(result, Exception):
                self.message.emit(str(result) or "The extension couldn't be installed.", "error")
                return
            result["source"] = source
            self._on_staged(result)

        relay.done.connect(finished)

        def run() -> None:
            try:
                result = work()
            except InstallError as exc:
                result = exc
            except Exception as exc:  # unexpected file problems are reported, never fatal
                result = InstallError(f"The extension couldn't be installed: {exc}")
            relay.done.emit(result)

        threading.Thread(target=run, daemon=True).start()

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
                    if len(inner) != 1:
                        raise InstallError("The package doesn't contain a manifest.json file.")
                    nested = inner[0]
                    holding = target.with_name(target.name + "-inner")
                    nested.rename(holding)
                    shutil.rmtree(target)
                    holding.rename(target)
            shutil.rmtree(target / "_metadata", ignore_errors=True)  # Chrome Web Store signatures (unpacked loads refuse "_" names)
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
            ext_id = extension_id_from_key(key) if key else None
            if key and "key" not in manifest:
                # Keeping the developer key keeps the official extension ID (some extensions rely on it).
                manifest["key"] = base64.b64encode(key).decode("ascii")
                (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            elif isinstance(manifest.get("key"), str):
                try:
                    ext_id = extension_id_from_key(base64.b64decode(manifest["key"]))
                except ValueError:
                    ext_id = None
            if expected_id and ext_id and ext_id != expected_id:
                raise InstallError("The downloaded file doesn't match the requested extension.")
            source_path = str(path.resolve()) if path is not None else None
            return {"dir": str(target), "name": name, "id": ext_id, "source_path": source_path}
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def _on_staged(self, job: dict) -> None:
        ext_id = job.get("id")
        source_path = job.get("source_path")
        busy_paths = {j.get("source_path") for j in [*self._jobs.values(), *self._updates.values()]} - {None}
        if (ext_id and (ext_id in self._installing or ext_id in self._updates)) or (source_path and source_path in busy_paths):
            shutil.rmtree(job["dir"], ignore_errors=True)
            self.message.emit(f"“{job.get('name')}” is already being installed.", "info")
            return
        if not ext_id and source_path:  # re-installing an unpacked folder (or plain .zip) updates the earlier copy
            ext_id = next((eid for eid, state in self.registry.items()
                           if state.get("source_path") == source_path and self._info(eid) is not None), None)
        existing = self._info(ext_id) if ext_id else None
        if existing is not None:
            self._start_update(existing, job)
            return
        self._install_staged(job)

    # Updates replace the files inside the extension's existing folder, so its ID - and with it the
    # extension's saved data - stays the same. The previous version is kept until the new one loads.
    RESTORE_MARKER = ".foxglove-restore-to"

    def _start_update(self, info, job: dict) -> None:
        job.update(ext_id=info.id(), target=info.path(), name=info.name() or job.get("name"), backup=None)
        self._updates[info.id()] = job
        self.manager.unloadExtension(info)  # continues in _on_unload_finished

    def _on_unload_finished(self, info) -> None:
        job = self._updates.get(info.id())
        if job is not None and "unloaded" not in job:
            job["unloaded"] = True
            QTimer.singleShot(0, lambda: self._swap_in_update(job))

    def _swap_in_update(self, job: dict) -> None:
        target, staged = Path(job["target"]), Path(job["dir"])
        backup = self.staging / f"previous-{uuid.uuid4().hex[:10]}"
        try:
            target.rename(backup)
        except OSError as exc:
            job["error"] = str(exc)
            self._load_update(job, rollback=True)  # nothing was moved: just load the old version again
            return
        job["backup"] = str(backup)
        try:
            (backup / self.RESTORE_MARKER).write_text(str(target), encoding="utf-8")
            staged.rename(target)
        except OSError as exc:
            job["error"] = str(exc)
            self._restore_backup(job)
            self._load_update(job, rollback=True)
            return
        self._load_update(job, rollback=False)

    def _load_update(self, job: dict, rollback: bool) -> None:
        job["rollback"] = rollback
        self._loading[os.path.realpath(job["target"])] = job
        self.manager.loadExtension(job["target"])

    def _restore_backup(self, job: dict) -> None:
        backup, target = job.get("backup"), Path(job["target"])
        if not backup or not Path(backup).exists():
            return
        shutil.rmtree(target, ignore_errors=True)
        try:
            (Path(backup) / self.RESTORE_MARKER).unlink(missing_ok=True)
            Path(backup).rename(target)
            job["backup"] = None
        except OSError as exc:
            log(f"Couldn't restore the previous version of an extension: {exc}")

    def _finish_update(self, job: dict, info) -> None:
        ext_id = job["ext_id"]
        loaded = info.isLoaded() and not info.error() and info.id() == ext_id
        if not loaded and not job["rollback"]:
            if info.isLoaded():  # something with a different ID loaded from this folder: take it out again
                self.manager.unloadExtension(info)
            job["error"] = info.error() or "the new version couldn't be loaded"
            self._restore_backup(job)
            self._load_update(job, rollback=True)
            return
        self._updates.pop(ext_id, None)
        shutil.rmtree(job["dir"], ignore_errors=True)
        if job.get("backup"):
            shutil.rmtree(job["backup"], ignore_errors=True)
        if not job["rollback"]:
            state = self.registry.setdefault(ext_id, {})
            state.update(source=job.get("source", state.get("source", "file")),
                         source_path=job.get("source_path") or state.get("source_path"), installed=time.time())
            self.save()
        for delay in (0, 500):  # Qt loads extensions disabled; switch it back on if the user had it on
            QTimer.singleShot(delay, lambda: self._apply_enabled(ext_id))
        if job["rollback"]:
            self.message.emit(f"Couldn't update “{job['name']}”: {job.get('error') or 'unknown error'}. "
                              "The previous version was kept.", "error")
        else:
            self.message.emit(f"“{info.name() or job['name']}” was updated.", "success")
        self.changed.emit()

    def _recover_interrupted_updates(self) -> list[str]:
        """Put back any previous version an interrupted update had moved aside."""
        restored = []
        for marker in self.staging.glob(f"previous-*/{self.RESTORE_MARKER}"):
            try:
                target = Path(marker.read_text(encoding="utf-8").strip())
                marker.unlink()
                if target.parent.name == "Extensions" and not target.exists():
                    marker.parent.rename(target)
                    restored.append(str(target))
            except OSError:
                pass
        return restored

    def _install_staged(self, job: dict) -> None:
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
        state = self.registry.setdefault(info.id(), {})
        state.update(enabled=True, source=job.get("source", "file"), source_path=job.get("source_path"),
                     installed=time.time())
        state.setdefault("pinned", True)
        self.save()
        ext_id = info.id()
        QTimer.singleShot(0, lambda: self._apply_enabled(ext_id))
        self.message.emit(f"“{info.name()}” was added to {APP_NAME}.", "success")
        self.changed.emit()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Internal pages (foxglove://newtab)
# ══════════════════════════════════════════════════════════════════════════════════════════
class InternalPages(QWebEngineUrlSchemeHandler):
    def __init__(self, render_newtab, parent: QObject | None = None):
        super().__init__(parent)
        self.render_newtab = render_newtab

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:
        if job.requestUrl().host() != "newtab":
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        try:
            body = self.render_newtab().encode("utf-8")
        except Exception as exc:  # never leave the tab hanging
            body = f"<!doctype html><title>New Tab</title><pre>{html.escape(str(exc))}</pre>".encode()
        buffer = QBuffer(job)  # owned by the job, so it lives exactly as long as the request
        buffer.setData(body)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(b"text/html", buffer)


NEWTAB_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>New Tab</title><meta name="color-scheme" content="dark">
<style>
:root { --bg:#2b2a33; --fg:#fbfbfe; --accent:%(accent)s; --field:#1c1b22; }
html, body { margin:0; min-height:100%%; background:var(--bg); color:var(--fg);
  font: 15px -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
main { max-width: 680px; margin: 0 auto; padding: 24vh 24px 48px; display:flex; flex-direction:column; align-items:center; }
.brand { display:flex; align-items:center; gap:16px; margin-bottom:34px; user-select:none; }
.brand svg { width:72px; height:72px; filter: drop-shadow(0 4px 10px rgba(0,0,0,.35)); }
.brand span { font-size:38px; font-weight:600; letter-spacing:.3px; }
form { width:100%%; position:relative; margin:0; }
form svg { position:absolute; left:16px; top:16px; width:20px; height:20px; opacity:.75; }
input { width:100%%; box-sizing:border-box; height:52px; border-radius:8px; border:2px solid transparent;
  background:var(--field); color:var(--fg); font-size:16px; padding:0 16px 0 48px; outline:none;
  box-shadow:0 2px 10px rgba(0,0,0,.35); }
input:focus { border-color:var(--accent); }
input::placeholder { color:#8f8f9d; }
</style></head>
<body><main>
<div class="brand">%(logo)s<span>%(app)s</span></div>
<form id="search"><svg viewBox="0 0 24 24" fill="none" stroke="#fbfbfe" stroke-width="2" stroke-linecap="round">
<circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4.5-4.5"/></svg>
<input id="q" placeholder="Search with %(engine)s or enter address" autocomplete="off"></form>
</main>
<script>
const template = %(template)s;
document.getElementById('search').addEventListener('submit', (e) => {
  e.preventDefault();
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const looksLikeUrl = /^[a-z][a-z0-9+.-]*:\\/\\//i.test(q) || (/^[^\\s/]+\\.[a-z]{2,}(:\\d+)?(\\/\\S*)?$/i.test(q));
  location.href = looksLikeUrl ? (/^[a-z][a-z0-9+.-]*:\\/\\//i.test(q) ? q : 'https://' + q)
                               : template.replace('{}', encodeURIComponent(q).replace(/%%20/g, '+'));
});
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
        pass

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

    def __init__(self, parent: QWidget, translucent: bool = True):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("Panel")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if TRANSLUCENT_POPUPS and translucent:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        else:
            self.setProperty("square", True)
        self._anchor: QWidget | None = None

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
        self.activateWindow()

    def reposition(self) -> None:
        anchor = self._anchor
        if anchor is None or sip.isdeleted(anchor):
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
        self.hide()

    def show_message(self, text: str, kind: str = "success", timeout: int = 4500) -> None:
        """kind: "success", "info" or "error"."""
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
        self.hide()


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
    """Opens from the padlock: connection security and the permissions granted to the site."""

    def __init__(self, win: "BrowserWindow", url: QUrl):
        super().__init__(win)
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
        permissions = []
        if url.scheme() in ("http", "https"):
            origin = QUrl(f"{url.scheme()}://{url.authority()}")
            permissions = [p for p in win.profile.listPermissionsForOrigin(origin)
                           if p.state() != QWebEnginePermission.State.Ask and p.permissionType() in PERMISSION_TEXT]
        if permissions:
            line = QFrame()
            line.setObjectName("PanelSeparator")
            line.setFixedHeight(1)
            layout.addWidget(line)
            layout.addWidget(tone_label("Permissions", "heading"))
            for permission in permissions:
                row = QHBoxLayout()
                allowed = permission.state() == QWebEnginePermission.State.Granted
                text = PERMISSION_TEXT[permission.permissionType()]
                row.addWidget(tone_label(f"{'Allowed' if allowed else 'Blocked'} to {text}", "secondary"), 1)
                clear = tool_button(icon("close", P.TEXT_2), "Forget this decision", 26)
                clear.clicked.connect(lambda *_, p=permission, r=row: self._reset(p, r))
                row.addWidget(clear)
                layout.addLayout(row)
        self.setMinimumWidth(340)

    def _reset(self, permission, row: QHBoxLayout) -> None:
        permission.reset()
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
POPUP_MEASURE_JS = """(() => {
  const b = document.body, d = document.documentElement;
  if (!b) return [0, 0];
  const cs = getComputedStyle(b), r = b.getBoundingClientRect();
  const mx = parseFloat(cs.marginLeft) + parseFloat(cs.marginRight);
  const my = parseFloat(cs.marginTop) + parseFloat(cs.marginBottom);
  const h = Math.max(r.height + my, b.scrollHeight + my, d.scrollHeight > innerHeight ? d.scrollHeight : 0);
  return [Math.ceil(r.width + mx), Math.ceil(h)];
})()"""


class ExtensionPopup(Panel):
    """Shows an extension's toolbar pop-up page, sized to its content like Chrome does."""

    def __init__(self, win: "BrowserWindow", url: QUrl):
        super().__init__(win, translucent=False)
        self.win = win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        self.view = QWebEngineView(self)
        self.page = WebPage(win.profile, self.view)
        self.view.setPage(self.page)
        self.view.setFixedSize(360, 160)
        layout.addWidget(self.view)
        self.page.windowCloseRequested.connect(self.close)
        self.page.newWindowRequested.connect(self._open_elsewhere)
        self.page.loadFinished.connect(lambda *_: self._measure_soon())
        self.page.load(url)

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
        if width < 20 or height < 20:
            return
        size = QSize(clamp(width, 120, 800), clamp(height, 40, 600))
        if size != self.view.size():
            self.view.setFixedSize(size)
            self.adjustSize()
            self.reposition()

    def _open_elsewhere(self, request) -> None:
        self.win.handle_new_window(request, None)
        self.close()


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

    def _on_url(self, url: QUrl) -> None:
        secure = url.scheme() == "https"
        prefix = "🔒 " if secure else ""
        self.header.setText(prefix + elide(url.toDisplayString(), 90))

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
        self.cookies = QCheckBox("Cookies (you'll be signed out of websites)")
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
        profile = self.win.profile
        if self.history.isChecked():
            self.win.clear_history_traces()
        if self.cookies.isChecked():
            profile.cookieStore().deleteAllCookies()
        if self.cache.isChecked():
            profile.clearHttpCache()
        self.accept()
        self.win.toast("Browsing data cleared.")


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
        layout.addLayout(controls)
        buttons = QVBoxLayout()
        buttons.setSpacing(6)
        if not entry.options_url.isEmpty():
            options = make_button("Options")
            options.clicked.connect(lambda *_: win.open_url(entry.options_url, "tab"))
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
            "that appears. Only Manifest V3 extensions are supported. Qt WebEngine provides a subset of Chrome's "
            "extension APIs, so extensions built on content scripts, pop-ups and storage work best.",
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
        clear = make_button("Clear Browsing Data…")
        clear.clicked.connect(lambda *_: run_dialog(ClearDataDialog(win)))
        privacy_row.addWidget(clear)
        privacy_row.addStretch(1)
        layout.addLayout(privacy_row)
        layout.addWidget(tone_label("Cookies are kept when you quit (including session cookies), so websites keep "
                                    "you signed in.", "dim", wrap=True))
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
    """Choose how Foxglove connects: directly, through Tor, Cloudflare WARP or your own proxy server."""

    def __init__(self, win: "BrowserWindow"):
        super().__init__(win)
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
        layout.addWidget(tone_label("Send Foxglove's traffic through:", "secondary"))
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
        layout.addWidget(tone_label("Only Foxglove's own traffic uses the VPN. Applying restarts Foxglove; your tabs "
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
        self.hint = QLabel("For quick access, place your bookmarks here: click ☆ in the address bar "
                           f"or press {shortcut_text('Ctrl+D')}.")
        self.hint.setObjectName("BookmarksHint")
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
            if widget is not None and widget not in (self.hint, self.overflow):
                widget.deleteLater()
        self.buttons = [BookmarkButton(self.win, node) for node in self.win.bookmarks.children("toolbar")]
        for button in self.buttons:
            self.layout_.addWidget(button)
        self.layout_.addWidget(self.hint)
        self.layout_.addStretch(1)
        self.layout_.addWidget(self.overflow)
        self.hint.setVisible(not self.buttons)
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
        self.bookmarks_bar.setVisible(settings.get("show_bookmarks_bar"))

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
        extensions.changed.connect(self._rebuild_extension_buttons)
        extensions.message.connect(lambda text, kind: self.toast(text, kind))
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
        self.vpn_button.clicked.connect(lambda *_: self.show_vpn_panel())
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
        self.act_bookmarks_bar = a("Bookmarks Toolbar", lambda: self.set_bookmarks_bar_visible(not self.bookmarks_bar.isVisible()),
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
                     self.act_clear_data, self.act_settings):
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
        return QUrl(NEWTAB)

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
        if not is_newtab(QUrl(entry.get("url", ""))) or entry.get("history"):
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
        page.renderProcessTerminated.connect(lambda status, _code, t=tab: self._on_crashed(t, status))
        page.findTextFinished.connect(lambda result, t=tab: self._on_find_result(t, result))
        page.certificateAccepted.connect(self._on_certificate_accepted)
        page.certificateProblem.connect(lambda error, t=tab: self._on_certificate_problem(t, error))
        tab.view.printFinished.connect(lambda ok: self._on_print_finished(ok))

    def _on_current_changed(self, index: int) -> None:
        tab = self.tab_at(index)
        if tab is not None:
            self._activate(tab)

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
    def _on_title_changed(self, tab: Tab, title: str) -> None:
        self._refresh_tab(tab)
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
        if tab is self.current_tab():
            self._sync_chrome(tab)
        self._apply_site_zoom(tab)
        self._update_webstore_bar(tab, url)
        for bar in list(tab.permission_bars):
            if sip.isdeleted(bar) or bar.property("origin") != f"{url.scheme()}://{url.authority()}":
                tab.permission_bars.remove(bar)
                if not sip.isdeleted(bar):
                    bar.dismiss()
        if not tab.loading and HistoryStore.recordable(url) and url.toString() != tab.last_recorded:
            tab.last_recorded = url.toString()
            self.history.add_visit(url.toString(), tab.page.title())
        self._refresh_tab(tab)
        self.schedule_session_save()

    def _on_load_started(self, tab: Tab) -> None:
        tab.loading, tab.progress, tab.crashed = True, 0, False
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
        origin = permission.origin()
        bar = InfoBar(icon("info", P.ACCENT), f"Allow <b>{html.escape(origin.host() or origin.toString())}</b> to {text}?")
        bar.setProperty("origin", f"{origin.scheme()}://{origin.authority()}")
        decided = {"done": False}

        def decide(allow: bool) -> None:
            if not decided["done"]:
                decided["done"] = True
                permission.grant() if allow else permission.deny()

        bar.add_button("Block", lambda: (decide(False), bar.dismiss()))
        bar.add_button("Allow", lambda: (decide(True), bar.dismiss()), primary=True)
        bar.on_dismiss = lambda: decide(False)
        tab.permission_bars.append(bar)
        tab.add_bar(bar)

    def ask_permission_modal(self, parent: QWidget, permission: QWebEnginePermission) -> None:
        text = PERMISSION_TEXT.get(permission.permissionType())
        if text is None:
            permission.deny()
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
        self.bookmarks_bar.setVisible(self.settings.get("show_bookmarks_bar"))
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
        if self._fullscreen_tab is None:
            self.bookmarks_bar.setVisible(bool(visible))
        self.act_bookmarks_bar.setChecked(bool(visible))

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
        default = str(Path.home() / f"{APP_NAME.lower()}-bookmarks-{time.strftime('%Y-%m-%d')}.html")
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
            button = tool_button(entry.icon, entry.name)
            button.clicked.connect(lambda *_, e=entry.id, b=button: self.open_extension(e, b))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(lambda pos, e=entry.id, n=entry.name, b=button: self._extension_button_menu(e, n, b.mapToGlobal(pos)))
            self.extension_buttons.addWidget(button)
        tab = self.current_tab()
        if tab is not None:
            self._update_webstore_bar(tab, tab.url())

    def _extension_button_menu(self, ext_id: str, name: str, pos: QPoint) -> None:
        menu = Menu("", self)
        entry = self.extensions.entry(ext_id)
        if entry is not None and not entry.options_url.isEmpty():
            menu.addAction("Options", lambda: self.open_url(entry.options_url, "tab"))
        menu.addAction("Unpin from Toolbar", lambda: self.extensions.set_pinned(ext_id, False))
        menu.addAction("Manage Extensions", self.show_extensions)
        menu.addSeparator()
        menu.addAction("Remove Extension…", lambda: self.confirm_remove_extension(ext_id, name))
        menu.exec(pos)
        menu.deleteLater()

    def open_extension(self, ext_id: str, anchor: QWidget | None = None) -> None:
        entry = self.extensions.entry(ext_id)
        if entry is None:
            return
        if not entry.popup_url.isEmpty():
            popup = ExtensionPopup(self, entry.popup_url)
            popup.popup_at(anchor if anchor is not None and anchor.isVisible() else self.extensions_button)
        elif not entry.options_url.isEmpty():
            self.open_url(entry.options_url, "tab")
        else:
            self.toast(f"“{entry.name}” has no pop-up — it works on web pages automatically.", "info")

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
        answer = QMessageBox.question(self, "Remove Extension", f"Remove “{name}” from {APP_NAME}?")
        if answer == QMessageBox.StandardButton.Yes:
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

    def show_vpn_panel(self) -> None:
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

    def show_about(self) -> None:
        QMessageBox.about(self, f"About {APP_NAME}",
                          f"<h3>{APP_NAME} {APP_VERSION}</h3><p>A Firefox-inspired browser written in Python.</p>"
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
        self.content.toast.show_message(text, kind)

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
def render_newtab_page(settings: Settings) -> str:
    engine = settings.get("search_engine")
    return NEWTAB_HTML % {
        "accent": P.ACCENT,
        "logo": LOGO_SVG.replace('<svg ', '<svg aria-hidden="true" ', 1),
        "app": APP_NAME,
        "engine": html.escape(engine),
        "template": json.dumps(settings.search_template()),
    }


def set_macos_app_name(name: str) -> None:
    """Show "Foxglove" instead of "Python" in the macOS menu bar (needs to run before QApplication)."""
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="foxglove.py", description=f"{APP_NAME} web browser")
    parser.add_argument("urls", nargs="*", help="pages to open")
    parser.add_argument("--profile", default="default", help="profile name (separate tabs, bookmarks and cookies)")
    options, _unknown = parser.parse_known_args(argv[1:])
    profile_name = re.sub(r"[^A-Za-z0-9_.-]", "_", options.profile) or "default"

    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName("")
    QCoreApplication.setApplicationVersion(APP_VERSION)
    data_root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
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
    if not any(f.startswith("--log-level") for f in flags):
        flags.append("--log-level=3")  # keep Chromium's internal chatter out of the terminal
    if vpn is not None and not any(f.startswith("--force-webrtc-ip-handling-policy") for f in flags):
        # Without this, video-call code (WebRTC) reveals your real IP address even through a proxy.
        flags.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)

    scheme = QWebEngineUrlScheme(b"foxglove")
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(QWebEngineUrlScheme.Flag.SecureScheme | QWebEngineUrlScheme.Flag.LocalScheme
                    | QWebEngineUrlScheme.Flag.LocalAccessAllowed)
    QWebEngineUrlScheme.registerScheme(scheme)

    install_error_guard()
    set_macos_app_name(APP_NAME)
    app = QApplication(argv)
    app.setApplicationDisplayName(APP_NAME)
    app.setStyle("Fusion")
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)  # dark native title bars
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
    apply_dark_palette(app)
    app.setStyleSheet(build_stylesheet())
    app.setWindowIcon(icons().logo())
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
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    profile.setPersistentPermissionsPolicy(QWebEngineProfile.PersistentPermissionsPolicy.StoreOnDisk)
    user_agent = re.sub(r"\s*QtWebEngine/\S+", "", profile.httpUserAgent())
    profile.setHttpUserAgent(user_agent)  # look like regular Chrome so sites don't serve a degraded version

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

    pages = InternalPages(lambda: render_newtab_page(settings), app)
    profile.installUrlSchemeHandler(b"foxglove", pages)
    extensions = ExtensionsController(profile, profile_dir / "extensions.json", profile_dir / "extension-staging", user_agent)

    restart_request = {"requested": False}
    window = BrowserWindow(profile, settings, bookmarks, history, favicons, extensions,
                           profile_dir / "session.json", options.urls, vpn, restart_request)
    window.show()

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

    code = app.exec()

    # Tear down in a safe order: every web page before the profile, so Chromium shuts down cleanly and
    # flushes cookies to disk.
    heartbeat.stop()
    menubar = getattr(window, "mac_menubar", None)
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
    lock.unlock()
    if restart_request["requested"]:
        restart_process(profile_name, original_flags)
    return code


def restart_process(profile_name: str, original_flags: str | None) -> None:
    """Start Foxglove again in this same process (same terminal, same Ctrl+C), e.g. to apply a VPN change."""
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

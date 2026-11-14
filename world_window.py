"""World Window: a small, collapsible Windows RSS reader. No API keys required."""
import concurrent.futures
import ctypes
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse
from urllib.request import getproxies
import webbrowser

import requests
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QPoint, QStandardPaths, QPointF, QRectF
from PySide6.QtGui import QColor, QCursor, QFont, QIcon, QPainter, QPen, QPixmap, QAction, QPolygonF, QPainterPath
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QScrollArea, QFrame, QSystemTrayIcon, QMenu)

TZ = timezone(timedelta(hours=8))
SOURCES = [
    ('联合国新闻', 'https://news.un.org/feed/subscribe/zh/news/all/rss.xml'),
    ('BBC World', 'https://feeds.bbci.co.uk/news/world/rss.xml'),
    ('The Guardian', 'https://www.theguardian.com/world/rss'),
    ('Al Jazeera', 'https://www.aljazeera.com/xml/rss/all.xml'),
    ('中国新闻网·国际', 'https://www.chinanews.com.cn/rss/world.xml'),
]
APPDIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'WorldWindow'
APPDIR.mkdir(parents=True, exist_ok=True)
SETTINGS = APPDIR / 'settings.json'
CACHE = APPDIR / 'news.json'
TRANSLATIONS = APPDIR / 'translations.json'


def now():
    return datetime.now(TZ)


def foreground_window():
    """Read foreground identity without moving focus or activating windows."""
    if os.name != 'nt':
        return 0, '', os.getpid()
    user = ctypes.windll.user32
    user.GetForegroundWindow.restype = ctypes.c_void_p
    user.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    handle = user.GetForegroundWindow()
    name = ctypes.create_unicode_buffer(128)
    process = ctypes.c_uint32()
    user.GetClassNameW(handle, name, len(name))
    user.GetWindowThreadProcessId(handle, ctypes.byref(process))
    return handle, name.value, process.value


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return default


def write_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def clean(text):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', text or ''))).strip()


def safe_url(url):
    return isinstance(url, str) and urlparse(url).scheme in ('https', 'http')


def parse_feed(content, source):
    tree = ET.fromstring(content)
    result = []
    for item in tree.findall('.//item')[:15]:
        title = clean(item.findtext('title'))
        link = (item.findtext('link') or '').strip()
        if not title or not safe_url(link):
            continue
        image = ''
        for el in item.iter():
            tag = el.tag.split('}')[-1]
            if tag in ('thumbnail', 'content', 'enclosure'):
                url = el.get('url', '')
                if safe_url(url) and (tag != 'enclosure' or el.get('type', '').startswith('image/')):
                    image = url
                    break
        description = item.findtext('description') or ''
        if not image:
            match = re.search(r'<img[^>]+src=[\"\']([^\"\']+)', description)
            if match and safe_url(match[1]):
                image = html.unescape(match[1])
        result.append(dict(title=title, link=link, summary=clean(description)[:360],
            source=source, published=clean(item.findtext('pubDate')), image=image))
    return result


def download(url, limit=5_000_000):
    # Honor the user's network settings first; retry direct if the proxy fails.
    error = None
    for trust in (True, False):
        try:
            with requests.Session() as session:
                session.trust_env = False
                if trust:
                    proxies = getproxies()
                    # Windows' shared HTTP CONNECT proxy is not a TLS proxy.
                    if os.name == 'nt' and not any(os.environ.get(k) for k in ('HTTPS_PROXY', 'https_proxy')):
                        p = proxies.get('https', '')
                        if p.startswith('https://'):
                            proxies['https'] = 'http://' + p[8:]
                    session.proxies.update({k:v for k,v in proxies.items() if k in ('http', 'https', 'all')})
                with session.get(url, timeout=(4, 6), headers={'User-Agent': 'WorldWindow/0.1 RSS Reader'}, stream=True) as r:
                    r.raise_for_status()
                    pieces, size = [], 0
                    for part in r.iter_content(65536):
                        size += len(part)
                        if size > limit:
                            raise ValueError('响应过大')
                        pieces.append(part)
                    return b''.join(pieces)
        except Exception as exc:
            error = exc
    raise RuntimeError(str(error))


def balanced(groups, limit=12):
    """Round robin by publisher. This is source variety, not ideological scoring."""
    out, seen = [], set()
    for index in range(max([len(g) for g in groups] or [0])):
        for group in groups:
            if index >= len(group):
                continue
            item = group[index]
            key = item['link'].split('#')[0]
            if key not in seen:
                seen.add(key)
                out.append(item)
            if len(out) >= limit:
                return out
    return out


def translation_key(item):
    return hashlib.sha256((item['title'] + '\n' + item.get('summary', '')).encode()).hexdigest()


def is_chinese(text):
    han = len(re.findall(r'[\u3400-\u9fff]', text))
    latin = len(re.findall(r'[A-Za-z]', text))
    return han > 0 and han >= latin / 2


def translation_chunks(text, max_bytes=480):
    chunks, chunk = [], ''
    for word in re.findall(r'\S+\s*', text):
        for char in word:
            if len((chunk + char).encode('utf-8')) > max_bytes:
                chunks.append(chunk.strip())
                chunk = ''
            chunk += char
    if chunk.strip():
        chunks.append(chunk.strip())
    return chunks


def translate_text(text):
    if not text or is_chinese(text):
        return text
    parts = []
    for segment in translation_chunks(text):
        url = 'https://api.mymemory.translated.net/get?' + urlencode({'q':segment, 'langpair':'en|zh-CN'})
        data = json.loads(download(url, 150000))
        if str(data.get('responseStatus')) != '200' or data.get('quotaFinished'):
            if data.get('quotaFinished') or str(data.get('responseStatus')) == '429':
                raise RuntimeError('免费翻译额度暂时用完，请稍后重试。')
            raise RuntimeError('翻译服务暂不可用，请稍后重试。')
        translated = html.unescape(data.get('responseData', {}).get('translatedText', '')).strip()
        if not translated:
            raise RuntimeError('没有取得译文，请重试。')
        parts.append(translated)
    return ' '.join(parts)


class Translator(QThread):
    translated = Signal(str, object)

    def __init__(self, key, item, parent=None):
        super().__init__(parent)
        self.key, self.item = key, dict(item)

    def run(self):
        try:
            value = {'title':translate_text(self.item['title']),
                     'summary':translate_text(self.item.get('summary', ''))}
        except Exception as exc:
            value = {'error':str(exc) if isinstance(exc, RuntimeError) and len(str(exc)) < 100 else '网络或翻译服务不可用，原文已保留。请重试。'}
        self.translated.emit(self.key, value)


def due(last, current=None):
    current = current or now()
    target = current.replace(hour=8, minute=30, second=0, microsecond=0)
    if current < target:
        target -= timedelta(days=1)
    try:
        return datetime.fromisoformat(last) < target
    except (ValueError, TypeError):
        return True


class Fetcher(QThread):
    finished_news = Signal(object)

    def run(self):
        groups, failures = [None] * len(SOURCES), []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(lambda n=n, u=u: parse_feed(download(u, 3_000_000), n)): i
                       for i, (n, u) in enumerate(SOURCES)}
            for future in concurrent.futures.as_completed(futures):
                i = futures[future]
                try:
                    groups[i] = future.result()
                    if not groups[i]:
                        failures.append(SOURCES[i][0])
                except Exception:
                    failures.append(SOURCES[i][0])
        items = balanced([g for g in groups if g])
        def fetch_image(item):
            if not item['image']:
                return
            name = hashlib.sha256(item['image'].encode()).hexdigest()[:24] + '.jpg'
            path = APPDIR / name
            try:
                if not path.exists():
                    path.write_bytes(download(item['image'], 3_000_000))
                item['local_image'] = str(path)
            except Exception:
                item['local_image'] = ''
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(fetch_image, items))
        self.finished_news.emit(dict(items=items, updated=now().isoformat(), failures=failures))


STYLE = """
QWidget {color:#edf4f4; font-family:'Microsoft YaHei UI'; font-size:13px;}
QWidget#panel {background:rgba(20,32,43,240); border:1px solid #465763; border-radius:14px;}
QLabel {background:transparent; border:0;}
QLabel#title {font-size:20px; font-weight:600;}
QLabel#meta {color:#b7c9cf; font-size:11px;}
QLabel#headline {font-size:17px; font-weight:600;}
QPushButton {background:transparent; border:1px solid #50616d; border-radius:6px; padding:6px 10px;}
QPushButton:hover {background:#314c53;}
QPushButton:focus {border:1px solid #bde5d8;}
QPushButton:disabled {color:#91a3ac;}
QPushButton:checked {background:#bde5d8; color:#142c28;}
QScrollArea {background:transparent; border:0;}
QScrollBar:vertical {background:#182b37; width:7px;}
QScrollBar::handle:vertical {background:#60777e; min-height:24px; border-radius:3px;}
QFrame#story {border:0; border-bottom:1px solid #40515b; background:transparent;}
QMenu {background:#20333f; color:#edf4f4; border:1px solid #60777e; padding:5px;}
QMenu::item {padding:8px 18px;}
QMenu::item:selected {background:#38535c;}
"""


def label(text, name='', wrap=False):
    w = QLabel(text)
    w.setTextFormat(Qt.TextFormat.PlainText)
    w.setObjectName(name)
    w.setWordWrap(wrap)
    w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return w


def make_icon():
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor('#203947'))
    p.setPen(QPen(QColor('#bde5d8'), 2))
    p.drawRoundedRect(4, 4, 56, 56, 16, 16)
    p.setPen(QPen(QColor('#d6f0e7'), 2))
    p.drawRoundedRect(17, 17, 30, 30, 3, 3)
    p.drawLine(17, 25, 47, 25)
    p.drawLine(32, 25, 32, 47)
    p.end()
    return QIcon(pix)


class Badge(QWidget):
    def __init__(self, owner):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnBottomHint)
        self.owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(156, 184)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAccessibleName('世界一隅小报刊架，拖动架子可移动，悬停展开新闻，点击固定')
        self.setToolTip('世界一隅 · 小报刊架\n拖住架子移动 · 悬停阅读 · 点击固定 · 右键设置')
        self.cover = QPixmap()
        self.cover_title = '每天，看看世界'
        self.press = None
        self.dragged = False

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 35))
        p.drawEllipse(QRectF(12, 169, 136, 10))
        # A freestanding A-frame news rack. Only the news thumbnail is a photo.
        p.setBrush(QColor('#203c39'))
        p.drawPolygon(QPolygonF([QPointF(112, 30), QPointF(128, 30), QPointF(149, 171), QPointF(132, 171)]))
        p.setBrush(QColor('#284c46'))
        p.drawRoundedRect(QRectF(18, 27, 120, 140), 5, 5)
        p.setBrush(QColor('#477063'))
        p.drawRoundedRect(QRectF(18, 31, 7, 142), 2, 2)
        p.drawRoundedRect(QRectF(130, 31, 7, 142), 2, 2)
        p.setBrush(QColor('#17342f'))
        p.drawRoundedRect(QRectF(11, 166, 25, 7), 2, 2)
        p.drawRoundedRect(QRectF(124, 166, 25, 7), 2, 2)
        p.setBrush(QColor('#f2ecd9'))
        p.drawRoundedRect(QRectF(29, 51, 98, 103), 2, 2)
        p.setPen(QColor('#234d41'))
        p.setFont(QFont('Microsoft YaHei UI', 8, QFont.Weight.DemiBold))
        p.drawText(QRectF(33, 53, 90, 16), Qt.AlignmentFlag.AlignCenter, '今日世界')
        rect = QRectF(34, 74, 88, 48)
        if not self.cover.isNull():
            pix = self.cover.scaled(176, 96, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            pix = pix.copy((pix.width()-176)//2, (pix.height()-96)//2, 176, 96)
            p.drawPixmap(rect, pix, QRectF(pix.rect()))
        else:
            p.fillRect(rect, QColor('#d4dfd0'))
            p.setPen(QColor('#375347'))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, '等待新闻更新')
        p.setPen(QColor('#263c36'))
        f = QFont('Microsoft YaHei UI', 7)
        p.setFont(f)
        text = p.fontMetrics().elidedText(self.cover_title, Qt.TextElideMode.ElideRight, 88)
        p.drawText(QRectF(34, 125, 88, 15), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        p.setPen(QPen(QColor('#8c9c8b'), 1))
        p.drawLine(34, 144, 118, 144)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor('#315b4f'))
        p.drawPolygon(QPolygonF([QPointF(10, 18), QPointF(142, 18), QPointF(150, 39), QPointF(3, 39)]))
        p.setBrush(QColor('#203f37'))
        p.drawRoundedRect(QRectF(3, 36, 147, 8), 2, 2)
        p.setPen(QColor('#f3eedb'))
        p.setFont(QFont('Microsoft YaHei UI', 10, QFont.Weight.DemiBold))
        p.drawText(QRectF(15, 17, 124, 24), Qt.AlignmentFlag.AlignCenter, '世界一隅')
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor('#b88e59'))
        p.drawRoundedRect(QRectF(24, 151, 107, 7), 2, 2)
        p.end()

    def refresh_visual(self):
        self.cover = QPixmap()
        self.cover_title = '每天，看看世界'
        for item in self.owner.items:
            pix = QPixmap(item.get('local_image', ''))
            if not pix.isNull():
                self.cover = pix
                self.cover_title = item['title']
                break
        self.update()

    def enterEvent(self, event):
        self.owner.outside_since = None
        self.owner.hover.start(350)

    def leaveEvent(self, event):
        self.owner.hover.stop()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.owner.menu.popup(event.globalPosition().toPoint())
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.press = event.globalPosition().toPoint()
            self.origin = self.pos()
            self.dragged = False
            self.owner.hover.stop()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.press is not None:
            delta = event.globalPosition().toPoint() - self.press
            if delta.manhattanLength() > QApplication.startDragDistance():
                self.dragged = True
            if self.dragged:
                self.move(self.origin + delta)
                self.owner.place_panel()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.press is None:
            return
        if not self.dragged:
            self.owner.toggle_pin()
        else:
            self.owner.clamp_badge()
            self.owner.settings['position'] = [self.x(), self.y()]
            self.owner.save_settings()
        self.press = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)


class Window(QWidget):
    def __init__(self, offline=False):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle('世界一隅')
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAccessibleName('世界一隅新闻阅读面板')
        self.settings = read_json(SETTINGS, {})
        self.news = read_json(CACHE, {})
        self.items = self.news.get('items', [])
        self.icon = make_icon()
        self.setWindowIcon(self.icon)
        self.pinned = False
        self.outside_since = None
        self.foreground_when_expanded = None
        self.last_attempt = float('-inf')
        self.worker = None
        self.translations = read_json(TRANSLATIONS, {})
        self.translation_workers = {}
        self.translation_widgets = {}
        self.translated_visible = set()
        self.exiting = False
        self.offline = offline
        self.resize(448, 680)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QWidget()
        panel.setObjectName('panel')
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label('世界一隅', 'title'))
        top.addStretch()
        self.pin = QPushButton('固定')
        self.pin.setCheckable(True)
        self.pin.clicked.connect(self.toggle_pin)
        top.addWidget(self.pin)
        close = QPushButton('收起')
        close.clicked.connect(self.collapse)
        top.addWidget(close)
        layout.addLayout(top)
        self.status = label('', 'meta', True)
        layout.addWidget(self.status)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        layout.addWidget(self.scroll)
        bar = QHBoxLayout()
        self.refresh = QPushButton('立即更新')
        self.refresh.clicked.connect(lambda: self.update_news(True))
        bar.addWidget(self.refresh)
        bar.addStretch()
        bar.addWidget(label('每天 08:30 · 北京时间', 'meta'))
        layout.addLayout(bar)
        self.badge = Badge(self)
        screen = QApplication.primaryScreen().availableGeometry()
        position = self.settings.get('position', [screen.right()-180, screen.bottom()-205])
        self.badge.move(*position)
        self.clamp_badge()
        self.hover = QTimer(self)
        self.hover.setSingleShot(True)
        self.hover.timeout.connect(self.expand)
        self.menu = QMenu()
        self.menu.addAction('展开 / 固定', self.toggle_pin)
        self.menu.addAction('立即更新', lambda: self.update_news(True))
        self.autostart = self.menu.addAction('开机启动')
        self.autostart.setCheckable(True)
        self.autostart.setChecked(self.startup_path().exists())
        self.autostart.triggered.connect(self.set_autostart)
        self.menu.addSeparator()
        self.menu.addAction('退出世界一隅', self.quit_app)
        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setToolTip('世界一隅 · 每日 08:30 更新')
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(lambda reason: self.toggle_pin() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        if not offline:
            self.tray.show()
        self.poll = QTimer(self)
        self.poll.timeout.connect(self.check_pointer)
        self.poll.start(120)
        self.clock = QTimer(self)
        self.clock.timeout.connect(self.update_news)
        self.clock.start(30000)
        self.render()
        if not offline:
            self.badge.show()
            self.badge.lower()
            if '--install-startup' in sys.argv:
                self.set_autostart(True)
            QTimer.singleShot(600, self.update_news)

    def save_settings(self):
        write_json(SETTINGS, self.settings)

    def startup_path(self):
        return Path(os.environ.get('APPDATA', '')) / 'Microsoft/Windows/Start Menu/Programs/Startup/WorldWindow.vbs'

    def set_autostart(self, enabled):
        path = self.startup_path()
        try:
            if enabled:
                if getattr(sys, 'frozen', False):
                    command = '"' + sys.executable + '"'
                else:
                    exe = str(Path(sys.executable).with_name('pythonw.exe'))
                    command = f'"{exe}" "{Path(__file__).resolve()}"'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('CreateObject("WScript.Shell").Run "' + command.replace('"', '""') + '", 0, False\n', encoding='utf-16')
            else:
                path.unlink(missing_ok=True)
        except OSError:
            self.status.setText('开机启动设置失败，请检查启动文件夹权限。')
        self.autostart.setChecked(path.exists())

    def clamp_badge(self):
        screen = QApplication.screenAt(self.badge.pos()) or QApplication.primaryScreen()
        rect = screen.availableGeometry()
        self.badge.move(max(rect.left(), min(self.badge.x(), rect.right()-self.badge.width()+1)),
                        max(rect.top(), min(self.badge.y(), rect.bottom()-self.badge.height()+1)))

    def place_panel(self):
        rect = (QApplication.screenAt(self.badge.pos()) or QApplication.primaryScreen()).availableGeometry()
        self.setFixedSize(min(448, rect.width()-16), min(680, rect.height()-24))
        x = self.badge.x()-self.width()-8
        if x < rect.left()+8:
            x = self.badge.x()+self.badge.width()+8
        x = max(rect.left()+8, min(x, rect.right()-self.width()-8))
        y = max(rect.top()+8, min(self.badge.y(), rect.bottom()-self.height()-8))
        self.move(x, y)

    def expand(self):
        self.outside_since = None
        self.place_panel()
        self.show()
        self.foreground_when_expanded = foreground_window()[0]

    def collapse(self):
        self.pinned = False
        self.pin.setChecked(False)
        self.pin.setText('固定')
        self.hide()

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.pin.setChecked(self.pinned)
        self.pin.setText('已固定' if self.pinned else '固定')
        if self.pinned:
            self.expand()
            self.activateWindow()
        else:
            self.collapse()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.collapse()
        else:
            super().keyPressEvent(event)

    def check_pointer(self):
        if self.isVisible() and not self.menu.isVisible():
            handle, kind, pid = foreground_window()
            if (handle and handle != self.foreground_when_expanded and pid != os.getpid()
                    and kind not in ('Progman', 'WorkerW', 'SHELLDLL_DefView', 'SysListView32')):
                self.collapse()
                return
        if not self.isVisible() or self.pinned or self.menu.isVisible() or self.badge.press is not None:
            return
        pos = QCursor.pos()
        near = self.geometry().adjusted(-12, -12, 12, 12).contains(pos) or self.badge.geometry().adjusted(-12, -12, 12, 12).contains(pos)
        if near:
            self.outside_since = None
        elif self.outside_since is None:
            self.outside_since = time.monotonic()
        elif time.monotonic()-self.outside_since > 0.85:
            self.collapse()

    def update_news(self, force=False):
        if self.offline or (self.worker and self.worker.isRunning()):
            return
        if not force and (time.monotonic()-self.last_attempt < 900 or not due(self.news.get('updated'))):
            return
        self.last_attempt = time.monotonic()
        self.status.setText('正在读取多家新闻源，旧新闻可继续阅读…')
        self.refresh.setEnabled(False)
        self.worker = Fetcher(self)
        self.worker.finished_news.connect(self.received)
        self.worker.start()

    def received(self, result):
        self.refresh.setEnabled(True)
        if result['items']:
            self.news = result
            self.items = result['items']
            write_json(CACHE, self.news)
            self.render()
        else:
            old = self.news.get('updated', '')[:16].replace('T', ' ')
            self.status.setText('更新失败：新闻源暂时无法连接。' + (f'保留 {old} 的缓存。' if old else '请检查网络或代理，稍后会自动重试。'))
            if not self.items:
                self.render_items()

    def render(self):
        self.badge.refresh_visual()
        date = self.news.get('updated', '')[:16].replace('T', ' ')
        failed = self.news.get('failures', [])
        self.status.setText((f'更新于 {date} · 来源轮换展示' if date else '尚无新闻缓存 · 联网后自动更新') +
            (f' · {len(failed)} 个来源暂不可用' if failed else ''))
        self.render_items()

    def render_items(self):
        self.translation_widgets = {}
        box = QWidget()
        box.setStyleSheet('background:transparent;')
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(16)
        if not self.items:
            layout.addWidget(label('世界正在窗外，等待连接。', 'headline', True))
            layout.addWidget(label('新闻将使用媒体 RSS 原始标题、摘要和配图。\n\n如果外网来源不可访问，请使用右下角“立即更新”重试。没有配图的新闻会标注“来源未提供图片”。\n\n悬停展开，移开后收起；点击小摆件可固定阅读。', '', True))
        for index, item in enumerate(self.items):
            frame = QFrame()
            frame.setObjectName('story')
            story = QVBoxLayout(frame)
            story.setContentsMargins(0, 0, 0, 18)
            story.setSpacing(9)
            image = QPixmap(item.get('local_image', ''))
            if not image.isNull():
                picture = QLabel()
                width = max(260, self.width()-62)
                height = 205 if index == 0 else 155
                pix = image.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                pix = pix.copy(max(0, (pix.width()-width)//2), max(0, (pix.height()-height)//2), width, height)
                picture.setPixmap(pix)
                picture.setAccessibleName('新闻配图，来自 ' + item['source'])
                story.addWidget(picture)
            else:
                story.addWidget(label('来源未提供图片 / 图片暂不可用', 'meta'))
            headline = label(item['title'], 'headline', True)
            story.addWidget(headline)
            story.addWidget(label(item['source'] + ' · ' + item.get('published', ''), 'meta', True))
            summary_label = label(item.get('summary', ''), '', True)
            story.addWidget(summary_label)
            buttons = QHBoxLayout()
            original = QPushButton('阅读原文')
            original.clicked.connect(lambda checked=False, url=item['link']: webbrowser.open(url))
            translated = QPushButton('翻译摘要')
            translated.setToolTip('将标题与简介发送至 MyMemory 翻译，在本窗口显示中文。')
            key = translation_key(item)
            translated.setObjectName('translate_' + key)
            translated.clicked.connect(lambda checked=False, entry=item: self.toggle_translation(entry))
            buttons.addWidget(original)
            buttons.addWidget(translated)
            buttons.addStretch()
            story.addLayout(buttons)
            notice = label('', 'meta', True)
            notice.hide()
            story.addWidget(notice)
            self.translation_widgets[key] = (headline, summary_label, translated, notice, item)
            self.apply_translation(key)
            layout.addWidget(frame)
        layout.addStretch()
        previous = self.scroll.takeWidget()
        self.scroll.setWidget(box)
        if previous:
            previous.deleteLater()

    def apply_translation(self, key, error=''):
        if key not in self.translation_widgets:
            return
        title, summary, button, notice, item = self.translation_widgets[key]
        chinese = is_chinese(item['title'] + item.get('summary', ''))
        busy = key in self.translation_workers and self.translation_workers[key].isRunning()
        value = self.translations.get(key) if key in self.translated_visible else None
        title.setText(value['title'] if value else item['title'])
        summary.setText(value.get('summary', '') if value else item.get('summary', ''))
        button.setEnabled(not busy and not chinese)
        button.setText('已是中文' if chinese else '翻译中…' if busy else '显示原文' if value else '重试翻译' if error else '翻译摘要')
        notice.setText(error or ('MyMemory 机器译文 · 仅供理解大意' if value else ''))
        notice.setVisible(bool(notice.text()))

    def toggle_translation(self, item):
        if self.exiting:
            return
        key = translation_key(item)
        if key in self.translated_visible:
            self.translated_visible.remove(key)
            self.apply_translation(key)
        elif key in self.translations:
            self.translated_visible.add(key)
            self.apply_translation(key)
        elif key not in self.translation_workers:
            worker = Translator(key, item, self)
            self.translation_workers[key] = worker
            worker.translated.connect(self.translation_received)
            worker.finished.connect(lambda k=key: self.translation_finished(k))
            worker.start()
            self.apply_translation(key)

    def translation_received(self, key, value):
        if 'error' in value:
            self.translation_workers[key].translation_error = value['error']
        else:
            self.translations[key] = value
            self.translated_visible.add(key)
            # Bound the cache and keep all writes on the GUI thread.
            self.translations = dict(list(self.translations.items())[-500:])
            write_json(TRANSLATIONS, self.translations)

    def translation_finished(self, key):
        worker = self.translation_workers.pop(key, None)
        self.apply_translation(key, getattr(worker, 'translation_error', ''))
        if worker:
            worker.deleteLater()
        self.finish_quitting()

    def finish_quitting(self):
        if self.exiting and not self.translation_workers and not (self.worker and self.worker.isRunning()):
            QApplication.instance().quit()

    def quit_app(self):
        self.exiting = True
        self.clock.stop()
        self.hover.stop()
        self.collapse()
        self.badge.hide()
        self.tray.hide()
        # Let bounded network requests complete before exiting QThread cleanly.
        if self.worker and self.worker.isRunning():
            self.status.setText('正在结束网络请求…')
            self.worker.finished.connect(self.finish_quitting)
        self.finish_quitting()


def main():
    import ctypes
    ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
    ctypes.windll.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    ctypes.windll.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\WorldWindowDesktopWidget')
    if ctypes.windll.kernel32.GetLastError() == 183:
        return
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)
    app.setFont(QFont('Microsoft YaHei UI', 10))
    diagnostic = '--diagnostic-screenshot' in sys.argv
    window = Window(offline=diagnostic)
    if diagnostic:
        def capture():
            window.expand()
            app.processEvents()
            window.grab().save(str(APPDIR / 'packaged-check.png'))
            app.quit()
        QTimer.singleShot(500, capture)
    app.exec()
    ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == '__main__':
    main()

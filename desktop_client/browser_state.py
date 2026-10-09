"""Tree presentation, persisted navigation, and bounded asynchronous restoration."""
from __future__ import annotations

import json
from pathlib import PurePosixPath

from PySide6.QtCore import QPoint, QRect, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeView

from . import theme
from .history import source_key
from .naming import display_name

# A file's watch progress (HistoryEntry.progress), drawn by BrowserTree.
PROGRESS_ROLE = Qt.UserRole + 8


class _ProgressDelegate(QStyledItemDelegate):
    """Draws a file's watch progress right-aligned against its icon, over the
    empty space to the left of it (the row's padding and the branch cells,
    which a file leaves empty), on top of the row's hover or selection."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.font = QFont()
        self.font.setPixelSize(10)

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        text = index.data(PROGRESS_ROLE)
        if not text:
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        icon = style.subElementRect(QStyle.SE_ItemViewItemDecoration, opt, opt.widget)
        t = theme.current()
        painter.save()
        painter.setFont(self.font)
        painter.setPen(QColor(t.text_dim if text == "\u2713" else t.accent_hi))
        painter.drawText(QRect(icon.left() - 40, option.rect.top(), 38, option.rect.height()),
                         Qt.AlignRight | Qt.AlignVCenter, text)
        painter.restore()


class BrowserTree(QTreeView):
    """File tree that leaves the most room for names deep in subfolders: a
    14 px step per level, with a file's watch progress drawn left of its icon
    in space a file leaves empty anyway (_ProgressDelegate); 14 px is the
    least that fits "99%" there at the top level. A 24 px step, sized to fit
    it in the branch cell alone, cost a file four folders deep 50 px of its
    name."""

    INDENT = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setIndentation(self.INDENT)
        self.setItemDelegate(_ProgressDelegate(self))


class LocalLibraryProxy(QSortFilterProxyModel):
    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.history = history
        self.order = "name"
        self.simplify_names = False  # files shown as naming.display_name; sorting keeps the real names
        self.setDynamicSortFilter(True)

    def lessThan(self, left, right):
        model = self.sourceModel()
        a, b = model.fileInfo(left), model.fileInfo(right)
        def key(info):
            return (not info.isDir(), -info.lastModified().toMSecsSinceEpoch() if self.order == "mtime" else 0,
                    info.fileName().casefold(), info.fileName())
        return key(a) < key(b)

    def data(self, index, role=Qt.DisplayRole):
        if role == Qt.DecorationRole and index.column() == 0:
            # Theme-tinted outlines instead of the platform's file icons.
            is_dir = self.sourceModel().isDir(self.mapToSource(index))
            return theme.icon(theme.Icons.folder if is_dir else theme.Icons.movie)
        if role in (PROGRESS_ROLE, Qt.ToolTipRole, Qt.DisplayRole) and index.column() == 0:
            source = self.mapToSource(index)
            model = self.sourceModel()
            if not model.isDir(source):
                name = model.fileName(source)
                if role == Qt.DisplayRole:
                    return display_name(name) if self.simplify_names else name
                entry = self.history.entries.get(source_key("uplink", model.filePath(source)))
                if role == PROGRESS_ROLE:
                    return entry.progress if entry else None
                return f"{name}  —  {entry.description}" if entry else name
        return super().data(index, role)

    def set_order(self, order):
        self.order = order
        self.invalidate()
        self.sort(0)


class BrowserStore:
    def __init__(self, settings):
        self.settings = settings
        try:
            data = json.loads(settings.value("browser/v1", "{}"))
            if data.get("version") != 1:
                data = {}
        except (ValueError, TypeError, AttributeError):
            data = {}
        self.tab = data.get("tab", "Local")
        self.servers = data.get("servers", {})
        if not isinstance(self.servers, dict):
            self.servers = {}

    def save(self):
        self.settings.setValue("browser/v1", json.dumps({
            "version": 1, "tab": self.tab, "servers": self.servers,
        }))

    def capture(self, endpoint, tree, model):
        if not endpoint or tree is None or model is None:
            return
        expanded = []
        def visit(parent):
            for row in range(parent.rowCount()):
                item = parent.child(row)
                if tree.isExpanded(item.index()):
                    path = item.data(Qt.UserRole)
                    if isinstance(path, str):
                        expanded.append(path)
                    visit(item)
        visit(model.invisibleRootItem())
        selected = tree.currentIndex().data(Qt.UserRole)
        anchor = tree.indexAt(QPoint(2, 2)).data(Qt.UserRole)
        self.servers[endpoint] = {"expanded": expanded, "selected": selected, "anchor": anchor}
        self.save()

    def context(self, endpoint):
        value = self.servers.get(endpoint, {})
        if not isinstance(value, dict):
            return {}
        return value


async def restore_server_tree(context, tree, model, load, valid, *, type_role, loaded_role, cursor_role):
    """Restore ancestors first, loading at most ten pages of each directory."""
    counts = {}
    async def locate(path):
        parent = model.invisibleRootItem()
        nearest = None
        prefix = []
        for part in PurePosixPath(path).parts:
            prefix.append(part)
            target = "/".join(prefix)
            directory = "/".join(prefix[:-1])
            if nearest is not None and not parent.data(loaded_role):
                await load(parent, directory, reset=True)
                counts[directory] = counts.get(directory, 0) + 1
                if not valid():
                    return None
            while valid():
                found = next((parent.child(i) for i in range(parent.rowCount())
                              if parent.child(i).data(Qt.UserRole) == target
                              and parent.child(i).data(type_role) != "more"), None)
                if found is not None:
                    break
                more = next((parent.child(i) for i in range(parent.rowCount())
                             if parent.child(i).data(type_role) == "more"), None)
                # The initial directory page counts toward the ten-page cap.
                if more is None or counts.get(directory, 1) >= 10:
                    return nearest
                cursor = more.data(cursor_role)
                parent.removeRow(more.row())
                counts[directory] = counts.get(directory, 1) + 1
                await load(parent, directory, cursor=cursor)
                if not valid():
                    return None
            if not valid():
                return None
            nearest = found
            parent = found
            if len(prefix) < len(PurePosixPath(path).parts):
                # Load ourselves before expanding: avoids concurrent expanded slot fetches.
                if not found.data(loaded_role) and found.data(type_role) == "directory":
                    await load(found, target, reset=True)
                    counts[target] = counts.get(target, 0) + 1
                    if not valid():
                        return None
                tree.expand(found.index())
        return nearest
    expanded = context.get("expanded", [])
    if not isinstance(expanded, list):
        expanded = []
    for path in sorted((p for p in expanded if isinstance(p, str)), key=lambda p: (p.count("/"), p.casefold())):
        if not valid():
            return
        item = await locate(path)
        if item is not None and item.data(type_role) == "directory":
            if not item.data(loaded_role):
                await load(item, item.data(Qt.UserRole), reset=True)
                if not valid():
                    return
            tree.expand(item.index())
    for key in ("selected", "anchor"):
        path = context.get(key)
        if isinstance(path, str) and path and valid():
            item = await locate(path)
            if item is not None and valid():
                if key == "selected":
                    tree.setCurrentIndex(item.index())
                else:
                    tree.scrollTo(item.index(), tree.ScrollHint.PositionAtTop)

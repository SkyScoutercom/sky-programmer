"""Small shared widgets: a value tile, a banner, a status pill and a one-measurement strip chart."""
from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import theme

GAP_S = 1.5  # draw a break rather than a line across time with no data


def set_property(widget, name: str, value) -> None:
    """Change a property a stylesheet rule depends on, and make the rule take effect."""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Tile(QFrame):
    def __init__(self, label: str):
        super().__init__()
        self.setObjectName("Tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        caption = QLabel(label)
        caption.setObjectName("TileLabel")
        self.value = QLabel("—")
        self.value.setObjectName("TileValue")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(caption)
        lay.addWidget(self.value)

    def set(self, text: str, status: str = "") -> None:
        self.value.setText(text)
        if self.value.property("status") != status:
            set_property(self.value, "status", status)


class Banner(QFrame):
    def __init__(self, kind: str):
        super().__init__()
        self.setObjectName("Banner")
        self.setProperty("kind", kind)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(14, 8, 10, 8)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.row.addWidget(self.text, 1)
        self.hide()


class Pill(QLabel):
    """Connection state, in words as well as colour."""

    def __init__(self):
        super().__init__()
        self.setObjectName("Pill")
        self.show_state("idle", "○  Not connected")

    def show_state(self, state: str, text: str) -> None:
        self.setText(text)
        set_property(self, "state", state)


class StripChart(QWidget):
    """One measurement over the last minute, on its own axis. Hover to read any point."""

    MARGIN_LEFT, MARGIN_RIGHT, MARGIN_TOP, MARGIN_BOTTOM = 56, 18, 34, 24

    def __init__(self, title: str, unit: str, min_span: float, decimals: int = 1, window_s: float = 60.0):
        super().__init__()
        self.title, self.unit, self.min_span, self.decimals, self.window_s = title, unit, min_span, decimals, window_s
        self.points: deque = deque()
        self.stale = False
        self._hover_x = None
        self.setMouseTracking(True)
        self.setMinimumHeight(140)
        self.setAccessibleName(f"{title} chart, last {window_s:.0f} seconds")

    def add(self, t: float, value: float) -> None:
        self.stale = False
        self.points.append((t, value))
        while self.points and t - self.points[0][0] > self.window_s:
            self.points.popleft()
        self.update()

    def mark_stale(self) -> None:
        """No new values. The old line stays, but it must not read as the present."""
        self.stale = True
        self.update()

    def clear(self) -> None:
        self.points.clear()
        self.stale = False
        self.update()

    def mouseMoveEvent(self, event) -> None:
        self._hover_x = event.position().x()
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover_x = None
        self.update()

    def _text(self, value: float) -> str:
        return f"{value:,.{self.decimals}f}"

    def _range(self) -> tuple:
        values = [v for _, v in self.points]
        low, high = min(values), max(values)
        span = max((high - low) * 1.15, self.min_span)  # noise on a flat signal shouldn't fill the chart
        middle = (low + high) / 2
        return middle - span / 2, middle + span / 2

    def paintEvent(self, event) -> None:
        c = theme.current
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(c.border)))
        p.setBrush(QColor(c.surface))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        small, bold = QFont(self.font()), QFont(self.font())
        small.setPixelSize(11)
        bold.setPixelSize(13)
        bold.setBold(True)
        plot = QRectF(self.MARGIN_LEFT, self.MARGIN_TOP, self.width() - self.MARGIN_LEFT - self.MARGIN_RIGHT,
                      self.height() - self.MARGIN_TOP - self.MARGIN_BOTTOM)  # fmt: skip
        p.setFont(bold)
        p.setPen(QColor(c.ink))
        head = QRectF(14, 9, self.width() - 30, 20)
        p.drawText(head, Qt.AlignmentFlag.AlignLeft, f"{self.title} ({self.unit})")
        if self.points:
            latest = "no data" if self.stale else f"{self._text(self.points[-1][1])} {self.unit}"
            p.drawText(head, Qt.AlignmentFlag.AlignRight, latest)
        p.setFont(small)
        if not self.points:
            p.setPen(QColor(c.muted))
            p.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Waiting for data")
            return
        low, high = self._range()
        last = self.points[-1][0]

        def x_of(t):
            return plot.right() - (last - t) / self.window_s * plot.width()

        def y_of(v):
            return plot.bottom() - (v - low) / (high - low) * plot.height()

        for i in range(3):  # recessive grid and axis labels
            v = low + (high - low) * i / 2
            y = y_of(v)
            p.setPen(QPen(QColor(c.grid), 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor(c.muted))
            p.drawText(QRectF(4, y - 8, self.MARGIN_LEFT - 12, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text(v))  # fmt: skip
        bottom = QRectF(plot.left(), plot.bottom() + 5, plot.width(), 16)
        p.drawText(bottom, Qt.AlignmentFlag.AlignLeft, f"-{self.window_s:.0f} s")
        p.drawText(bottom, Qt.AlignmentFlag.AlignRight, "last value" if self.stale else "now")

        pen = QPen(QColor(c.muted if self.stale else c.brand), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        previous = None
        for t, v in self.points:
            if previous is not None and t - previous[0] <= GAP_S:
                p.drawLine(QPointF(x_of(previous[0]), y_of(previous[1])), QPointF(x_of(t), y_of(v)))
            previous = (t, v)
        if not self.stale:  # the live end of the line
            p.setPen(QPen(QColor(c.surface), 2))
            p.setBrush(QColor(c.brand))
            p.drawEllipse(QPointF(x_of(last), y_of(self.points[-1][1])), 4.5, 4.5)
        self._paint_hover(p, plot, x_of, y_of, last)

    def _paint_hover(self, p, plot, x_of, y_of, last) -> None:
        if self._hover_x is None or not plot.left() <= self._hover_x <= plot.right():
            return
        c = theme.current
        t, v = min(self.points, key=lambda point: abs(x_of(point[0]) - self._hover_x))
        x, y = x_of(t), y_of(v)
        p.setPen(QPen(QColor(c.muted), 1))
        p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        p.setPen(QPen(QColor(c.surface), 2))
        p.setBrush(QColor(c.brand))
        p.drawEllipse(QPointF(x, y), 5, 5)
        label = f"{self._text(v)} {self.unit}   {t - last:.0f} s"
        width = p.fontMetrics().horizontalAdvance(label) + 14
        box = QRectF(min(x + 8, plot.right() - width), plot.top() + 2, width, 22)
        p.setPen(QPen(QColor(c.border_strong), 1))
        p.setBrush(QColor(c.surface))
        p.drawRoundedRect(box, 6, 6)
        p.setPen(QColor(c.ink))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

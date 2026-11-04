"""Read the config, stage edits, and send them through the verified write path. Nothing is written from the table."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..config import APP_ROOT, MC_ROOT
from ..errors import SafetyRefusal
from . import theme

KEY_FIELDS = (
    "motor_type", "si_motor_poles", "l_current_max", "l_current_min", "l_abs_current_max", "l_in_current_max",
    "l_in_current_min", "l_min_vin", "l_max_vin", "l_battery_cut_start", "l_battery_cut_end", "l_max_erpm",
    "l_min_erpm", "l_max_duty", "l_temp_fet_start", "l_temp_fet_end", "m_bldc_f_sw_min", "m_bldc_f_sw_max",
    "sl_bemf_coupling_k", "sl_cycle_int_limit", "sl_min_erpm", "foc_motor_r", "foc_motor_l", "foc_motor_flux_linkage",
    "foc_current_kp", "foc_current_ki", "foc_f_zv",
)  # fmt: skip
GROUPS = {
    "Key settings": None,
    "All": (),
    "Limits": ("l_",),
    "FOC": ("foc_",),
    "BLDC sensorless": ("sl_",),
    "Motor hardware": ("m_",),
    "Hall sensors": ("hall_",),
    "Speed, position and current control": ("s_", "p_", "cc_", "sp_"),
    "Setup information": ("si_",),
    "BMS": ("bms.",),
}
FIELD, VALUE, RANGE, NOTE = range(4)


class NoWheelCombo(QComboBox):
    """Scrolling past a dropdown in a long table must not change a motor setting."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class FieldTable(QTableWidget):
    edited = Signal()

    def __init__(self):
        super().__init__(0, 4)
        self.setHorizontalHeaderLabels(("Field", "Value", "Allowed range", "Note"))
        self.verticalHeader().hide()
        self.setAlternatingRowColors(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(FIELD, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(VALUE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(RANGE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(NOTE, QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(VALUE, 240)
        self.rows, self.original, self.base_note, self.staged, self.invalid = {}, {}, {}, {}, {}
        self.layout_, self.root = None, MC_ROOT
        self._loading = False
        self.itemChanged.connect(self._item_changed)

    def fill(self, values: dict, layout, root: str, hw: str, editable: bool) -> None:
        self._loading = True
        self.layout_, self.root = layout, root
        self.rows, self.original, self.base_note, self.staged, self.invalid = {}, {}, {}, {}, {}
        self.clearContents()
        self.setRowCount(len(values))
        for row, (field, value) in enumerate(values.items()):
            self.rows[field] = row
            shown = config.show(layout, root, field, value)
            self.original[field] = shown
            why_locked = self._lock_reason(field, values, hw) if editable else "Read only: VESC Tool writes app config."
            self.base_note[field] = why_locked or self._hint(field)
            name = QTableWidgetItem(field)
            name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.setItem(row, FIELD, name)
            self._fill_value(row, field, value, shown, locked=bool(why_locked))
            limit = layout.mc_limits.get(field) if root == MC_ROOT else None
            for col, text in ((RANGE, f"{limit[0]:g} to {limit[1]:g}" if limit else ""), (NOTE, self.base_note[field])):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.setItem(row, col, item)
        self._loading = False
        self.edited.emit()

    def _lock_reason(self, field: str, values: dict, hw: str) -> str:
        """Fields the planner would always refuse here are locked up front, with the planner's own reason."""
        if field.startswith(config.PROTECTED_PREFIXES):
            return "Calibration: the firmware measures this itself."
        if field.startswith("_"):
            return "Internal field."
        risky = config.risky_changes([field], values, values, self.layout_, hw)
        return f"Locked here. {risky[0]}. Only the `sky` terminal command can override this." if risky else ""

    @staticmethod
    def _hint(field: str) -> str:
        if field in config.BOOT_CRITICAL:
            return "A wrong value can hang the board at boot, so this range has no override."
        return ""

    def _fill_value(self, row, field, value, shown, locked) -> None:
        layout, root = self.layout_, self.root
        names = layout.enum_of(root, field)
        is_bool = layout.field_type(root, field) == "bool"
        if not locked and (names or is_bool):
            combo = NoWheelCombo()
            combo.addItems(list(names.values()) if names else ["off", "on"])
            if is_bool:
                self.original[field] = "on" if value else "off"
            combo.setCurrentText(self.original[field])
            combo.currentTextChanged.connect(lambda _, f=field: self._edited(f))
            self.setCellWidget(row, VALUE, combo)
            return
        item = QTableWidgetItem(shown)
        if locked:
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            item.setBackground(QBrush(QColor(theme.LOCKED)))
        self.setItem(row, VALUE, item)

    def _item_changed(self, item) -> None:
        if not self._loading and item.column() == VALUE:
            self._edited(self.item(item.row(), FIELD).text())

    def text_of(self, field: str) -> str:
        row = self.rows[field]
        widget = self.cellWidget(row, VALUE)
        return (widget.currentText() if widget else self.item(row, VALUE).text()).strip()

    def set_value(self, field: str, text: str) -> None:
        row = self.rows[field]
        widget = self.cellWidget(row, VALUE)
        if widget:
            widget.setCurrentText(text)
        else:
            self.item(row, VALUE).setText(text)

    def _edited(self, field: str) -> None:
        text = self.text_of(field)
        self.staged.pop(field, None)
        self.invalid.pop(field, None)
        note, color = self.base_note[field], None
        if text != self.original[field]:
            try:
                value = config.parse_value(self.layout_, self.root, field, text)
                limit = self.layout_.mc_limits.get(field)
                if limit and not limit[0] <= value <= limit[1]:
                    raise SafetyRefusal(f"Outside its range, {limit[0]:g} to {limit[1]:g}.")
                self.staged[field] = text
                note, color = f"Changed from {self.original[field]}", theme.CHANGED
            except SafetyRefusal as e:
                self.invalid[field] = str(e)
                note, color = str(e), theme.INVALID
        self._paint_row(field, note, color)
        self.edited.emit()

    def _paint_row(self, field, note, color) -> None:
        self._loading = True
        row = self.rows[field]
        self.item(row, NOTE).setText(note)
        brush = QBrush(QColor(color)) if color else QBrush()
        for col in (FIELD, VALUE, RANGE, NOTE):
            item = self.item(row, col)
            if item:
                item.setBackground(brush)
        self._loading = False

    def apply_filter(self, group: str, search: str) -> None:
        prefixes = GROUPS.get(group, ())
        needle = search.strip().lower()
        for field, row in self.rows.items():
            if self.root == APP_ROOT:
                match = True
            elif prefixes is None:
                match = field in KEY_FIELDS
            else:
                match = not prefixes or field.startswith(prefixes)
            if needle:  # a search covers every field, so a setting is never hidden by the chosen group
                match = needle in field.lower()
            self.setRowHidden(row, not match)


class ConfigPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._hw = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        title = QLabel("Configuration")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        bar = QHBoxLayout()
        self.group = NoWheelCombo()
        self.group.addItems(list(GROUPS))
        self.group.currentTextChanged.connect(self._refilter)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search all fields, e.g. foc_motor")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refilter)
        self.read_button = QPushButton("Read from board")
        self.read_button.clicked.connect(self.read)
        self.discard_button = QPushButton("Discard changes")
        self.discard_button.clicked.connect(self.discard)
        self.review_button = QPushButton("Review && write…")
        self.review_button.setObjectName("Primary")
        self.review_button.clicked.connect(self.review)
        bar.addWidget(self.group)
        bar.addWidget(self.search, 1)
        for b in (self.read_button, self.discard_button, self.review_button):
            bar.addWidget(b)
        root.addLayout(bar)

        self.status = QLabel()
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.tabs = QTabWidget()
        self.motor, self.app = FieldTable(), FieldTable()
        self.tabs.addTab(self.motor, "Motor config")
        self.tabs.addTab(self.app, "App config (read only)")
        self.tabs.currentChanged.connect(lambda _: self._refilter())
        self.motor.edited.connect(self._sync_buttons)
        root.addWidget(self.tabs, 1)
        self._enabled, self._busy = False, False
        self._sync_buttons()

    # ---------------------------------------------------------------- board actions
    def read(self) -> None:
        board = self.win.board
        if board is None:
            return
        self.status.setText("Reading the board…")

        def job(prog):
            return prog.read_conf(MC_ROOT), prog.read_conf(APP_ROOT)

        board.submit(job, self._loaded, self._read_failed)

    def _loaded(self, result) -> None:
        mc, app = result
        lay = self.win.layout
        if lay is None:  # disconnected while the read was on its way
            return
        self.motor.fill(mc, lay, MC_ROOT, self._hw, editable=True)
        self.app.fill(app, lay, APP_ROOT, self._hw, editable=False)
        self.win.set_mode(config.show(lay, MC_ROOT, "motor_type", mc["motor_type"]))
        self._refilter()
        self._sync_buttons()

    def _read_failed(self, error) -> None:
        self.status.setText(self.win.words(error))

    def review(self) -> None:
        if self.motor.invalid or not self.motor.staged:
            return
        changes = dict(self.motor.staged)
        self.win.write_flow(lambda prog: prog.plan_set(changes), after=self.read)

    def discard(self) -> None:
        self.read()

    # ---------------------------------------------------------------- state
    def _refilter(self) -> None:
        table = self.tabs.currentWidget()
        self.group.setEnabled(table is self.motor)
        table.apply_filter(self.group.currentText(), self.search.text())

    def _sync_buttons(self) -> None:
        staged, invalid = len(self.motor.staged), len(self.motor.invalid)
        idle = self._enabled and not self._busy
        self.review_button.setEnabled(idle and staged > 0 and invalid == 0)
        self.discard_button.setEnabled(idle and staged + invalid > 0)
        self.read_button.setEnabled(idle and staged + invalid == 0)  # a read would silently drop the edits
        if not self._enabled:
            return
        if invalid:
            self.status.setText(f"{invalid} value{'s' if invalid > 1 else ''} can't be used; see the Note column.")
        elif staged:
            self.status.setText(f"{staged} change{'s' if staged > 1 else ''} ready to review. "
                                "Nothing reaches the board until you confirm.")  # fmt: skip
        else:
            self.status.setText("Showing the board's working copy. Edits stay here until you review and confirm them.")

    def on_connected(self, info: dict) -> None:
        self._hw = info["ident"]["hw"]
        if info["layout"] is not None:
            self.read()

    def reset(self) -> None:
        for table in (self.motor, self.app):
            table.setRowCount(0)
            table.rows, table.original, table.staged, table.invalid = {}, {}, {}, {}
        self.status.setText("")
        self._sync_buttons()

    def set_state(self, connected: bool, busy: bool, config_ok: bool) -> None:
        self._enabled, self._busy = connected and config_ok, busy
        self.tabs.setEnabled(self._enabled)
        self.search.setEnabled(self._enabled)
        self._sync_buttons()

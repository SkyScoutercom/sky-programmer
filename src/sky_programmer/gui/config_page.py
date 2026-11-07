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
# Plain names for the settings people change most. Every other field shows its firmware name only.
LABELS = {
    "motor_type": "Motor type", "si_motor_poles": "Motor poles", "l_current_max": "Motor current limit (A)",
    "l_current_min": "Motor regen limit (A)", "l_abs_current_max": "Absolute current limit (A)",
    "l_in_current_max": "Battery current limit (A)", "l_in_current_min": "Battery regen limit (A)",
    "l_min_vin": "Minimum input voltage (V)", "l_max_vin": "Maximum input voltage (V)",
    "l_battery_cut_start": "Battery cutoff start (V)", "l_battery_cut_end": "Battery cutoff end (V)",
    "l_max_erpm": "Maximum speed (erpm)", "l_min_erpm": "Minimum speed (erpm)", "l_max_duty": "Maximum duty",
    "l_temp_fet_start": "MOSFET limit start (°C)", "l_temp_fet_end": "MOSFET limit end (°C)",
    "m_bldc_f_sw_min": "BLDC min switching (Hz)", "m_bldc_f_sw_max": "BLDC max switching (Hz)",
    "sl_bemf_coupling_k": "BEMF coupling", "sl_cycle_int_limit": "Cycle integrator limit",
    "sl_min_erpm": "Sensorless min speed (erpm)", "foc_motor_r": "FOC motor resistance (ohm)",
    "foc_motor_l": "FOC motor inductance (H)", "foc_motor_flux_linkage": "FOC flux linkage (Wb)",
    "foc_current_kp": "FOC current Kp", "foc_current_ki": "FOC current Ki", "foc_f_zv": "FOC switching (Hz)",
}  # fmt: skip
FIELD, VALUE, RANGE, NOTE = range(4)
FIELD_ROLE = Qt.ItemDataRole.UserRole


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
        self.verticalHeader().setDefaultSectionSize(38)
        self.setShowGrid(False)
        self.rows, self.original, self.base_note, self.staged, self.invalid = {}, {}, {}, {}, {}
        self.locked: set = set()
        self.layout_, self.root = None, MC_ROOT
        self._loading = False
        self.itemChanged.connect(self._item_changed)

    def fill(self, values: dict, layout, root: str, hw: str, editable: bool) -> None:
        self._loading = True
        self.layout_, self.root = layout, root
        self.rows, self.original, self.base_note, self.staged, self.invalid = {}, {}, {}, {}, {}
        self.locked = set()
        self.clearContents()
        self.setRowCount(len(values))
        for row, (field, value) in enumerate(values.items()):
            self.rows[field] = row
            shown = config.show(layout, root, field, value)
            self.original[field] = shown
            why_locked = self._lock_reason(field, values, hw) if editable else "Read only: VESC Tool writes app config."
            self.base_note[field] = why_locked or self._hint(field)
            name = QTableWidgetItem(f"{LABELS[field]}  ·  {field}" if field in LABELS else field)
            name.setData(FIELD_ROLE, field)
            name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.setItem(row, FIELD, name)
            self._fill_value(row, field, value, shown, locked=bool(why_locked))  # may update base_note
            limit = layout.mc_limits.get(field) if root == MC_ROOT else None
            for col, text in ((RANGE, f"{limit[0]:g} to {limit[1]:g}" if limit else ""), (NOTE, self.base_note[field])):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.setItem(row, col, item)
        self._loading = False
        self.retheme()
        self.edited.emit()

    def _lock_reason(self, field: str, values: dict, hw: str) -> str:
        """Fields the planner would always refuse here are locked up front, with the planner's own reason."""
        if field.startswith(config.PROTECTED_PREFIXES):
            return "Calibration: the firmware measures this itself."
        if field.startswith("_"):
            return "Internal field."
        if field.startswith("hall_table"):
            return "Locked: hall detection sets this. It's stored as one byte, so 255 means -1."
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
            if combo.findText(self.original[field]) < 0:  # never let a dropdown show a value the board doesn't hold
                combo.insertItem(0, self.original[field])
                self.base_note[field] = f"The board holds {self.original[field]}, which isn't a valid choice."
            combo.setCurrentText(self.original[field])
            combo.currentTextChanged.connect(lambda _, f=field: self._edited(f))
            # An empty item behind the dropdown, so the row's highlight reaches across this cell too.
            behind = QTableWidgetItem("")
            behind.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.setItem(row, VALUE, behind)
            self.setCellWidget(row, VALUE, combo)
            return
        item = QTableWidgetItem(shown)
        if locked:
            self.locked.add(field)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.setItem(row, VALUE, item)

    def _item_changed(self, item) -> None:
        if not self._loading and item.column() == VALUE:
            self._edited(self.item(item.row(), FIELD).data(FIELD_ROLE))

    def _dropdown(self, row: int):
        """The cell's dropdown, or None. While a cell is being typed in, Qt reports its text editor as the cell's
        widget too, so only a dropdown counts here: the bench found that editor being taken for one."""
        widget = self.cellWidget(row, VALUE)
        return widget if isinstance(widget, QComboBox) else None

    def text_of(self, field: str) -> str:
        row = self.rows[field]
        dropdown = self._dropdown(row)
        return (dropdown.currentText() if dropdown else self.item(row, VALUE).text()).strip()

    def set_value(self, field: str, text: str) -> None:
        row = self.rows[field]
        dropdown = self._dropdown(row)
        if dropdown:
            dropdown.setCurrentText(text)
        else:
            self.item(row, VALUE).setText(text)

    def _edited(self, field: str) -> None:
        text = self.text_of(field)
        self.staged.pop(field, None)
        self.invalid.pop(field, None)
        note = self.base_note[field]
        if text != self.original[field]:
            try:
                value = config.parse_value(self.layout_, self.root, field, text)
                limit = self.layout_.mc_limits.get(field)
                if limit and not limit[0] <= value <= limit[1]:
                    raise SafetyRefusal(f"Outside its range, {limit[0]:g} to {limit[1]:g}.")
                self.staged[field] = text
                note = f"Changed from {self.original[field]}"
            except SafetyRefusal as e:
                self.invalid[field] = str(e)
                note = str(e)
        self._loading = True
        self.item(self.rows[field], NOTE).setText(note)
        self._loading = False
        self._paint_row(field)
        self.edited.emit()

    def _paint_row(self, field) -> None:
        c = theme.current
        color = c.invalid if field in self.invalid else c.changed if field in self.staged else None
        row = self.rows[field]
        was_loading, self._loading = self._loading, True
        for col in (FIELD, VALUE, RANGE, NOTE):
            item = self.item(row, col)
            if item:
                shade = color or (c.locked if col == VALUE and field in self.locked else None)
                item.setBackground(QBrush(QColor(shade)) if shade else QBrush())
        self._loading = was_loading

    def retheme(self) -> None:
        """Row colours are set per item, so a stylesheet change doesn't reach them."""
        for field in self.rows:
            self._paint_row(field)

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
                match = needle in field.lower() or needle in LABELS.get(field, "").lower()
            self.setRowHidden(row, not match)


class ConfigPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._hw = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)
        title = QLabel("Configuration")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        bar = QHBoxLayout()
        self.group = NoWheelCombo()
        self.group.addItems(list(GROUPS))
        self.group.setMinimumWidth(190)
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
        self._read_error = ""
        self._sync_buttons()

    def retheme(self) -> None:
        self.motor.retheme()
        self.app.retheme()

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
        self._read_error = ""
        self.win.set_mode(config.show(lay, MC_ROOT, "motor_type", mc["motor_type"]))
        self._refilter()
        self._sync_buttons()

    def _read_failed(self, error) -> None:
        """Clear the table: old values next to "the board's working copy" would be false."""
        self._clear_tables()
        self._read_error = f"Couldn't read the board, so nothing is shown. {self.win.words(error)}"
        self._sync_buttons()

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
        if self._read_error:
            self.status.setText(self._read_error)
        elif invalid:
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

    def _clear_tables(self) -> None:
        for table in (self.motor, self.app):
            table.setRowCount(0)
            table.rows, table.original, table.staged, table.invalid = {}, {}, {}, {}
            table.locked = set()

    def reset(self) -> None:
        self._clear_tables()
        self._read_error = ""
        self.status.setText("")
        self._sync_buttons()

    def set_state(self, connected: bool, busy: bool, config_ok: bool) -> None:
        self._enabled, self._busy = connected and config_ok, busy
        self.tabs.setEnabled(self._enabled and not busy)  # an edit made during a write would be lost in the re-read
        self.search.setEnabled(self._enabled)
        self._sync_buttons()

"""可复用的单测点编辑区；独立管理启用、地址、数据源和参数切换。"""
from .i18n import tr, bind, add_item, retranslate_tree
from dataclasses import asdict

from PySide6.QtCore import Signal, Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QHBoxLayout, QLineEdit,
    QVBoxLayout, QWidget,
)

from ..domain.config import PointConfig, SOURCE_FIELDS
from ..domain.numeric import NUMERIC_TYPES, ORDERS, canonical_order, numeric_type, parse_integer
from .context_choices import ContextComboBox
from .theme import METRICS
from .widgets import CheckBox, ComboBox, SpinBox, label, refresh_style


class PointEditor(QGroupBox):
    changed = Signal()

    def __init__(self, index, parent=None):
        super().__init__(parent)
        bind(self, "setTitle", tr("Point {p0}").format(p0=index + 1))
        self.index = index
        self._loading = False
        self._source_used = True
        self._source_tip = ""
        self._default_order = "B1234"
        self._point = PointConfig(enabled=False)
        self.setObjectName(f"point-{index + 1}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, METRICS.section_spacing, 0, 0)
        layout.setSpacing(METRICS.spacing)
        header = QHBoxLayout()
        self.enabled = CheckBox(tr('Enable'))
        bind(self.enabled, 'setAccessibleName', tr('Enable point {p0}').format(p0=index + 1))
        self.range_label = label("", "pointRange")
        self.range_label.setWordWrap(True)
        self.range_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.enabled)
        header.addWidget(self.range_label, 1)
        layout.addLayout(header)
        self.body = QWidget()
        self.form = QFormLayout(self.body)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setHorizontalSpacing(METRICS.section_spacing)
        self.form.setVerticalSpacing(METRICS.spacing)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.fields = {"name": QLineEdit(), "address": SpinBox(),
                       "data_type": ComboBox(), "byte_order": ContextComboBox(normalize=canonical_order),
                       "source.kind": ContextComboBox(allow_unknown=True),
                       "source.value": QLineEdit(), "source.amplitude": QLineEdit(),
                       "source.frequency_hz": QLineEdit(), "source.lower": QLineEdit(), "source.upper": QLineEdit()}
        bind(self.fields['name'], 'setPlaceholderText', tr('Optional, e.g. temperature or pressure'))
        self.fields["address"].setRange(0, 65535)
        bind(self.fields['address'], 'setToolTip', tr('Zero-based register address; the selected data type determines register width.'))
        for data_type in NUMERIC_TYPES:
            self.fields["data_type"].addItem(data_type, data_type)
        self.fields["byte_order"].addItem("", None)
        for order in ORDERS:
            self.fields["byte_order"].addItem(order, order)
        bind(self.fields['byte_order'], 'setToolTip', tr('Only byte orders matching this type are shown. B12 is the default for 16-bit points.'))
        bind(self.fields['byte_order'], 'setPlaceholderText', tr('Select a compatible byte order'))
        add_item(self.fields['source.kind'], tr('Constant'), 'constant')
        add_item(self.fields['source.kind'], tr('Sine wave'), 'sine')
        add_item(self.fields['source.kind'], tr('Triangle wave'), 'triangle')
        bind(self.fields['source.kind'], 'setPlaceholderText', tr('Select a compatible data source'))
        for key, caption in (("name", tr('Name')), ("address", tr('Start address')),
                             ("data_type", tr('Data type')), ("byte_order", tr('Byte order')), ("source.kind", tr('Data source')),
                             ("source.value", tr('Value')), ("source.amplitude", tr('Amplitude')),
                             ("source.frequency_hz", tr('Frequency (Hz)')), ("source.lower", tr('Lower bound')),
                             ("source.upper", tr('Upper bound'))):
            widget = self.fields[key]
            widget.setObjectName(f"points.{index}.{key}")
            bind(widget, 'setAccessibleName', tr('Point {p0} · {p1}').format(p0=index + 1, p1=caption))
            self.form.addRow(label(caption), widget)
        layout.addWidget(self.body)
        self.source_note = label("", "caption")
        self.source_note.setWordWrap(True)
        layout.addWidget(self.source_note)
        self.selection_note = label("", "caption")
        self.selection_note.setWordWrap(True)
        self.selection_note.setProperty("tone", "error")
        layout.addWidget(self.selection_note)
        self.enabled.toggled.connect(self._changed)
        self.fields["address"].valueChanged.connect(self._changed)
        self.fields["source.kind"].currentIndexChanged.connect(self._changed)
        self.fields["data_type"].currentIndexChanged.connect(self._changed)
        self.fields["byte_order"].currentIndexChanged.connect(self._changed)
        for key in ("name", "source.value", "source.amplitude", "source.frequency_hz", "source.lower", "source.upper"):
            self.fields[key].textChanged.connect(self._changed)

    def _changed(self, *_):
        if self._loading:
            return
        active = self.enabled.isChecked()
        specification = numeric_type(self.current_data_type())
        orders = self.fields["byte_order"]
        with QSignalBlocker(orders):
            orders.setItemText(orders.findData(None), str(tr('Follow global ({order})').format(order=self._default_order)))
        orders.filter_values(specification.orders if specification.registers == 1 else (None, *specification.orders),
                             aliases={None: "B12"} if specification.registers == 1 else None)
        # 不兼容的旧选择保留在草稿中，界面留空并即时解释，避免静默替换。
        source_kind = self.fields["source.kind"]
        source_kind.filter_values(tuple(key for key in SOURCE_FIELDS if key != "sine" or not specification.integer))
        self.body.setVisible(active)
        kind = source_kind.currentData()
        sine = kind == "sine"
        self._source_row("source.kind", active and self._source_used)
        bind(source_kind, 'setToolTip', self._source_tip)
        bind(self.form.labelForField(self.fields['source.value']), 'setText', tr('Offset') if sine else tr('Value'))
        for key in ("value", "amplitude", "frequency_hz", "lower", "upper"):
            self._source_row("source." + key, active and self._source_used and key in SOURCE_FIELDS.get(kind, ()))
        bind(self.source_note, 'setText', tr('Values come from the remote slave; no local source is used.') if not self._source_used
             else tr('Fixed step 1 per data update; starts at the lower bound and reverses after reaching each bound.') if kind == "triangle" else "")
        self.source_note.setVisible(active and (not self._source_used or kind == "triangle"))
        issues = self.selection_issues()
        message = issues[0] if issues else ""
        for issue in issues[1:]:
            message = message + " · " + issue
        bind(self.selection_note, 'setText', message)
        self.selection_note.setVisible(active and bool(issues))
        self.set_conflict(False)
        self.changed.emit()

    def _source_row(self, key, visible):
        widget = self.fields[key]
        self.form.setRowVisible(widget, visible)
        widget.setEnabled(visible)
        self.form.labelForField(widget).setEnabled(visible)

    def selection_issues(self):
        if not self.enabled.isChecked():
            return []
        issues = []
        if not self.fields["byte_order"].compatible():
            issues.append(tr('Previous byte order is incompatible; select a byte order for this type.'))
        if self._source_used and not self.fields["source.kind"].compatible():
            issues.append(tr('Previous data source is incompatible; select a data source for this type.'))
        return issues

    def set_default_order(self, order):
        self._default_order = order
        self._changed()

    def retranslate_ui(self):
        retranslate_tree(self)
        orders = self.fields["byte_order"]
        with QSignalBlocker(orders):
            orders.setItemText(orders.findData(None), str(tr('Follow global ({order})').format(order=self._default_order)))

    def set_usage(self, usage):
        self._source_used = usage.source
        if usage.master:
            self._source_tip = tr('Generate values for the master to write to remote holding registers.')
        elif usage.mode == "rtu-slave":
            self._source_tip = tr('The source updates input registers continuously and initializes holding registers once; later source updates leave holding registers unchanged.')
        else:
            self._source_tip = tr('Update the register snapshot read by TCP clients.')
        caption = (tr('Write start address') if usage.writing else tr('Read start address')) if usage.master else tr('Local start address')
        bind(self.form.labelForField(self.fields['address']), 'setText', caption)
        bind(self.fields['address'], 'setAccessibleName', tr('Point {p0} · {p1}').format(p0=self.index + 1, p1=caption))
        self._changed()

    def set_conflict(self, conflict):
        address = self.fields["address"].value()
        data_type = self.current_data_type()
        overflow = self.enabled.isChecked() and address + numeric_type(data_type).registers > 65536
        text = (tr('Registers {p0}–{p1} · {data_type}').format(p0=address, p1=address + numeric_type(data_type).registers - 1, data_type=data_type)
                if self.enabled.isChecked() else tr('Disabled · No registers occupied'))
        bind(self.range_label, 'setText', text + (tr(' · Address overlap') if conflict else '')
             + (tr(' · Address range exceeds 65535') if overflow else ''))
        self.range_label.setProperty("tone", "error" if conflict or overflow or self.selection_issues() else "idle")
        refresh_style(self.range_label)

    def apply_point(self, point):
        self._loading = True
        self._point = point
        try:
            self.enabled.setChecked(point.enabled)
            bind(self.fields['name'], 'setText', str(point.name))
            valid_address = isinstance(point.address, int) and not isinstance(point.address, bool)
            self.fields["address"].setValue(point.address if valid_address else 0)
            self.fields["data_type"].setCurrentIndex(self.fields["data_type"].findData(point.data_type))
            self.fields["byte_order"].set_draft_value(point.byte_order)
            kind = self.fields["source.kind"]
            kind.set_draft_value(point.source.kind)
            for key in ("value", "amplitude", "frequency_hz", "lower", "upper"):
                bind(self.fields['source.' + key], 'setText', str(getattr(point.source, key)))
        finally:
            self._loading = False
        self._changed()

    def configuration(self):
        """有效参数转换为数值；无关草稿保留，切换操作后再校验。"""
        active = self.enabled.isChecked()
        issues = self.selection_issues()
        if issues:
            raise ValueError(tr('Point {p0}: {message}').format(p0=self.index + 1, message=issues[0]))
        kind = self.fields["source.kind"].draft_value()
        if kind is None:
            kind = self._point.source.kind
        data = asdict(self._point)
        data.update(enabled=active, name=self.fields["name"].text().strip(),
                    address=self.fields["address"].value())
        data["byte_order"] = self.fields["byte_order"].draft_value()
        if self.fields["data_type"].currentIndex() >= 0:
            data["data_type"] = self.fields["data_type"].currentData()
        if not active and not (isinstance(self._point.address, int) and not isinstance(self._point.address, bool)
                               and 0 <= self._point.address <= 65535):
            data["address"] = self._point.address
        data["source"]["kind"] = kind
        for key, caption in (("value", tr('Value / offset')), ("amplitude", tr('Amplitude')), ("frequency_hz", tr('Frequency (Hz)')),
                             ("lower", tr('Lower bound')), ("upper", tr('Upper bound'))):
            draft = self.fields["source." + key].text().strip()
            used = active and self._source_used and key in SOURCE_FIELDS.get(kind, ())
            if not used:
                if draft != str(data["source"][key]):
                    data["source"][key] = draft
                continue
            try:
                data["source"][key] = (parse_integer(draft, data["data_type"])
                                       if numeric_type(data["data_type"]).integer else float(draft))
            except ValueError:
                raise ValueError(tr('Point {p0} · {p1}: enter a valid number').format(p0=self.index + 1, p1=caption)) from None
        return data

    def current_data_type(self):
        """无效的禁用草稿仅按默认宽度预览，不修改其保存内容。"""
        return self.fields["data_type"].currentData() or "float32"

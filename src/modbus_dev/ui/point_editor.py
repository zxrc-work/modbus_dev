"""可复用的单测点编辑区；独立管理启用、地址、数据源和参数切换。"""
from .i18n import tr, bind, add_item, retranslate_tree
from dataclasses import asdict

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QHBoxLayout, QLineEdit,
    QVBoxLayout, QWidget,
)

from ..domain.config import PointConfig, REGISTERS_PER_POINT
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
        self.fields = {"name": QLineEdit(), "address": SpinBox(), "source.kind": ComboBox(),
                       "source.value": QLineEdit(), "source.amplitude": QLineEdit(),
                       "source.frequency_hz": QLineEdit()}
        bind(self.fields['name'], 'setPlaceholderText', tr('Optional, e.g. temperature or pressure'))
        self.fields["address"].setRange(0, 65536 - REGISTERS_PER_POINT)
        bind(self.fields['address'], 'setToolTip', tr('Zero-based register address; each float32 point occupies two registers.'))
        add_item(self.fields['source.kind'], tr('Constant'), 'constant')
        add_item(self.fields['source.kind'], tr('Sine wave'), 'sine')
        for key, caption in (("name", tr('Name')), ("address", tr('Start address')), ("source.kind", tr('Data source')),
                             ("source.value", tr('Value')), ("source.amplitude", tr('Amplitude')),
                             ("source.frequency_hz", tr('Frequency (Hz)'))):
            widget = self.fields[key]
            widget.setObjectName(f"points.{index}.{key}")
            bind(widget, 'setAccessibleName', tr('Point {p0} · {p1}').format(p0=index + 1, p1=caption))
            self.form.addRow(label(caption), widget)
        layout.addWidget(self.body)
        self.enabled.toggled.connect(self._changed)
        self.fields["address"].valueChanged.connect(self._changed)
        self.fields["source.kind"].currentIndexChanged.connect(self._changed)
        for key in ("name", "source.value", "source.amplitude", "source.frequency_hz"):
            self.fields[key].textChanged.connect(self._changed)

    def _changed(self, *_):
        if self._loading:
            return
        active = self.enabled.isChecked()
        self.body.setVisible(active)
        sine = self.fields["source.kind"].currentData() == "sine"
        for key in ("source.kind", "source.value"):
            self.fields[key].setEnabled(active and self._source_used)
            self.form.labelForField(self.fields[key]).setEnabled(active and self._source_used)
            bind(self.fields[key], 'setToolTip', self._source_tip if self._source_used else tr('Read-only operations generate no local data; values are supplied by the remote slave.'))
        bind(self.form.labelForField(self.fields['source.value']), 'setText', tr('Offset') if sine else tr('Value'))
        for key in ("source.amplitude", "source.frequency_hz"):
            self.fields[key].setVisible(sine)
            self.fields[key].setEnabled(sine and active and self._source_used)
            self.form.labelForField(self.fields[key]).setEnabled(sine and active and self._source_used)
            bind(self.fields[key], 'setToolTip', '' if self._source_used else tr('Read-only operations generate no local waveform.'))
            self.form.labelForField(self.fields[key]).setVisible(sine)
        self.set_conflict(False)
        self.changed.emit()

    def retranslate_ui(self):
        retranslate_tree(self)

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
        text = (tr('Registers {p0}–{p1} · float32').format(p0=address, p1=address + REGISTERS_PER_POINT - 1)
                if self.enabled.isChecked() else tr('Disabled · No registers occupied'))
        bind(self.range_label, 'setText', text + (tr(' · Address overlap') if conflict else ''))
        self.range_label.setProperty("tone", "error" if conflict else "idle")
        refresh_style(self.range_label)

    def apply_point(self, point):
        self._loading = True
        self._point = point
        try:
            self.enabled.setChecked(point.enabled)
            bind(self.fields['name'], 'setText', str(point.name))
            valid_address = isinstance(point.address, int) and not isinstance(point.address, bool)
            self.fields["address"].setValue(point.address if valid_address else 0)
            kind = self.fields["source.kind"]
            bind(kind, 'setPlaceholderText', tr('This data source is not available'))
            kind.setCurrentIndex(kind.findData(point.source.kind))
            for key in ("value", "amplitude", "frequency_hz"):
                bind(self.fields['source.' + key], 'setText', str(getattr(point.source, key)))
        finally:
            self._loading = False
        self._changed()

    def configuration(self):
        """有效参数转换为数值；无关草稿保留，切换操作后再校验。"""
        active = self.enabled.isChecked()
        kind = self.fields["source.kind"].currentData()
        if kind is None:
            if active and self._source_used:
                raise ValueError(tr('Point {p0}: select a constant or sine source').format(p0=self.index + 1))
            kind = self._point.source.kind
        data = asdict(self._point)
        data.update(enabled=active, name=self.fields["name"].text().strip(),
                    address=self.fields["address"].value())
        if not active and not (isinstance(self._point.address, int) and not isinstance(self._point.address, bool)
                               and 0 <= self._point.address <= 65536 - REGISTERS_PER_POINT):
            data["address"] = self._point.address
        data["source"]["kind"] = kind
        for key, caption in (("value", tr('Value / offset')), ("amplitude", tr('Amplitude')), ("frequency_hz", tr('Frequency (Hz)'))):
            draft = self.fields["source." + key].text().strip()
            used = active and self._source_used and (key == "value" or kind == "sine")
            if not used:
                if draft != str(data["source"][key]):
                    data["source"][key] = draft
                continue
            try:
                data["source"][key] = float(draft)
            except ValueError:
                raise ValueError(tr('Point {p0} · {p1}: enter a valid number').format(p0=self.index + 1, p1=caption)) from None
        return data

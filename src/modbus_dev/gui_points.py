"""可复用的单测点编辑区；独立管理启用、地址、数据源和参数切换。"""
from dataclasses import asdict

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QHBoxLayout, QLineEdit,
    QVBoxLayout, QWidget,
)

from .domain.config import PointConfig, REGISTERS_PER_POINT
from .gui_theme import METRICS
from .gui_widgets import CheckBox, ComboBox, SpinBox, label, refresh_style


class PointEditor(QGroupBox):
    changed = Signal()

    def __init__(self, index, parent=None):
        super().__init__(f"测点 {index + 1}", parent)
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
        self.enabled = CheckBox("启用")
        self.enabled.setAccessibleName(f"启用测点 {index + 1}")
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
        self.fields["name"].setPlaceholderText("可选，例如温度、压力")
        self.fields["address"].setRange(0, 65536 - REGISTERS_PER_POINT)
        self.fields["address"].setToolTip("零基寄存器地址；每个 float32 测点占两个寄存器。")
        self.fields["source.kind"].addItem("常量", "constant")
        self.fields["source.kind"].addItem("正弦波", "sine")
        for key, caption in (("name", "名称"), ("address", "起始地址"), ("source.kind", "数据源"),
                             ("source.value", "数值"), ("source.amplitude", "振幅"),
                             ("source.frequency_hz", "频率 Hz")):
            widget = self.fields[key]
            widget.setObjectName(f"points.{index}.{key}")
            widget.setAccessibleName(f"测点 {index + 1} · {caption}")
            self.form.addRow(caption, widget)
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
            self.fields[key].setToolTip(self._source_tip if self._source_used else
                                        "纯读取不生成本机数据；接收值由对方从站提供。")
        self.form.labelForField(self.fields["source.value"]).setText("偏置" if sine else "数值")
        for key in ("source.amplitude", "source.frequency_hz"):
            self.fields[key].setVisible(sine)
            self.fields[key].setEnabled(sine and active and self._source_used)
            self.form.labelForField(self.fields[key]).setEnabled(sine and active and self._source_used)
            self.fields[key].setToolTip("" if self._source_used else "纯读取不生成本机波形。")
            self.form.labelForField(self.fields[key]).setVisible(sine)
        self.set_conflict(False)
        self.changed.emit()

    def set_usage(self, usage):
        self._source_used = usage.source
        if usage.master:
            self._source_tip = "生成主站写入对方保持区的数值。"
        elif usage.mode == "rtu-slave":
            self._source_tip = "数据源持续更新输入区，仅初始化一次保持区；后续源更新不会改变保持区。"
        else:
            self._source_tip = "更新供 TCP 客户端读取的寄存器快照。"
        caption = ("写入起始地址" if usage.writing else "读取起始地址") if usage.master else "本机起始地址"
        self.form.labelForField(self.fields["address"]).setText(caption)
        self.fields["address"].setAccessibleName(f"测点 {self.index + 1} · {caption}")
        self._changed()

    def set_conflict(self, conflict):
        address = self.fields["address"].value()
        text = (f"寄存器 {address}～{address + REGISTERS_PER_POINT - 1} · float32"
                if self.enabled.isChecked() else "未启用 · 不占用寄存器")
        self.range_label.setText(text + (" · 地址重叠" if conflict else ""))
        self.range_label.setProperty("tone", "error" if conflict else "idle")
        refresh_style(self.range_label)

    def apply_point(self, point):
        self._loading = True
        self._point = point
        try:
            self.enabled.setChecked(point.enabled)
            self.fields["name"].setText(str(point.name))
            valid_address = isinstance(point.address, int) and not isinstance(point.address, bool)
            self.fields["address"].setValue(point.address if valid_address else 0)
            kind = self.fields["source.kind"]
            kind.setPlaceholderText("此数据源暂不可用")
            kind.setCurrentIndex(kind.findData(point.source.kind))
            for key in ("value", "amplitude", "frequency_hz"):
                self.fields["source." + key].setText(str(getattr(point.source, key)))
        finally:
            self._loading = False
        self._changed()

    def configuration(self):
        """有效参数转换为数值；无关草稿保留，切换操作后再校验。"""
        active = self.enabled.isChecked()
        kind = self.fields["source.kind"].currentData()
        if kind is None:
            if active and self._source_used:
                raise ValueError(f"测点 {self.index + 1}：请选择常量或正弦数据源")
            kind = self._point.source.kind
        data = asdict(self._point)
        data.update(enabled=active, name=self.fields["name"].text().strip(),
                    address=self.fields["address"].value())
        if not active and not (isinstance(self._point.address, int) and not isinstance(self._point.address, bool)
                               and 0 <= self._point.address <= 65536 - REGISTERS_PER_POINT):
            data["address"] = self._point.address
        data["source"]["kind"] = kind
        for key, caption in (("value", "数值 / 偏置"), ("amplitude", "振幅"), ("frequency_hz", "频率 Hz")):
            draft = self.fields["source." + key].text().strip()
            used = active and self._source_used and (key == "value" or kind == "sine")
            if not used:
                if draft != str(data["source"][key]):
                    data["source"][key] = draft
                continue
            try:
                data["source"][key] = float(draft)
            except ValueError:
                raise ValueError(f"测点 {self.index + 1} · {caption}：请填写有效数字") from None
        return data

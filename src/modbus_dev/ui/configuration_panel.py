"""会话配置与逐点编码编辑，共用领域校验并保留无关参数草稿。"""
from .i18n import tr, bind, make_button, group_box, add_item, add_tab, retranslate_tree
import json
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QComboBox, QFormLayout, QFrame, QHBoxLayout, QLineEdit, QScrollArea, QTabWidget, QVBoxLayout, QWidget
from ..application.session import check_capabilities
from ..domain.config import MAX_POINTS, PointConfig, TaskConfig, point_conflicts
from ..domain.operations import OPERATIONS
from ..domain.numeric import numeric_type
from ..domain.usage import ParameterUsage
from .point_editor import PointEditor
from .theme import METRICS
from .widgets import CheckBox, ComboBox, SerialPortComboBox, label, refresh_style
from .texts import CHOICE_LABELS, SCHEDULE_LATE_HELP
from .form_state import FormState, programmatic_changes


class ConfigurationPanel(QFrame):
    changed = Signal()
    edited = Signal()
    loadRequested = Signal()
    saveRequested = Signal()
    validateRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fields = {}
        self._busy = False
        self.base_config = TaskConfig(mode="tcp-slave", duration_s=0).to_mapping()
        self._build_ui()
        self.fields["mode"].currentIndexChanged.connect(self.mode_changed)
        self.fields["access_mode"].currentIndexChanged.connect(self.operation_changed)
        self.fields["master_operation"].currentIndexChanged.connect(self.operation_changed)
        self.fields["readback_every"].textChanged.connect(self.operation_changed)
        for field in self.fields.values():
            signal = field.currentTextChanged if isinstance(field, QComboBox) else field.textChanged
            signal.connect(lambda *_: self.changed.emit())
        draft_fields = dict(self.fields, **{"serial.rts_toggle": self.rts})
        for index, editor in enumerate(self.point_editors):
            draft_fields[f"points.{index}.enabled"] = editor.enabled
            draft_fields.update({f"points.{index}.{key}": field for key, field in editor.fields.items()})
        self.form_state = FormState(draft_fields, self.edited.emit)

    def capture_draft(self):
        return dict(schema_version=1, base_config=self.base_config, values=self.form_state.capture())

    @programmatic_changes
    def restore_draft(self, draft):
        if not isinstance(draft, dict) or draft.get("schema_version") != 1:
            raise ValueError("Unsupported saved configuration draft")
        config = TaskConfig.from_mapping(draft["base_config"])
        check_capabilities(config)
        self.form_state.validate(draft["values"])
        self.apply_config(config)
        self.form_state.restore(draft["values"])
        self.mode_changed()
        self.update_points_summary()

    def set_busy(self, busy):
        self._busy = busy
        self.setEnabled(not busy)


    def _build_ui(self):
        self.editor = self
        self.setProperty("card", True)
        form_layout = QVBoxLayout(self)
        form_layout.setContentsMargins(*([METRICS.card_padding] * 4))
        form_layout.setSpacing(METRICS.section_spacing)
        self.editor.setMinimumWidth(374)
        form_layout.addWidget(label(tr('Session configuration'), "section"))
        description = label(tr('Configure the connection, shared timing and four independent points.'), "caption")
        description.setWordWrap(True)
        form_layout.addWidget(description)
        self.config_buttons = QWidget()
        config_actions = QHBoxLayout(self.config_buttons)
        config_actions.setContentsMargins(0, 0, 0, 0)
        config_actions.setSpacing(METRICS.spacing)
        for caption, callback in ((tr('Import Config'), self.loadRequested.emit), (tr('Export Config'), self.saveRequested.emit), (tr('Validate'), self.validateRequested.emit)):
            button = make_button(caption)
            button.clicked.connect(callback)
            config_actions.addWidget(button)
        form_layout.addWidget(self.config_buttons)
        self.config_tabs = QTabWidget()
        form_layout.addWidget(self.config_tabs, 1)

        def page(title):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            body = QWidget()
            body.setObjectName("configPage")
            layout = QVBoxLayout(body)
            layout.setContentsMargins(0, METRICS.section_spacing, METRICS.spacing, METRICS.spacing)
            layout.setSpacing(METRICS.page_spacing)
            scroll.setWidget(body)
            add_tab(self.config_tabs, scroll, title)
            return layout

        def group(layout, title, rows):
            box = group_box(title)
            form = QFormLayout(box)
            form.setContentsMargins(0, METRICS.section_spacing, 0, 0)
            form.setHorizontalSpacing(METRICS.section_spacing)
            form.setVerticalSpacing(METRICS.spacing)
            form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            for key, caption, choices in rows:
                widget = (SerialPortComboBox() if key == "serial.port" else
                          ComboBox() if choices else QLineEdit())
                if choices:
                    for value in choices:
                        add_item(widget, CHOICE_LABELS.get(value, value), value)
                if key == "serial.baud":
                    widget.setEditable(True)
                    widget.setInsertPolicy(QComboBox.NoInsert)
                    widget.setCompleter(None)
                    bind(widget.lineEdit(), 'setPlaceholderText', tr('Select or enter a baud rate'))
                    bind(widget, 'setToolTip', tr('Select a common baud rate or enter a custom value. Both the serial adapter and peer must support the same rate.'))
                widget.setObjectName(key)
                bind(widget, 'setAccessibleName', caption)
                self.fields[key] = widget
                form.addRow(label(caption), widget)
            layout.addWidget(box)
            return box

        connection = page(tr('Connection and timing'))
        group(connection, tr('Communication mode'), [("mode", tr('Mode'), ["tcp-slave", "rtu-slave", "rtu-master"]),
                                      ("policy", tr('Run policy'), ["continuous", "strict"])])
        self.access_box = group(connection, tr('RTU access'), [("access_mode", tr('Access'), ["read-only", "read-write"])])
        self.tcp_box = group(connection, tr('TCP listener'), [("host", tr('Bind address'), None), ("port", tr('Port'), None)])
        self.serial_box = group(connection, tr('Serial connection'), [("serial.port", tr('Serial port'), None),
                                                       ("serial.baud", tr('Baud rate'), ["1200", "2400", "4800", "9600", "19200", "38400",
                                                                              "57600", "115200"]),
                                                       ("serial.parity", tr('Parity'), ["N", "E", "O"]),
                                                       ("serial.stop_bits", tr('Stop bits'), ["1", "2"])])
        self.rts = CheckBox(tr('Driver RTS toggle (Windows only)'))
        self.serial_box.layout().addRow(self.rts)
        group(connection, tr('Device'), [("unit", tr('Unit ID'), None)])
        self.timing_box = group(connection, tr('Session timing'), [("period_ms", tr('Data period (ms)'), None), ("timeout_ms", tr('I/O timeout (ms)'), None),
                                ("duration_s", tr('Duration (s)'), None)])
        bind(self.fields['period_ms'], 'setToolTip', tr('Enabled points share one data clock; each point calculates its own value.'))
        bind(self.fields['timeout_ms'], 'setToolTip', tr('Master: whole-cycle transaction budget. Slave: maximum wait for one write, not the polling period.'))
        bind(self.fields['duration_s'], 'setToolTip', tr('0 keeps the session running until stopped manually.'))
        bind(self.fields['duration_s'], 'setPlaceholderText', tr('0 = run continuously'))
        self.master_box = group(connection, tr('Master operation and scheduling'), [("master_operation", tr('Cycle operation'), list(OPERATIONS)),
                                                      ("readback_every", tr('Readback every N cycles'), None),
                                                      ("read_function", tr('Readback function'), ["3", "4"]),
                                                      ("schedule_tolerance_percent", tr('Scheduling tolerance (%)'), None)])
        bind(self.fields['readback_every'], 'setToolTip', tr('Read back every N cycles; 0 disables readback.'))
        bind(self.fields['schedule_tolerance_percent'], 'setToolTip', SCHEDULE_LATE_HELP)
        bind(self.fields['master_operation'], 'setToolTip', tr('Reads send no writes. FC06 writes one register per transaction; 32-bit points are split for raw-register diagnostics, not atomic updates. FC16 writes complete groups.'))
        connection.addStretch()

        data = page(tr('Data'))
        group(data, tr('Default 32-bit encoding'), [("byte_order", tr('Default byte order'), list(numeric_type("float32").orders))])
        self.data_note = label("", "caption")
        self.data_note.setWordWrap(True)
        data.addWidget(self.data_note)
        self.points_status = label("", "caption")
        self.points_status.setWordWrap(True)
        data.addWidget(self.points_status)
        # 按统一模型创建四个编辑区，不再用逗号字符串表达多个测点。
        self.point_editors = [PointEditor(i) for i in range(MAX_POINTS)]
        for editor in self.point_editors:
            data.addWidget(editor)
            editor.changed.connect(self.update_points_summary)
        data.addStretch()

        other = page(tr('Other settings'))
        interface = group(other, tr("Interface"), [])
        self.language = ComboBox()
        self.language.setObjectName("language")
        bind(self.language, "setAccessibleName", tr("Display language"))
        add_item(self.language, tr("Follow system"), "system")
        add_item(self.language, "简体中文", "zh_CN")
        add_item(self.language, "English", "en")
        bind(self.language, "setToolTip", tr("Stop communication before changing settings."))
        interface.layout().addRow(label(tr("Display language")), self.language)
        retention = group(other, tr('Log retention'), [("logging.max_files", tr('Maximum log segments'), None)])
        bind(self.fields['logging.max_files'], 'setToolTip', tr('1 to 10000 files, including the current file. Each is at most 32 MiB; the oldest is removed when the limit is exceeded.'))
        retention_note = label(tr('Each log segment is at most 32 MiB. Only the newest segments are retained; removed history marks the evidence as incomplete.'), "caption")
        retention_note.setWordWrap(True)
        retention.layout().addRow(retention_note)
        other.addStretch()


    def text(self, key):
        field = self.fields[key]
        if isinstance(field, QComboBox) and field.isEditable():
            return field.currentText().strip()
        return str(field.currentData()) if isinstance(field, QComboBox) else field.text().strip()

    def retranslate_ui(self):
        retranslate_tree(self)
        for editor in self.point_editors:
            editor.retranslate_ui()
        self.update_points_summary()


    @programmatic_changes
    def apply_config(self, config):
        if self._busy:
            return False
        config = TaskConfig.from_mapping(config.to_mapping())
        check_capabilities(config)
        self.base_config = config.to_mapping()
        for key, field in self.fields.items():
            value = self.base_config
            for part in key.split("."):
                value = value[part]
            text = str(value)
            if isinstance(field, QComboBox) and field.isEditable():
                field.setCurrentIndex(field.findText(text, Qt.MatchExactly))
                field.setEditText(text)
            elif isinstance(field, QComboBox):
                field.setCurrentIndex(field.findData(text))
            else:
                bind(field, 'setText', text)
        self.rts.setChecked(config.serial.rts_toggle is True)
        for editor, point in zip(self.point_editors, config.points):
            editor.apply_point(point)
        self.mode_changed()
        self.operation_changed()


    def configuration(self):
        data = json.loads(json.dumps(self.base_config))
        usage = self.parameter_usage()
        integers = {"unit", "port", "period_ms", "timeout_ms", "readback_every", "read_function",
                    "serial.baud", "serial.stop_bits", "logging.max_files"}
        floats = {"duration_s", "schedule_tolerance_percent"}
        for key in self.fields:
            value = self.text(key)
            if not usage.field_active(key):
                original = self.base_config
                for part in key.split("."):
                    original = original[part]
                field = self.fields[key]
                if value == str(original) or (isinstance(field, QComboBox) and not field.isEditable() and field.currentIndex() < 0):
                    value = original
                if "." in key:
                    parent, name = key.split(".")
                    data[parent][name] = value
                else:
                    data[key] = value
                continue
            try:
                if key in integers:
                    value = int(value)
                elif key in floats:
                    value = float(value)
            except ValueError:
                raise ValueError(tr('{p0}: enter a valid number').format(p0=self.fields[key].accessibleName())) from None
            if "." in key:
                parent, name = key.split(".")
                data[parent][name] = value
            else:
                data[key] = value
        if usage.field_active("serial.rts_toggle"):
            data["serial"]["rts_toggle"] = self.rts.isChecked()
        data["points"] = [editor.configuration() for editor in self.point_editors]
        config = TaskConfig.from_mapping(data)
        check_capabilities(config)
        return config


    def parameter_usage(self):
        try:
            readback = int(self.text("readback_every"))
        except ValueError:
            readback = 0
        return ParameterUsage(self.text("mode"), self.text("master_operation"), readback)


    def mode_changed(self, *_):
        mode = self.text("mode")
        self.tcp_box.setVisible(mode == "tcp-slave")
        self.tcp_box.setEnabled(mode == "tcp-slave")
        self.serial_box.setVisible(mode != "tcp-slave")
        self.serial_box.setEnabled(mode != "tcp-slave")
        self.access_box.setVisible(mode != "tcp-slave")
        self.access_box.setEnabled(mode != "tcp-slave")
        self.master_box.setVisible(mode == "rtu-master")
        self.master_box.setEnabled(mode == "rtu-master")
        self.operation_changed()


    @programmatic_changes
    def operation_changed(self, *_):
        mode = self.text("mode")
        readonly = self.text("access_mode") == "read-only"
        operation = self.fields["master_operation"]
        for index in range(operation.count()):
            operation.model().item(index).setEnabled(not readonly or OPERATIONS[operation.itemData(index)] in (3, 4))
        if mode == "rtu-master" and readonly and self.text("master_operation") in ("write-single", "write-multiple"):
            operation.setCurrentIndex(operation.findData("read-input"))
        usage = self.parameter_usage()
        for key, widget in self.fields.items():
            widget.setEnabled(usage.field_active(key))
        self.fields["read_function"].setEnabled(False)
        if usage.readback:
            self.fields["read_function"].setCurrentIndex(self.fields["read_function"].findData("3"))
        bind(self.fields['read_function'], 'setToolTip', tr('Write readback uses FC03 and applies only when readback is enabled.'))
        bind(self.fields['readback_every'], 'setToolTip', tr('Read back with FC03 every N cycles; 0 disables readback.') if usage.writing else tr('Read-only operations and slave modes do not perform write readback.'))
        self.rts.setEnabled(usage.field_active("serial.rts_toggle"))
        bind(self.rts, 'setToolTip', tr('Supported by Windows serial drivers only; ignored on other platforms.'))
        for editor in self.point_editors:
            editor.set_usage(usage)
        timing = self.timing_box.layout()
        bind(timing.labelForField(self.fields['period_ms']), 'setText', tr('Polling period (ms)') if usage.master else tr('Data update period (ms)'))
        bind(self.fields['period_ms'], 'setAccessibleName', tr('Polling period (ms)') if usage.master else tr('Data update period (ms)'))
        timeout_caption = tr('Cycle budget (ms)') if usage.master else tr('Reply write timeout (ms)') if usage.serial else tr('Partial request timeout (ms)')
        bind(timing.labelForField(self.fields['timeout_ms']), 'setText', timeout_caption)
        bind(self.fields['timeout_ms'], 'setAccessibleName', timeout_caption)
        bind(self.fields['period_ms'], 'setToolTip', tr('Send read or write requests on an absolute periodic schedule.') if usage.master else tr('Update the data source on an independent clock, regardless of master polling.'))
        bind(self.fields['timeout_ms'], 'setToolTip', tr('Budget for the entire cycle, including the silent interval and all requests.') if usage.master else tr('Wait for one reply write, capped at 50 ms.') if usage.serial else tr('Fixed deadline for receiving a partial request, at least 1000 ms.'))
        if usage.master:
            bind(self.data_note, 'setText', tr('Set the remote point addresses and byte order. The local source generates values to write.') if usage.writing else tr('Set the remote point addresses and byte order. Values come from the slave; the local source is unused.'))
        elif mode == "rtu-slave":
            bind(self.data_note, 'setText', tr('The source updates input registers continuously and initializes holding registers only at session start. Master writes are retained.'))
        else:
            bind(self.data_note, 'setText', tr('The source updates a read-only TCP snapshot for clients to read with FC03/04.'))
        self.changed.emit()

    def update_points_summary(self):
        # 即时检查使用实际类型宽度，不解析用户尚未输入完毕的数值草稿。
        points = [PointConfig(enabled=editor.enabled.isChecked(), address=editor.fields["address"].value(),
                              data_type=editor.current_data_type())
                  for editor in self.point_editors]
        conflicts = point_conflicts(points)
        overflow = any(point.enabled and point.address + point.register_count > 65536 for point in points)
        overlapping = {index for pair in conflicts for index in pair}
        for i, editor in enumerate(self.point_editors):
            editor.set_conflict(i in overlapping)
        count = sum(point.enabled for point in points)
        if conflicts:
            text = "；".join(tr('Point {p0} overlaps point {p1}').format(p0=a + 1, p1=b + 1) for a, b in conflicts)
        else:
            text = tr('{p0} / {p1} points enabled').format(p0=count, p1=MAX_POINTS) if count else tr('Enable at least one point')
        if overflow:
            text += tr(' · Address range exceeds 65535')
        bind(self.points_status, 'setText', text)
        self.points_status.setProperty("tone", "error" if conflicts or overflow or not count else "idle")
        refresh_style(self.points_status)

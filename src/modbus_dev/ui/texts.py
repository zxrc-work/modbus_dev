"""Stable protocol keys mapped to presentation text."""
from .i18n import tr
CHOICE_LABELS = {
    "tcp-slave": tr('TCP read-only slave'), "rtu-slave": tr('RTU slave'), "rtu-master": tr('RTU master'),
    "continuous": tr('Continuous'), "strict": tr('Strict'), "constant": tr('Constant'), "sine": tr('Sine wave'),
    "read-only": tr('Read-only'), "read-write": tr('Read/write'),
    "read-holding": tr('FC03 · Read holding registers'), "read-input": tr('FC04 · Read input registers'),
    "write-single": tr('FC06 · Write single registers'), "write-multiple": tr('FC16 · Write multiple registers'),
}
COUNTER_LABELS = {
    "source_updates": tr('Data updates'), "slave_request": tr('Slave requests'), "rx": tr('Receive calls'), "tx": tr('Transmit calls'),
    "listener_opens": tr('Listener opens'), "port_opens": tr('Port opens'), "open_errors": tr('Open failures'),
    "io_errors": tr('I/O errors'), "write_attempts": tr('Requests sent'), "ack": tr('Regular ACKs'), "read": tr('Regular read replies'),
    "valid_ack": tr('Valid ACKs'), "valid_read": tr('Valid read replies'),
    "ack_ambiguous": tr('Uncertain ACK correlations'), "read_ambiguous": tr('Uncertain read correlations'),
    "exception": tr('Protocol exceptions'), "exception_ambiguous": tr('Uncertain exception correlations'), "timeout": tr('Reply timeout'),
    "schedule_late": tr('Late cycles'), "schedule_missed": tr('Missed transaction budgets'), "skipped_slots": tr('Skipped cycles'),
    "port_unavailable": tr('Port unavailable'), "readback_mismatch": tr('Readback mismatch'),
}
SCHEDULE_LATE_HELP = (tr('Count adjacent cycle-first send intervals only when they exceed the period plus tolerance; equality and the first send are excluded. For a 100 ms period with 10% tolerance, count intervals above 110 ms. Groups and readbacks within one cycle are not counted again. Planned offsets remain in the detailed logs and session summary.'))
VALID_REPLY_HELP = tr('Count replies accepted within the transaction budget after frame and request-field checks, including uncertain correlations. This does not establish unique per-request correlation or device source quality.')
METRIC_HELP = {"schedule_late": SCHEDULE_LATE_HELP, "valid_ack": VALID_REPLY_HELP, "valid_read": VALID_REPLY_HELP}

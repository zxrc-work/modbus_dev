"""Localize structured configuration failures without changing CLI diagnostics."""
from ..domain.events import ConfigurationError
from .i18n import tr

TEMPLATES = {
    "must be an integer": tr("Enter an integer."),
    "must be a number": tr("Enter a number."),
    "numeric_range": tr("Enter a finite value from {low} to {high}."),
    "invalid_choice": tr("Choose one of: {choices}."),
    "must be a nonempty string": tr("Enter a non-empty value."),
    "must be an object": tr("An object is required."),
    "unknown_fields": tr("Unknown fields: {fields}."),
    "must be a boolean": tr("A boolean value is required."),
    "must be an array of strings": tr("A list of strings is required."),
    "must be a string": tr("A string is required."),
    "must be positive and below output Nyquist frequency": tr("Frequency must be positive and below the output Nyquist frequency."),
    "signal exceeds finite float32 range": tr("The signal exceeds the finite float32 range."),
    "provide one unique channel per point": tr("Provide one unique channel per point."),
    "independent points require schema_version=2": tr("Independent points require schema version 2."),
    "schema_version=2 uses points instead of addresses/source": tr("Schema version 2 uses points instead of addresses/source."),
    "address_count": tr("Provide 1 to {maximum} float32 point addresses."),
    "point_count": tr("Provide 1 to {maximum} point configurations."),
    "readonly_write": tr("A read-only master cannot perform writes."),
    "no_enabled_points": tr("Enable at least one point"),
    "point_overlap": tr("The register ranges of point {first} and point {second} overlap."),
    "transaction_budget": tr("The timeout must exceed the {minimum:.3f} ms transaction budget and leave {gap:.3f} ms of silence before the next cycle."),
    "duplicate_field": tr("Duplicate configuration field: {field}."),
    "invalid_json": tr("Invalid JSON at line {line}, column {column}."),
}

FIELD_NAMES = {
    "serial.port": tr("Serial port"), "serial.baud": tr("Baud rate"), "serial.parity": tr("Parity"),
    "serial.stop_bits": tr("Stop bits"), "unit": tr("Unit ID"), "host": tr("Bind address"), "port": tr("Port"),
    "period_ms": tr("Data period (ms)"), "timeout_ms": tr("I/O timeout (ms)"), "duration_s": tr("Duration (s)"),
    "points": tr("Points"), "master_operation": tr("Cycle operation"), "byte_order": tr("Byte order"),
    "logging.max_files": tr("Maximum log segments"),
}


def format_error(error):
    if isinstance(error, ConfigurationError):
        source = TEMPLATES.get(error.reason)
        if source is not None:
            return tr("{field}: {message}").format(
                field=FIELD_NAMES.get(error.path, error.path), message=source.format(**error.parameters))
    if str(error) == "This build runs constant/sine sources only; offline sources are deferred until after GUI":
        return tr("This build supports constant and sine sources only; offline sources are not available.")
    return str(error)

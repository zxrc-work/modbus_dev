"""Stable desktop entry points; presentation lives in modbus_dev.ui."""
import sys
from .ui.app import create_application, main
from .ui.main_window import Workbench

__all__ = ["Workbench", "create_application", "main"]

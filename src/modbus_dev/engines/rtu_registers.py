"""保留 RTU 寄存器适配器入口，协议行为委托公共独立寄存器区。"""
from .register_banks import RegisterBanks


class RtuRegisters(RegisterBanks):
    pass

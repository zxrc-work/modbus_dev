"""Independent TCP protocol and 100ms polling checks against an explicit endpoint."""
import argparse
import json
from pathlib import Path
import struct
import time

from verify_backend_communications import TcpWire, check_protocol, read_pdu, require, save

parser = argparse.ArgumentParser()
parser.add_argument('--host', required=True)
parser.add_argument('--port', type=int, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
trace = []
wire = None
report = dict(host=args.host, port=args.port)
try:
    wire = TcpWire(args.host, args.port, trace)
    report['protocol_checks'] = check_protocol(wire, trace)
    times = []
    origin = time.perf_counter() + .1
    for slot in range(100):
        time.sleep(max(0, origin + slot*.1 - time.perf_counter()))
        before = time.perf_counter()
        require(wire.exchange(read_pdu()) == b'\x03\x10' + struct.pack('>ffff', *([12.5]*4)),
                'Network snapshot mismatch')
        times.append((time.perf_counter() - before) * 1000)
    report.update(status='passed', polling_reads=len(times), response_mean_ms=sum(times)/len(times),
                  response_max_ms=max(times), response_p95_ms=sorted(times)[94])
except Exception as exc:
    report.update(status='failed', error=repr(exc))
    raise
finally:
    if wire:
        wire.close()
    save(args.output/'trace.json', trace)
    save(args.output/'report.json', report)
    print(json.dumps(report), flush=True)

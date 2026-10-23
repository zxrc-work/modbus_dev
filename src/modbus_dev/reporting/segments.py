"""日志分片写入与有界保留；磁盘操作仅由日志线程执行，元数据锁不包围 I/O。"""
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import threading


MAX_SEGMENT_BYTES = 32 * 1024 * 1024
_SEGMENT_PATTERN = re.compile(r"events-(\d+)\.jsonl")


def event_paths(directory):
    """按分片序号读取新旧格式；即使首片已淘汰，也能发现当前保留的文件。"""
    indexed = []
    for path in Path(directory).glob("events*.jsonl"):
        match = _SEGMENT_PATTERN.fullmatch(path.name)
        if path.name == "events.jsonl":
            indexed.append((1, path))
        elif match and int(match[1]) >= 2:
            indexed.append((int(match[1]), path))
    return [path for _, path in sorted(indexed)]


@dataclass
class Segment:
    name: str
    bytes: int = 0
    records: int = 0
    first_sequence: int | None = None
    last_sequence: int | None = None


class SegmentedEventWriter:
    def __init__(self, directory, *, max_bytes, max_files, opener=None):
        if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                or not 1 <= max_bytes <= MAX_SEGMENT_BYTES):
            raise ValueError("Invalid log segment size")
        if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
            raise ValueError("Invalid retained log file count")
        self.directory = Path(directory)
        self.max_bytes, self.max_files = max_bytes, max_files
        self._opener = opener or (lambda path: path.open("xb"))
        self._lock = threading.Lock()
        self._segments = deque()
        self._opened = self._retained_bytes = 0
        self._deleted_files = self._deleted_records = 0
        self._first_deleted_sequence = self._last_deleted_sequence = None
        self._output = None
        self._open_next()

    def _open_next(self):
        index = self._opened + 1
        name = "events.jsonl" if index == 1 else f"events-{index:06d}.jsonl"
        # 独占新建且序号不复用，不能覆盖已有取证文件。
        self._output = self._opener(self.directory / name)
        with self._lock:
            self._segments.append(Segment(name))
            self._opened = index

    def _close_output(self):
        output, self._output = self._output, None
        if output is not None:
            try:
                output.flush()
            finally:
                output.close()

    def _rotate(self):
        self._close_output()
        # 先淘汰再建新片，保证磁盘上文件数量也不瞬时超过配置上限。
        while len(self._segments) >= self.max_files:
            oldest = self._segments[0]
            (self.directory / oldest.name).unlink()
            with self._lock:
                self._segments.popleft()
                self._retained_bytes -= oldest.bytes
                self._deleted_files += 1
                self._deleted_records += oldest.records
                if self._first_deleted_sequence is None:
                    self._first_deleted_sequence = oldest.first_sequence
                if oldest.last_sequence is not None:
                    self._last_deleted_sequence = oldest.last_sequence
        self._open_next()

    def write(self, encoded, *, sequence=None):
        # 一行必须完整落在一个分片中；先检查长度，避免为不可能写入的记录删历史。
        if len(encoded) > self.max_bytes:
            raise OSError("Event exceeds log segment size limit")
        if self._segments[-1].bytes + len(encoded) > self.max_bytes:
            self._rotate()
        size = self._output.write(encoded)
        if size != len(encoded):
            raise OSError("Short event log write")
        with self._lock:
            segment = self._segments[-1]
            segment.bytes += size
            segment.records += 1
            if sequence is not None:
                if segment.first_sequence is None:
                    segment.first_sequence = sequence
                segment.last_sequence = sequence
            self._retained_bytes += size

    def flush(self):
        if self._output is not None:
            self._output.flush()

    def close(self):
        self._close_output()

    def snapshot(self, *, include_files=False):
        with self._lock:
            result = dict(max_bytes=self.max_bytes, max_files=self.max_files,
                          retained_files=len(self._segments), retained_bytes=self._retained_bytes,
                          rotations=self._opened - 1, deleted_files=self._deleted_files,
                          deleted_records=self._deleted_records,
                          first_deleted_sequence=self._first_deleted_sequence,
                          last_deleted_sequence=self._last_deleted_sequence)
            # 实时 worker 状态只传固定大小统计，完整索引仅写最终摘要。
            if include_files:
                result["files"] = [asdict(segment) for segment in self._segments]
            return result

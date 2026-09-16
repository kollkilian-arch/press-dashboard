"""Read Linux container memory accounting without loading a monitoring library."""
from pathlib import Path


def container_memory():
    for current_path, limit_path in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            used = int(Path(current_path).read_text().strip())
            limit = int(Path(limit_path).read_text().strip())
            # v1 uses an enormous sentinel and v2 uses 'max' for no limit.
            if 0 < limit < 2**60:
                return {"used_bytes": used, "limit_bytes": limit}
        except (OSError, ValueError):
            continue
    return None


def indexing_should_pause(memory, *, already_paused=False):
    if memory is None:
        return False
    # Reserve headroom for normal page requests. Hysteresis avoids flapping.
    threshold = 0.60 if already_paused else 0.70
    return memory["used_bytes"] >= memory["limit_bytes"] * threshold

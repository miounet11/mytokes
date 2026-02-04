import time
from typing import Dict, Any, Optional

class TTLCache:
    """简单的带 TTL 和容量限制的缓存"""
    def __init__(self, maxsize: int = 1000, ttl: int = 3600):
        self.maxsize = maxsize
        self.ttl = ttl
        self.cache: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        if key not in self.cache:
            return None
        
        entry = self.cache[key]
        if time.time() - entry["timestamp"] > self.ttl:
            del self.cache[key]
            return None
        
        return entry["value"]

    def set(self, key: str, value: Any):
        # 清理过期条目或超出容量
        self._cleanup()
        
        if len(self.cache) >= self.maxsize and key not in self.cache:
            # 简单策略：删除最早的一个
            oldest_key = min(self.cache.keys(), key=lambda k: self.cache[k]["timestamp"])
            del self.cache[oldest_key]
            
        self.cache[key] = {
            "value": value,
            "timestamp": time.time()
        }

    def _cleanup(self):
        now = time.time()
        expired_keys = [k for k, v in self.cache.items() if now - v["timestamp"] > self.ttl]
        for k in expired_keys:
            del self.cache[k]

    def __len__(self):
        self._cleanup()
        return len(self.cache)

    def __contains__(self, key):
        return self.get(key) is not None

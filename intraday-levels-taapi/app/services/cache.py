import time
from typing import Any, Dict, Tuple, Optional, List
import logging

logger = logging.getLogger(__name__)

class TTLCache:
    def __init__(self, ttl_seconds: int = 60):
        self.ttl = ttl_seconds
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        """Получение значения из кэша"""
        item = self._store.get(key)
        if not item:
            return None
        
        exp, val = item
        if time.time() > exp:
            self._store.pop(key, None)
            logger.debug(f"Cache expired for key: {key}")
            return None
        
        logger.debug(f"Cache hit for key: {key}")
        return val

    def set(self, key: str, value: Any) -> None:
        """Установка значения в кэш"""
        self._store[key] = (time.time() + self.ttl, value)
        logger.debug(f"Cache set for key: {key}")

    def clear(self) -> None:
        """Очистка всего кэша"""
        self._store.clear()
        logger.info("Cache cleared")

    def delete(self, key: str) -> bool:
        """Удаление конкретного ключа"""
        if key in self._store:
            del self._store[key]
            logger.debug(f"Cache deleted for key: {key}")
            return True
        return False

    def exists(self, key: str) -> bool:
        """Проверка существования ключа"""
        item = self._store.get(key)
        if not item:
            return False
        
        exp, _ = item
        if time.time() > exp:
            self._store.pop(key, None)
            return False
        
        return True

    def keys(self) -> List[str]:
        """Получение всех ключей (с очисткой истекших)"""
        current_time = time.time()
        expired_keys = []
        
        for key, (exp, _) in self._store.items():
            if current_time > exp:
                expired_keys.append(key)
        
        # Удаляем истекшие ключи
        for key in expired_keys:
            self._store.pop(key, None)
        
        return list(self._store.keys())

    def size(self) -> int:
        """Получение размера кэша (с очисткой истекших)"""
        self._cleanup_expired()
        return len(self._store)

    def _cleanup_expired(self) -> None:
        """Очистка истекших записей"""
        current_time = time.time()
        expired_keys = [
            key for key, (exp, _) in self._store.items() 
            if current_time > exp
        ]
        
        for key in expired_keys:
            self._store.pop(key, None)
        
        if expired_keys:
            logger.debug(f"Cleaned up {len(expired_keys)} expired cache entries")

    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики кэша"""
        self._cleanup_expired()
        return {
            "size": len(self._store),
            "ttl_seconds": self.ttl,
            "keys": list(self._store.keys())
        }

    def update_ttl(self, new_ttl: int) -> None:
        """Обновление TTL для кэша"""
        self.ttl = new_ttl
        logger.info(f"Cache TTL updated to {new_ttl} seconds")

    def get_with_ttl(self, key: str) -> Optional[Tuple[Any, float]]:
        """Получение значения с оставшимся TTL"""
        item = self._store.get(key)
        if not item:
            return None
        
        exp, val = item
        remaining_ttl = exp - time.time()
        
        if remaining_ttl <= 0:
            self._store.pop(key, None)
            return None
        
        return val, remaining_ttl 
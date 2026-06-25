"""
Caching module for ByMeVPN bot performance optimization.
Uses TTL cache for frequently accessed data.
"""
import logging
from typing import Dict, Any
from cachetools import TTLCache
from functools import wraps

# Cache TTL settings - optimized for speed
CACHE_CONFIG = {
    'user_cache_size': 5000,    # Increased for better hit rate
    'user_cache_ttl': 5,        # Reduced for faster invalidation
    'subscription_cache_size': 2000,  # New cache for subscription data
    'subscription_cache_ttl': 3,     # Very fast subscription checks
}

logger = logging.getLogger(__name__)

# Global caches
_user_cache = TTLCache(maxsize=CACHE_CONFIG['user_cache_size'], ttl=CACHE_CONFIG['user_cache_ttl'])
_subscription_cache = TTLCache(maxsize=CACHE_CONFIG['subscription_cache_size'], ttl=CACHE_CONFIG['subscription_cache_ttl'])

def cache_user_info(func):
    """Decorator for caching user information."""
    @wraps(func)
    async def wrapper(user_id: int, *args, **kwargs):
        cache_key = f"user_{user_id}"
        
        # Try to get from cache first
        if cache_key in _user_cache:
            logger.debug(f"Cache hit for user {user_id}")
            return _user_cache[cache_key]
        
        # Cache miss - call the function
        result = await func(user_id, *args, **kwargs)
        
        # Store in cache
        _user_cache[cache_key] = result
        logger.debug(f"Cached data for user {user_id}")
        
        return result
    return wrapper


def invalidate_user_cache(user_id: int):
    """Invalidate cache for specific user."""
    cache_key = f"user_{user_id}"
    if cache_key in _user_cache:
        del _user_cache[cache_key]
        logger.debug(f"Invalidated cache for user {user_id}")


def cache_subscription_data(func):
    """Decorator for caching subscription data."""
    @wraps(func)
    async def wrapper(user_id: int, *args, **kwargs):
        cache_key = f"sub_{user_id}"
        
        # Try to get from cache first
        if cache_key in _subscription_cache:
            logger.debug(f"Subscription cache hit for user {user_id}")
            return _subscription_cache[cache_key]
        
        # Cache miss - call function
        result = await func(user_id, *args, **kwargs)
        
        # Store in cache
        if result is not None:  # Don't cache None results
            _subscription_cache[cache_key] = result
            logger.debug(f"Cached subscription data for user {user_id}")
        
        return result
    return wrapper


def invalidate_subscription_cache(user_id: int) -> None:
    """Invalidate subscription cache for user."""
    cache_key = f"sub_{user_id}"
    if cache_key in _subscription_cache:
        del _subscription_cache[cache_key]
        logger.debug(f"Invalidated subscription cache for user {user_id}")


def get_cache_stats() -> Dict[str, Any]:
    """Get cache statistics for monitoring."""
    total_size = len(_user_cache) + len(_subscription_cache)
    return {
        "size": total_size,
        "hits": 0,  # TODO: implement hit/miss tracking
        "misses": 0,
        "user_cache_size": len(_user_cache),
        "user_cache_maxsize": CACHE_CONFIG['user_cache_size'],
        "subscription_cache_size": len(_subscription_cache),
        "subscription_cache_maxsize": CACHE_CONFIG['subscription_cache_size'],
    }


def clear_cache() -> None:
    """Clear all caches. Used by admin panel."""
    global _user_cache, _subscription_cache
    _user_cache.clear()
    _subscription_cache.clear()
    logger.info("All caches cleared by admin")

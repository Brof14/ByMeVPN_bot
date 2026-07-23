"""
Caching module for ByMeVPN bot performance optimization.
Uses TTL cache for frequently accessed data.
"""
import logging
from typing import Dict, Any
from cachetools import TTLCache
from functools import wraps

# Cache TTL settings - optimized for speed and reduced API load
CACHE_CONFIG = {
    'user_cache_size': 10000,   # Increased for better hit rate
    'user_cache_ttl': 30,        # Increased to reduce API calls (30s)
    'subscription_cache_size': 5000,  # Increased cache for subscription data
    'subscription_cache_ttl': 15,     # Increased to reduce API calls (15s)
}

logger = logging.getLogger(__name__)

# Global caches
_user_cache = TTLCache(maxsize=CACHE_CONFIG['user_cache_size'], ttl=CACHE_CONFIG['user_cache_ttl'])
_subscription_cache = TTLCache(maxsize=CACHE_CONFIG['subscription_cache_size'], ttl=CACHE_CONFIG['subscription_cache_ttl'])

def cache_user_info(func):
    """
    Decorator for caching user information.

    IMPORTANT: the cache key includes the wrapped function's name. Previously
    the key was just f"user_{user_id}" for every decorated function, which
    meant has_trial_used(), has_active_subscription(), has_ever_had_key(),
    get_user_active_keys() and has_paid_subscription() all shared ONE cache
    slot per user — whichever ran first would silently overwrite the cached
    value for all the others (a bool result could even be returned in place
    of a list). This caused users to see the wrong subscription/key status.
    """
    @wraps(func)
    async def wrapper(user_id: int, *args, **kwargs):
        cache_key = f"user_{func.__name__}_{user_id}"

        # Try to get from cache first
        if cache_key in _user_cache:
            logger.debug(f"Cache hit for {func.__name__} user {user_id}")
            return _user_cache[cache_key]

        # Cache miss - call the function
        result = await func(user_id, *args, **kwargs)

        # Store in cache
        _user_cache[cache_key] = result
        logger.debug(f"Cached data for {func.__name__} user {user_id}")

        return result
    return wrapper


def invalidate_user_cache(user_id: int):
    """
    Invalidate cache for specific user across ALL functions decorated with
    @cache_user_info (has_trial_used, has_active_subscription, etc).
    Keys are now namespaced as f"user_{func_name}_{user_id}", so we must
    scan and remove every matching suffix rather than a single fixed key.
    """
    suffix = f"_{user_id}"
    keys_to_delete = [
        k for k in list(_user_cache.keys())
        if k.startswith("user_") and k.endswith(suffix)
    ]
    for k in keys_to_delete:
        try:
            del _user_cache[k]
        except KeyError:
            pass  # already evicted by TTL between listing and deleting
    if keys_to_delete:
        logger.debug(f"Invalidated {len(keys_to_delete)} cache entries for user {user_id}")


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

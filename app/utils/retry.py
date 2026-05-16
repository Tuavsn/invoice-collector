"""
Retry utilities — sync decorator and async decorator with exponential back-off.
"""
from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Callable, Tuple, Type

from loguru import logger


def retry_sync(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    reraise: bool = True,
) -> Callable:
    """Synchronous retry decorator with exponential back-off."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            wait = delay
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.error(
                            "{}() failed after {} attempts: {}",
                            func.__name__,
                            max_attempts,
                            exc,
                        )
                        if reraise:
                            raise
                        return None
                    logger.warning(
                        "{}() attempt {}/{} failed: {}. Retrying in {:.1f}s…",
                        func.__name__,
                        attempt,
                        max_attempts,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                    wait *= backoff

        return wrapper

    return decorator


def retry_async(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    reraise: bool = True,
) -> Callable:
    """Async retry decorator with exponential back-off."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            wait = delay
            while attempt < max_attempts:
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.error(
                            "{}() failed after {} attempts: {}",
                            func.__name__,
                            max_attempts,
                            exc,
                        )
                        if reraise:
                            raise
                        return None
                    logger.warning(
                        "{}() attempt {}/{} failed: {}. Retrying in {:.1f}s…",
                        func.__name__,
                        attempt,
                        max_attempts,
                        exc,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    wait *= backoff

        return wrapper

    return decorator
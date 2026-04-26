import asyncio
import random


async def random_delay(min_s: float, max_s: float) -> None:
    """Sleep a uniformly random duration between min_s and max_s seconds."""
    await asyncio.sleep(random.uniform(min_s, max_s))

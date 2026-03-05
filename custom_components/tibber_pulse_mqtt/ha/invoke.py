from __future__ import annotations

import inspect
from homeassistant.core import HomeAssistant


def call_sm_threadsafe(hass: HomeAssistant, func, *args):
    """Invoke a SensorManager method from a NON-LOOP context (e.g., threadpool).
    Supports both sync and async methods."""
    if not func:
        return
    if inspect.iscoroutinefunction(func):
        hass.loop.call_soon_threadsafe(hass.async_create_task, func(*args))
    else:
        hass.loop.call_soon_threadsafe(func, *args)


def call_sm_on_loop(hass: HomeAssistant, func, *args):
    """Invoke a SensorManager method from the HA event loop.
    Supports both sync and async methods."""
    if not func:
        return
    if inspect.iscoroutinefunction(func):
        hass.async_create_task(func(*args))
    else:
        hass.loop.call_soon(func, *args)
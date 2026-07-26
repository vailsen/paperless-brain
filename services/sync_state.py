# services/sync_state.py
# Shared mutable flag — True while a sync job is running.
# Use a list so it can be mutated across module boundaries without rebinding.
is_running: list[bool] = [False]

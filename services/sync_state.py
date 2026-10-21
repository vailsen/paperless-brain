# services/sync_state.py
# Shared mutable flag — True while a sync job is running.
# Use a list so it can be mutated across module boundaries without rebinding.
is_running: list[bool] = [False]

# Set by the stop button; the sync loop checks it between documents, so the
# document being read is finished, never left half-ingested.
stop_requested: list[bool] = [False]

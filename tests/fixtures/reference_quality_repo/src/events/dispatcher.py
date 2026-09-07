class PluginEventDispatcher:
    def __init__(self):
        self._callback_registry = {}

    def register_callback(self, event_name, callback):
        self._callback_registry.setdefault(event_name, []).append(callback)

    def dispatch_event(self, event_name, payload):
        for callback in self._callback_registry.get(event_name, []):
            callback(payload)

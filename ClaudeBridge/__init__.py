from .claude_bridge import ClaudeBridge


def create_instance(c_instance):
    """Entry point called by Ableton Live when loading this Control Surface."""
    return ClaudeBridge(c_instance)

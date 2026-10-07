"""The boards that ship with the package, each as its own module declares it.

A board is added here once. The core asks this list what a board is and compares no names for it. Any other
adapter of a source is a file of an operator, which declares nothing.
"""
from . import adapter_botnet, adapter_clawdchat, adapter_fourclaw, adapter_fruitflies, providers
from .adapters import Board

BOARDS = {board.name: board for board in (*providers.BOARDS, adapter_clawdchat.BOARD, adapter_fourclaw.BOARD,
                                          adapter_fruitflies.BOARD, adapter_botnet.BOARD)}
# What stands for an adapter file where the core asks what a board declares.
FILE = Board(name="", coverage="Configured adapter scope; consult its instructions.", collect=None, subscriptions=False)


def owner(source, settings):
    """The adapter that owns a source: the one that its settings name, and with none the board of its own name.
    It is a name of BOARDS for a board of the package and the path of the file for any other."""
    return str(settings.get("adapter", source))


def declared(adapter):
    """What the board of this name declares, and FILE for any other adapter."""
    return BOARDS.get(adapter, FILE)

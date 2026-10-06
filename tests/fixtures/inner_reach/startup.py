"""A file that replaces a package name when it is imported. Read by the count, never run."""
from boardmail import providers
from boardmail.store import Store

providers.MAX_PAGES = 1

if __name__ == '__main__':
    Store('inbox.sqlite3').initialize()

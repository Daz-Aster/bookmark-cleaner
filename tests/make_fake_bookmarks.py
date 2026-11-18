"""Creates fake Chrome and Firefox bookmarks in this computer's normal locations,
so the automatic build can check the app finds and loads them."""
import json, os, sqlite3, sys
from pathlib import Path

home = Path.home()
if sys.platform.startswith("win"):
    chrome = Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data/Default"
    firefox = Path(os.environ["APPDATA"]) / "Mozilla/Firefox/Profiles/test.default"
elif sys.platform == "darwin":
    chrome = home / "Library/Application Support/Google/Chrome/Default"
    firefox = home / "Library/Application Support/Firefox/Profiles/test.default"
else:
    chrome = home / ".config/google-chrome/Default"
    firefox = home / ".mozilla/firefox/test.default"

chrome.mkdir(parents=True, exist_ok=True)
(chrome / "Bookmarks").write_text(json.dumps({"roots": {"bookmark_bar": {"type": "folder", "name": "Bar",
    "children": [{"type": "url", "id": "1", "name": "Example", "url": "https://example.com/"},
                 {"type": "url", "id": "2", "name": "Gone", "url": "https://example.com/nope"}]}}, "version": 1}))

firefox.mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(firefox / "places.sqlite")
con.executescript("""CREATE TABLE IF NOT EXISTS moz_places(id INTEGER PRIMARY KEY, url TEXT);
CREATE TABLE IF NOT EXISTS moz_bookmarks(id INTEGER PRIMARY KEY, type INT, fk INT, parent INT, title TEXT);
INSERT OR REPLACE INTO moz_places VALUES (1, 'https://example.com/');
INSERT OR REPLACE INTO moz_bookmarks VALUES (10, 2, NULL, 0, 'Menu'), (11, 1, 1, 10, 'Example');""")
con.commit(); con.close()
print("fake bookmarks created in", chrome, "and", firefox)

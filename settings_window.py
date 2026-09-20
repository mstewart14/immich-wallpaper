#!/usr/bin/env python3
"""Native settings window (GTK 3) for the Immich wallpaper rotator.

This is the settings screen on Linux. It runs in its own small process, has
no network server and opens no port, and keeps the API key in a masked field
inside the process. Everything that is not drawing widgets lives in
settings_service.py, which the web page (used on other platforms) shares.

Usage:
    python3 settings_window.py

Running it again while it is open just brings the existing window forward.
"""
from __future__ import annotations

import contextlib
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Callable

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib, Gtk
except (ImportError, ValueError) as error:  # pragma: no cover
    print(f"The settings window needs GTK 3 and PyGObject: {error}")
    print("On Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0")
    sys.exit(1)

import settings
import settings_service as service

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
APP_ID = "io.github.mstewart14.ImmichWallpaper"
# Themed icon the packages install; desktops find the window by it.
ICON_NAME = "immich-wallpaper"
ICON_PATH = HERE / "assets" / "app-icon.png"

THUMBNAIL_SIZE = 28
LOGO_SIZE = 48
TITLE = "Immich Wallpaper Rotator"
SUBTITLE = "Configure where photos come from and how the rotation behaves."
PERMISSIONS_HELP = (
    "In Immich, go to Account Settings → API Keys → New API Key, "
    "give it a name like wallpaper-rotator, and grant only these "
    "read/download scopes (untick everything else — especially "
    "anything with write, delete or admin):\n\n"
    "  • asset.read — see photo metadata\n"
    "  • asset.download — download the actual image bytes\n"
    "  • album.read — list albums to filter by\n"
    "  • person.read — list people (face groups) to filter by\n\n"
    "If your Immich version doesn't offer granular scopes, an "
    "all-permissions key also works — just keep it out of anything else "
    "and be ready to revoke it.")
PERSON_MATCH_CHOICES = (
    ("any", "Any of them (OR) — a photo just needs to contain at least "
            "one of the selected people"),
    ("all", "All of them (AND) — a photo must contain every selected "
            "person together"),
    ("both", "Both — a genuine mix of solo and together photos (each "
             "rotation is a coin flip between Any and All)"),
)
MULTI_MODE_CHOICES = (
    ("same", "Same photo on every screen — each screen gets its own "
             "copy, sized to fit it"),
    ("different", "Different photos — every screen gets its own"),
    ("span", "One picture across all screens — photos side by side, "
             "filling the width"),
)

# Column positions of the list stores used for albums and people.
SELECTED, ITEM_ID, NAME, DETAIL, THUMB = range(5)


def _background(work: Callable[[], Any], done: Callable[[Any], None]) -> None:
    """Run `work()` off the UI thread, then `done(result)` on it.

    If `work` raises, `done` receives the exception object instead.
    """
    def runner() -> None:
        try:
            result: Any = work()
        except Exception as error:  # noqa: BLE001
            result = error

        def deliver() -> bool:
            done(result)
            return False  # run once

        GLib.idle_add(deliver)

    threading.Thread(target=runner, daemon=True).start()


def _label(text: str, bold: bool = False) -> Gtk.Label:
    """A left-aligned, wrapping label showing `text` literally.

    Text is never interpreted as markup, so a name that arrives from the
    server (or anywhere else) cannot change how a label is drawn. Bold
    headings escape the text before adding the bold tag.
    """
    label = Gtk.Label()
    label.set_xalign(0)
    label.set_line_wrap(True)
    if bold:
        label.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>")
    else:
        label.set_text(text)
    return label


class ChoiceList(Gtk.Box):
    """A filterable list of checkable items (albums or people)."""

    def __init__(self, with_thumbnails: bool = False) -> None:
        """Build an empty list, optionally with a thumbnail column."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.selected: dict[str, dict[str, str]] = {}
        self.store = Gtk.ListStore(bool, str, str, str, GdkPixbuf.Pixbuf)
        self._filter_text = ""
        self._rows: dict[str, Gtk.TreeRowReference] = {}
        self.filter_entry = Gtk.SearchEntry()
        self.filter_entry.set_placeholder_text("Filter…")
        self.filter_entry.connect("search-changed", self._on_filter_changed)
        self.pack_start(self.filter_entry, False, False, 0)

        self._model = self.store.filter_new()
        self._model.set_visible_func(self._is_visible)
        self.view = Gtk.TreeView(model=self._model)
        self.view.set_headers_visible(False)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._on_toggled)
        self.view.append_column(
            Gtk.TreeViewColumn("", toggle, active=SELECTED))
        if with_thumbnails:
            self.view.append_column(Gtk.TreeViewColumn(
                "", Gtk.CellRendererPixbuf(), pixbuf=THUMB))
        name = Gtk.CellRendererText()
        name.set_property("ellipsize", 3)  # Pango.EllipsizeMode.END
        column = Gtk.TreeViewColumn("", name, text=NAME)
        column.set_expand(True)
        self.view.append_column(column)
        self.view.append_column(Gtk.TreeViewColumn(
            "", Gtk.CellRendererText(), text=DETAIL))
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(170)
        scrolled.set_shadow_type(Gtk.ShadowType.IN)
        scrolled.add(self.view)
        self.pack_start(scrolled, True, True, 0)

    # ---- content -------------------------------------------------------
    def set_items(
        self, items: list[dict[str, Any]], detail_key: str = "",
    ) -> None:
        """Replace the list; ticks follow `self.selected`."""
        self.store.clear()
        self._rows.clear()
        for item in items:
            detail = ""
            if detail_key == "count":
                detail = f"{item.get('count', 0)} photos"
            iter_ = self.store.append(
                [item["id"] in self.selected, item["id"], item["name"],
                 detail, None])
            self._rows[item["id"]] = Gtk.TreeRowReference.new(
                self.store, self.store.get_path(iter_))

    def set_thumbnail(self, item_id: str, pixbuf: GdkPixbuf.Pixbuf) -> None:
        """Show a person's thumbnail, if that row is still in the list."""
        reference = self._rows.get(item_id)
        if reference is not None and reference.valid():
            self.store[reference.get_path()][THUMB] = pixbuf

    def item_ids(self) -> list[str]:
        """The ids of every listed item, in order."""
        return [row[ITEM_ID] for row in self.store]

    # ---- filtering and ticking -----------------------------------------
    def _on_filter_changed(self, entry: Gtk.SearchEntry) -> None:
        self._filter_text = entry.get_text().strip().lower()
        self._model.refilter()

    def _is_visible(self, model, iter_, _data=None) -> bool:
        return self._filter_text in (model[iter_][NAME] or "").lower()

    def _on_toggled(self, _renderer, path: str) -> None:
        child_path = self._model.convert_path_to_child_path(
            Gtk.TreePath.new_from_string(path))
        self.toggle(self.store[child_path][ITEM_ID])

    def toggle(self, item_id: str) -> None:
        """Flip whether `item_id` is chosen."""
        for row in self.store:
            if row[ITEM_ID] == item_id:
                chosen = not row[SELECTED]
                row[SELECTED] = chosen
                if chosen:
                    self.selected[item_id] = {"id": item_id,
                                              "name": row[NAME]}
                else:
                    self.selected.pop(item_id, None)
                return


class SettingsPanel(Gtk.Box):
    """All the settings widgets and what they do."""

    def __init__(self) -> None:
        """Build the widgets and fill them from the saved config."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._closed = False
        self._monitors_usable = False
        self._build()
        self.populate(settings.load_config())
        self.reload_monitors()

    # ---- building the UI -----------------------------------------------
    def _header(self) -> Gtk.Box:
        """The logo beside the title, as on the web page."""
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for side, margin in (("top", 12), ("bottom", 4), ("start", 14),
                             ("end", 14)):
            getattr(header, f"set_margin_{side}")(margin)
        self.logo: Gtk.Image | None = None
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(ICON_PATH), LOGO_SIZE, LOGO_SIZE, True)
        except GLib.Error:
            pixbuf = None  # the logo is decoration; the title still shows
        if pixbuf is not None:
            self.logo = Gtk.Image.new_from_pixbuf(pixbuf)
            header.pack_start(self.logo, False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label()
        title.set_xalign(0)
        title.set_markup('<span size="x-large" weight="bold">'
                         f"{GLib.markup_escape_text(TITLE)}</span>")
        titles.pack_start(title, False, False, 0)
        titles.pack_start(_label(SUBTITLE), False, False, 0)
        header.pack_start(titles, True, True, 0)
        return header

    def _build(self) -> None:
        self.pack_start(self._header(), False, False, 0)
        self.notebook = Gtk.Notebook()
        self.notebook.set_vexpand(True)
        for title, page in (("Server", self._server_page()),
                            ("Photos", self._photos_page()),
                            ("Display", self._display_page()),
                            ("Screens", self._screens_page())):
            self.notebook.append_page(page, Gtk.Label(label=title))
        self.pack_start(self.notebook, True, True, 0)

        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(12)
        self.save_button = Gtk.Button(label="Save configuration")
        self.save_button.get_style_context().add_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        self.save_status = _label("")
        bar.pack_start(self.save_button, False, False, 0)
        bar.pack_start(self.save_status, False, False, 0)
        self.pack_start(bar, False, False, 0)

    def _page(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(14)
        return box

    def _server_page(self) -> Gtk.Box:
        page = self._page()
        page.pack_start(_label("Immich server", bold=True), False, False, 0)
        page.pack_start(_label("Server URL"), False, False, 0)
        self.url_entry = Gtk.Entry()
        self.url_entry.set_placeholder_text("http://192.168.1.x:2283")
        page.pack_start(self.url_entry, False, False, 0)
        page.pack_start(_label("API key"), False, False, 0)
        self.key_entry = Gtk.Entry()
        self.key_entry.set_visibility(False)
        self.key_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.key_entry.set_placeholder_text("paste API key")
        page.pack_start(self.key_entry, False, False, 0)
        expander = Gtk.Expander(
            label="Which permissions should this key have?")
        expander.add(_label(PERMISSIONS_HELP))
        page.pack_start(expander, False, False, 0)
        self.test_button = Gtk.Button(label="Test connection")
        self.test_button.connect("clicked", self._on_test)
        self.test_button.set_halign(Gtk.Align.START)
        self.server_status = _label("")
        page.pack_start(self.test_button, False, False, 0)
        page.pack_start(self.server_status, False, False, 0)
        return page

    def _photos_page(self) -> Gtk.Box:
        page = self._page()
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroll.add(inner)
        page.pack_start(scroll, True, True, 0)

        inner.pack_start(_label("Albums", bold=True), False, False, 0)
        inner.pack_start(_label(
            "Pick albums to pull from. Leave all unticked to pull from your "
            "whole library."), False, False, 0)
        self.albums = ChoiceList()
        inner.pack_start(self.albums, False, False, 0)
        self.load_albums_button = Gtk.Button(label="Load albums")
        self.load_albums_button.set_halign(Gtk.Align.START)
        self.load_albums_button.connect("clicked", self._on_load_albums)
        inner.pack_start(self.load_albums_button, False, False, 0)

        inner.pack_start(_label("People", bold=True), False, False, 0)
        inner.pack_start(_label(
            "Pick specific people to pull from. Combined with any albums "
            "using AND — leave unticked to not filter by person."),
            False, False, 0)
        self.people = ChoiceList(with_thumbnails=True)
        inner.pack_start(self.people, False, False, 0)
        self.load_people_button = Gtk.Button(label="Load people")
        self.load_people_button.set_halign(Gtk.Align.START)
        self.load_people_button.connect("clicked", self._on_load_people)
        inner.pack_start(self.load_people_button, False, False, 0)

        inner.pack_start(
            _label("When multiple people are selected"), False, False, 0)
        self.person_match: dict[str, Gtk.RadioButton] = {}
        group = None
        for value, text in PERSON_MATCH_CHOICES:
            button = Gtk.RadioButton.new_with_label_from_widget(group, text)
            button.get_child().set_line_wrap(True)
            group = group or button
            self.person_match[value] = button
            inner.pack_start(button, False, False, 0)
        self.list_status = _label("")
        inner.pack_start(self.list_status, False, False, 0)
        return page

    def _display_page(self) -> Gtk.Box:
        page = self._page()
        page.pack_start(_label("Rotation timing", bold=True), False, False, 0)
        page.pack_start(
            _label("Pull a new photo every (minutes)"), False, False, 0)
        self.interval_spin = Gtk.SpinButton.new_with_range(1, 1440, 1)
        page.pack_start(self.interval_spin, False, False, 0)
        page.pack_start(_label("Images to keep on disk"), False, False, 0)
        self.keep_spin = Gtk.SpinButton.new_with_range(1, 100, 1)
        page.pack_start(self.keep_spin, False, False, 0)
        page.pack_start(_label(
            "Only this many images ever sit on disk at once (oldest "
            "deleted as new ones arrive)."), False, False, 0)

        page.pack_start(_label("Overlays", bold=True), False, False, 0)
        self.info_check = Gtk.CheckButton.new_with_label(
            "Show photo info (date taken, location, people) in a corner of "
            "each image")
        self.date_check = Gtk.CheckButton.new_with_label(
            "Show today's date in the top-left corner")
        for check in (self.info_check, self.date_check):
            check.get_child().set_line_wrap(True)
            page.pack_start(check, False, False, 0)
        page.pack_start(_label(
            "Baked into the image at each rotation (this is a static "
            "wallpaper, not a live page), so the date updates whenever a new "
            "photo rotates in, not by the second."), False, False, 0)
        return page

    def _screens_page(self) -> Gtk.Box:
        page = self._page()
        page.pack_start(_label("Screens", bold=True), False, False, 0)
        self.screens_note = _label("")
        page.pack_start(self.screens_note, False, False, 0)

        self.multi_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 spacing=6)
        self.multi_box.pack_start(
            _label("When you have more than one screen"), False, False, 0)
        self.multi_mode: dict[str, Gtk.RadioButton] = {}
        group = None
        for value, text in MULTI_MODE_CHOICES:
            button = Gtk.RadioButton.new_with_label_from_widget(group, text)
            button.get_child().set_line_wrap(True)
            group = group or button
            self.multi_mode[value] = button
            self.multi_box.pack_start(button, False, False, 0)
        self.multi_box.pack_start(
            _label("Change these screens"), False, False, 0)
        self.monitor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                   spacing=4)
        self.multi_box.pack_start(self.monitor_box, False, False, 0)
        self.multi_box.pack_start(_label(
            "Leave all unticked to change every screen. Tick some to change "
            "only those — the others keep whatever wallpaper you set "
            "yourself."), False, False, 0)
        page.pack_start(self.multi_box, False, False, 0)

        page.pack_start(_label("Most photos on one screen"), False, False, 0)
        self.photos_spin = Gtk.SpinButton.new_with_range(1, 6, 1)
        page.pack_start(self.photos_spin, False, False, 0)
        page.pack_start(_label(
            "1 never pairs photos. 2 puts two portraits side by side on a "
            "wide screen (the default). More fills a very wide screen with "
            "several portraits."), False, False, 0)
        self.monitor_checks: dict[str, Gtk.CheckButton] = {}
        return page

    # ---- loading and collecting settings --------------------------------
    def populate(self, config: dict[str, Any]) -> None:
        """Show `config` in the widgets."""
        self.url_entry.set_text(config.get("immich_url") or "")
        self.key_entry.set_text(config.get("api_key") or "")
        self.interval_spin.set_value(config.get("interval_minutes") or 5)
        self.keep_spin.set_value(config.get("keep_count") or 2)
        self.info_check.set_active(bool(config.get("show_photo_info")))
        self.date_check.set_active(bool(config.get("show_date_overlay")))
        self.photos_spin.set_value(config.get("max_photos_per_screen") or 2)
        for choices, key, default in (
                (self.person_match, "person_match", "any"),
                (self.multi_mode, "multi_monitor_mode", "same")):
            value = config.get(key)
            choices[value if value in choices else default].set_active(True)
        self.albums.selected = {a["id"]: a for a in config.get("albums", [])}
        self.people.selected = {p["id"]: p for p in config.get("people", [])}
        self.albums.set_items(list(self.albums.selected.values()), "count")
        self.people.set_items(list(self.people.selected.values()))
        self._chosen_monitors = list(config.get("monitors") or [])

    def collect(self) -> dict[str, Any]:
        """What the widgets currently say, in save-request form."""
        return {
            "immich_url": self.url_entry.get_text().strip(),
            "api_key": self.key_entry.get_text().strip(),
            "interval_minutes": self.interval_spin.get_value_as_int(),
            "keep_count": self.keep_spin.get_value_as_int(),
            "albums": list(self.albums.selected.values()),
            "people": list(self.people.selected.values()),
            "person_match": self._active(self.person_match),
            "show_photo_info": self.info_check.get_active(),
            "show_date_overlay": self.date_check.get_active(),
            "multi_monitor_mode": self._active(self.multi_mode),
            "monitors": [name for name, check in self.monitor_checks.items()
                         if check.get_active()],
            "max_photos_per_screen": self.photos_spin.get_value_as_int(),
        }

    @staticmethod
    def _active(choices: dict[str, Gtk.RadioButton]) -> str:
        return next(value for value, button in choices.items()
                    if button.get_active())

    # ---- monitors ---------------------------------------------------------
    def reload_monitors(self) -> None:
        """Detect the screens (off the UI thread) and show them."""
        _background(service.describe_monitors, self.show_monitors)

    def show_monitors(self, info: Any) -> None:
        """Rebuild the screens list from detection results.

        Screens chosen earlier but not connected right now stay listed and
        ticked, labelled "not connected", so saving can't silently forget
        them and they can still be unticked.
        """
        if isinstance(info, Exception):
            info = {"monitors": [], "per_monitor": False}
        for child in self.monitor_box.get_children():
            self.monitor_box.remove(child)
        self.monitor_checks.clear()
        detected = info["monitors"]
        several = len(detected) > 1
        self._monitors_usable = several and info["per_monitor"]
        present = {monitor["name"] for monitor in detected}
        rows = [(m["name"], f"{m['name']} — {m['width']}×"
                            f"{m['height']}"
                            + (" (primary)" if m["primary"] else ""), True)
                for m in detected]
        rows += [(name, f"{name} — not connected", False)
                 for name in self._chosen_monitors if name not in present]
        for name, text, connected in rows:
            check = Gtk.CheckButton.new_with_label(text)
            check.set_active(name in self._chosen_monitors)
            check.set_sensitive(self._monitors_usable or not connected)
            self.monitor_box.pack_start(check, False, False, 0)
            self.monitor_checks[name] = check
        for button in self.multi_mode.values():
            button.set_sensitive(self._monitors_usable)
        self.multi_box.set_sensitive(True)
        if not detected:
            note = "Could not detect your screens, so every screen is " \
                   "treated as one."
        elif not several:
            note = ("One screen detected, so the choices below about "
                    "several screens do not apply yet.")
        elif not info["per_monitor"]:
            note = ("This desktop cannot set screens one by one, so every "
                    "screen shows the same picture.")
        else:
            note = f"{len(detected)} screens detected."
        self.screens_note.set_text(note)
        self.monitor_box.show_all()

    # ---- actions ------------------------------------------------------------
    def _set_status(self, label: Gtk.Label, ok: bool, message: str) -> None:
        label.set_text(message)
        context = label.get_style_context()
        context.remove_class("error")
        context.remove_class("success")
        context.add_class("success" if ok else "error")

    def _credentials(self, status: Gtk.Label) -> tuple[str, str] | None:
        """The URL and key to use now, or None after showing why not."""
        try:
            return service.resolve_credentials({
                "immich_url": self.url_entry.get_text(),
                "api_key": self.key_entry.get_text()})
        except service.SettingsError as error:
            self._set_status(status, False, str(error))
            return None

    def _on_test(self, _button: Gtk.Button) -> None:
        credentials = self._credentials(self.server_status)
        if credentials is None:
            return
        self.test_button.set_sensitive(False)
        self._set_status(self.server_status, True, "Testing…")
        _background(lambda: service.check_connection(*credentials),
                    self._show_test_result)

    def _show_test_result(self, result: Any) -> None:
        self.test_button.set_sensitive(True)
        if isinstance(result, Exception):
            result = {"ok": False, "error": str(result)}
        if result["ok"]:
            count = result.get("album_count")
            extra = f" — {count} albums visible" if count is not None \
                else ""
            self._set_status(self.server_status, True, f"Connected{extra}.")
        else:
            self._set_status(self.server_status, False, result["error"])

    def _on_load_albums(self, _button: Gtk.Button) -> None:
        self._load_list(self.load_albums_button, service.list_albums,
                        "albums", self.albums, "count")

    def _on_load_people(self, _button: Gtk.Button) -> None:
        self._load_list(self.load_people_button, service.list_people,
                        "people", self.people, "")

    def _load_list(self, button: Gtk.Button, fetch, key: str,
                   target: ChoiceList, detail: str) -> None:
        credentials = self._credentials(self.list_status)
        if credentials is None:
            return
        button.set_sensitive(False)
        self._set_status(self.list_status, True, "Loading…")

        def show(result: Any) -> None:
            button.set_sensitive(True)
            if isinstance(result, Exception):
                result = {"ok": False, "error": str(result)}
            if not result["ok"]:
                self._set_status(self.list_status, False, result["error"])
                return
            self._set_status(self.list_status, True, "")
            target.set_items(result[key], detail)
            if target is self.people:
                self._load_thumbnails(credentials, target.item_ids())

        _background(lambda: fetch(*credentials), show)

    def _load_thumbnails(
        self, credentials: tuple[str, str], person_ids: list[str],
    ) -> None:
        """Fetch face thumbnails one after another in the background."""
        def worker() -> None:
            for person_id in person_ids:
                if self._closed:
                    return
                try:
                    url, key = credentials
                    data = service.fetch_person_thumbnail(url, key, person_id)
                    GLib.idle_add(self._show_thumbnail, person_id, data)
                except Exception:
                    # A missing thumbnail is not worth telling the user.
                    logger.debug("no thumbnail for %s", person_id,
                                 exc_info=True)

        threading.Thread(target=worker, daemon=True).start()

    def _show_thumbnail(self, person_id: str, data: bytes) -> bool:
        loader = GdkPixbuf.PixbufLoader()
        try:
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
        except GLib.Error:
            return False
        if pixbuf is not None:
            self.people.set_thumbnail(person_id, pixbuf.scale_simple(
                THUMBNAIL_SIZE, THUMBNAIL_SIZE,
                GdkPixbuf.InterpType.BILINEAR))
        return False

    def _on_save(self, _button: Gtk.Button) -> None:
        body = self.collect()
        self.save_button.set_sensitive(False)
        self._set_status(self.save_status, True, "Saving…")
        _background(lambda: service.save_settings(body), self._show_saved)

    def _show_saved(self, result: Any) -> None:
        self.save_button.set_sensitive(True)
        if isinstance(result, Exception):
            result = {"ok": False, "error": str(result)}
        if not result["ok"]:
            self._set_status(self.save_status, False,
                             result.get("error") or "Save failed.")
        elif result["applied"]:
            self._set_status(self.save_status, True, "Saved and applied.")
        else:
            self._set_status(
                self.save_status, True,
                "Saved (wallpaper refresh failed — check the tray icon "
                "for the error).")

    def close(self) -> None:
        """Stop background work that would touch widgets."""
        self._closed = True


class SettingsWindow(Gtk.Window):
    """The top-level window around a SettingsPanel."""

    def __init__(self) -> None:
        """Create the window with its settings panel."""
        super().__init__(title="Immich Wallpaper Settings")
        self.set_default_size(680, 640)
        if ICON_PATH.exists():
            with contextlib.suppress(GLib.Error):  # the icon is decoration
                self.set_icon_from_file(str(ICON_PATH))
        self.panel = SettingsPanel()
        self.add(self.panel)
        self.connect("destroy", lambda _window: self.panel.close())


def main() -> int:
    """Run the settings window as a single-instance GTK application."""
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(TITLE)
    Gtk.Window.set_default_icon_name(ICON_NAME)
    application = Gtk.Application(application_id=APP_ID)

    def on_activate(app: Gtk.Application) -> None:
        windows = app.get_windows()
        if windows:
            windows[0].present()
            return
        window = SettingsWindow()
        app.add_window(window)
        window.show_all()

    application.connect("activate", on_activate)
    return application.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())

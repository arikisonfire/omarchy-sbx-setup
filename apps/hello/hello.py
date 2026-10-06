#!/usr/bin/env python3
"""Minimal GTK4/libadwaita test app for the omarchy-sbx bridge."""
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402


class HelloApp(Adw.Application):
    def __init__(self):
        # NON_UNIQUE: there is no D-Bus session bus on the host sandbox.
        super().__init__(
            application_id="org.example.omarchysandbox.Hello",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.clicks = 0

    def do_activate(self):
        win = Adw.ApplicationWindow(application=self, title="Hello")
        win.set_default_size(420, 260)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)

        title = Gtk.Label(label="Hello from the sandbox")
        title.add_css_class("title-1")
        self.status = Gtk.Label(label="Clicks: 0")
        button = Gtk.Button(label="Click me")
        button.add_css_class("suggested-action")
        button.add_css_class("pill")
        button.connect("clicked", self.on_click)

        box.append(title)
        box.append(self.status)
        box.append(button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(box)
        win.set_content(toolbar)
        win.present()
        print("hello: window presented", flush=True)

    def on_click(self, _button):
        self.clicks += 1
        self.status.set_label(f"Clicks: {self.clicks}")
        print(f"hello: click {self.clicks}", flush=True)


if __name__ == "__main__":
    sys.exit(HelloApp().run(sys.argv))

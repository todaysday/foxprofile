import asyncio
import os
import time

import flet as ft

from ..core.config import DATA_DIR
from ..core.container import Container
from ..core.logging import get_logger
from ..core.strings import current_language, get_string, set_language
from ..interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ..services.browser import fingerprint
from .components import (
    build_content_area,
    build_empty_state,
    build_profile_card,
    build_sidebar,
    build_ui_refs,
    rebuild_bulk_bar,
)
from .components.profile_card import build_header_row
from .components.sidebar import build_filter_buttons
from .dialogs.mcp_guide import open_mcp_guide
from .handlers import AppHandlers
from .refs import UIRefs
from .state import ITEMS_PER_PAGE, AppState
from .theme import COLORS, configure_page
from .theme.colors import FONT_SEMIBOLD
from .theme.page import ASSETS_DIR

logger = get_logger("app")


class App:
    def __init__(self, container: Container | None = None) -> None:
        c = container or Container()
        from foxprofile_ext.config import extension_enabled

        self.personal = None
        if extension_enabled():
            from foxprofile_ext.desktop import DesktopSession

            from .protected import PersonalDesktop

            self.personal = PersonalDesktop(self, DesktopSession(c))
        self.pm: IProfileManager = None if self.personal else c.profile_manager
        self.bl: IBrowserLauncher = None if self.personal else c.browser_launcher
        self.ps: IProxyService = None if self.personal else c.proxy_service
        self.state = AppState()
        self.page: ft.Page | None = None
        self._reconcile_started = False
        self.refs: UIRefs | None = None
        self.filter = "all"
        self._live_since: dict[str, str] = {}
        self._filter_column = ft.Column(spacing=2)
        self._header_slot = ft.Container()
        c.event_bus.subscribe(self.state.schedule_refresh)
        self.h = AppHandlers(
            pm=self.pm,
            bl=self.bl,
            ps=self.ps,
            state=self.state,
            get_page=lambda: self.page,
            get_refs=lambda: self.refs,
            log_fn=self._log,
            refresh_fn=self._refresh_profiles,
            get_page_profiles=self._get_page_profiles,
        )

    def run(self) -> None:
        ft.run(self._main, assets_dir=ASSETS_DIR)

    def _main(self, page: ft.Page) -> None:
        self.page = page
        configure_page(page)
        fp = ft.FilePicker()
        page.services.append(fp)
        self.clipboard = ft.Clipboard()
        page.services.append(self.clipboard)
        self._file_picker = fp
        self._build_ui()
        if self.bl:
            self.state._last_running_snapshot = self.bl.running_profile_names()
        if not self._reconcile_started:
            self._reconcile_started = True
            page.run_task(self._ui_reconcile_loop)

    def _build_ui(self) -> None:
        """(Re)build every control; strings are read at build time."""
        assert self.page is not None
        if self.personal and self.personal.session.binding is None:
            self.personal.build_entry()
            return
        if self.personal:
            self.personal.close_dialogs()
        self.refs = build_ui_refs(
            pm=self.pm,
            on_change_page=self._session_callback(self._change_page),
            file_picker=self._file_picker,
            on_search=self._session_callback(self._on_search),
        )
        self._filter_column = ft.Column(spacing=2)
        self._header_slot = ft.Container()
        self.page.title = get_string("window_title")
        self.page.controls.clear()
        layout = self._build_root_layout(self.refs)
        self.page.add(self.personal.wrap(layout) if self.personal else layout)
        self._restore_log()
        self._refresh_profiles()

    def _restore_log(self) -> None:
        """Refill a freshly built log pane (flush_log only returns new lines)."""
        r = self.refs
        assert r is not None
        lines = self.state.get_all_log_lines()[-6:]
        r.log_text.value = "\n".join(lines)
        r.log_column.height = max(72, len(lines) * 18 + 20)
        r.log_column.visible = bool(lines) and not self.state.log_collapsed
        r.log_toggle_btn.icon = (
            ft.Icons.KEYBOARD_ARROW_RIGHT
            if self.state.log_collapsed
            else ft.Icons.KEYBOARD_ARROW_DOWN
        )

    def _set_language(self, code: str) -> None:
        if code == current_language():
            return
        set_language(code)
        self.state.search_query = ""
        self._build_ui()

    def _build_root_layout(self, r: UIRefs) -> ft.Row:
        handlers = self.h
        sidebar = build_sidebar(
            r.stats_text,
            r.running_text,
            r.log_text,
            r.log_column,
            r.log_toggle_btn,
            on_new_profile=lambda _: handlers.open_add_dialog(),
            on_import=handlers.on_import,
            on_export=lambda _: handlers.on_export_open(),
            on_mcp=self._session_callback(lambda _: self._open_mcp()),
            on_toggle_log=lambda _: handlers.toggle_log(),
            on_fullscreen_log=lambda _: handlers.open_log_fullscreen(),
            filter_column=self._filter_column,
            language=current_language(),
            on_language=self._session_callback(self._set_language),
            show_mcp=self.personal.is_admin() if self.personal else True,
        )
        content = build_content_area(
            r.content_subtitle,
            r.profile_list_area,
            r.prev_btn,
            r.next_btn,
            r.page_label,
            r.bulk_bar,
            r.search_field,
            header_slot=self._header_slot,
        )
        return ft.Row(
            expand=True,
            spacing=0,
            controls=[
                sidebar,
                content,
            ],
        )

    def _open_mcp(self) -> None:
        if self.personal:
            self.personal.open_mcp()
        else:
            open_mcp_guide(self.page, self.clipboard)

    def _profile_key(self, profile) -> str:
        return profile.profile_id if self.personal else profile.name

    def _row_callback(self, callback, profile):
        key = self._profile_key(profile)
        return lambda _: callback(key)

    def _session_callback(self, callback):
        if not self.personal:
            return callback
        from foxprofile_ext.errors import DomainError

        binding = self.personal.session.binding

        def call(*args, **kwargs):
            try:
                binding.actor()
            except DomainError:
                return None
            return callback(*args, **kwargs)

        return call

    def _on_search(self, query: str) -> None:
        self.state.search_query = query.strip().lower()
        self.state.current_page = 1
        self._refresh_profiles()

    def _on_filter(self, fid: str) -> None:
        self.filter = fid
        self.state.current_page = 1
        self._refresh_profiles()

    def _filter_tests(self) -> dict:
        return {
            "all": lambda p: True,
            "running": lambda p: (
                p.is_running if self.personal else self.bl.is_running(self._profile_key(p))
            ),
            "proxy": lambda p: bool(p.proxy),
            "direct": lambda p: not p.proxy,
            "chrome": lambda p: p.engine == "chrome",
        }

    def _get_page_profiles(self, profiles=None) -> tuple[list, list, int]:
        all_profiles = self.pm.list_profiles() if profiles is None else profiles
        test = self._filter_tests().get(self.filter, lambda p: True)
        all_profiles = [p for p in all_profiles if test(p)]
        if q := self.state.search_query:
            all_profiles = [
                p for p in all_profiles if q in p.name.lower() or q in (p.proxy or "").lower()
            ]
        total = max(1, (len(all_profiles) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        self.state.current_page = min(self.state.current_page, total)
        start = (self.state.current_page - 1) * ITEMS_PER_PAGE
        return all_profiles, all_profiles[start : start + ITEMS_PER_PAGE], total

    def _refresh_profiles(self, profile_snapshot=None) -> None:
        r = self.refs
        assert r is not None
        profile_snapshot = self.pm.list_profiles() if profile_snapshot is None else profile_snapshot
        if self.personal:
            running_profiles = [p for p in profile_snapshot if p.is_running]
            running = {p.name for p in running_profiles}
            running_keys = {p.profile_id for p in running_profiles}
        else:
            running = self.bl.running_profile_names()
            running_keys = running
        self._update_stats(profile_snapshot, len(running))
        self._flush_log()
        all_profiles, page_profiles, total_pages = self._get_page_profiles(profile_snapshot)

        all_names = {self._profile_key(p) for p in profile_snapshot}
        for stale in self.state.selected_names() - all_names:
            self.state.toggle_selection(stale)

        # A failure mark lasts until the profile runs again or is gone.
        self.state.failed.intersection_update(all_names - running_keys)
        for name in running - set(self._live_since):
            self._live_since[name] = time.strftime("%H:%M")
        for name in set(self._live_since) - running:
            del self._live_since[name]

        everything = profile_snapshot
        tests = self._filter_tests()
        self._filter_column.controls = build_filter_buttons(
            self.filter,
            {fid: sum(1 for p in everything if test(p)) for fid, test in tests.items()},
            self._session_callback(self._on_filter),
        )
        names = [self._profile_key(p) for p in page_profiles]
        all_sel = bool(names) and all(self.state.is_selected(n) for n in names)
        any_sel = any(self.state.is_selected(n) for n in names)
        handlers = self.h
        self._header_slot.content = (
            build_header_row(
                all_sel,
                any_sel,
                lambda select: (
                    handlers.on_select_all_page() if select else handlers.on_deselect_page()
                ),
            )
            if page_profiles
            else None
        )

        r.profile_list_area.controls = (
            [
                build_profile_card(
                    p,
                    self.state.is_loading(self._profile_key(p)),
                    p.is_running if self.personal else self.bl.is_running(self._profile_key(p)),
                    self._row_callback(self.h.on_launch, p),
                    self._row_callback(self.h.on_edit, p),
                    self._row_callback(self.h.on_delete, p),
                    is_selected=self.state.is_selected(self._profile_key(p)),
                    on_select=self._row_callback(self.h.on_toggle_select, p),
                    on_cookies=self._row_callback(self.h.on_cookies, p),
                    device=self._device_label(self._profile_key(p), p if self.personal else None),
                    since=self._live_since.get(p.name, ""),
                    failed=self._profile_key(p) in self.state.failed,
                    runtime_state=p.state if self.personal else None,
                )
                for p in page_profiles
            ]
            if page_profiles
            else [self._build_empty_or_no_results()]
        )
        rebuild_bulk_bar(
            r.bulk_bar,
            self.state,
            page_profiles,
            {
                "launch": self.h.on_bulk_launch,
                "stop": self.h.on_bulk_stop,
                "delete": self.h.on_bulk_delete,
                "select_page": self.h.on_select_all_page,
                "deselect_page": self.h.on_deselect_page,
                "clear": self.h.on_clear_selection,
            },
        )
        self._set_subtitle(r.content_subtitle, len(profile_snapshot), len(running))
        r.page_label.value = get_string(
            "page_of",
            current=self.state.current_page,
            total=total_pages,
        )
        r.prev_btn.disabled = self.state.current_page <= 1
        r.next_btn.disabled = self.state.current_page >= total_pages
        self._safe_update()

    def _device_label(self, name: str, profile=None) -> str:
        info = (
            profile.fingerprint
            if self.personal
            else fingerprint.summary(os.path.join(DATA_DIR, name))
        )
        if not info or (self.personal and not info["exists"]):
            return ""
        cpu = f"{info['hardware_concurrency']} CPU"
        return f"{info['screen']} · {cpu}" if info.get("screen") else cpu

    def _build_empty_or_no_results(self) -> ft.Control:
        if self.pm.profiles:
            return ft.Container(
                padding=ft.Padding.symmetric(vertical=56, horizontal=24),
                alignment=ft.Alignment(0, 0),
                content=ft.Column(
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12,
                    controls=[
                        ft.Text(
                            get_string("no_search_results", query=self.state.search_query)
                            if self.state.search_query
                            else get_string("no_filter_results"),
                            size=14,
                            color=COLORS["text_sub"],
                        ),
                        ft.OutlinedButton(
                            get_string("clear_filters"),
                            on_click=self._session_callback(lambda _: self._clear_filters()),
                        ),
                    ],
                ),
            )
        return build_empty_state(self._session_callback(lambda _: self.h.open_add_dialog()))

    def _clear_filters(self) -> None:
        self.filter = "all"
        self.state.search_query = ""
        if self.refs and self.refs.search_field is not None:
            self.refs.search_field.value = ""
        self._refresh_profiles()

    def _change_page(self, delta: int) -> None:
        self.state.current_page += delta
        self._refresh_profiles()

    def _set_subtitle(self, text: ft.Text, profile_count: int, running_count: int) -> None:
        """ "10 profile · 2 đang chạy", the running count in stamp violet."""
        head = get_string("profiles_configured", count=profile_count) + " · "
        text.value = None
        text.spans = (
            [
                ft.TextSpan(head),
                ft.TextSpan(
                    str(running_count),
                    ft.TextStyle(color=COLORS["live"], font_family=FONT_SEMIBOLD),
                ),
                ft.TextSpan(" " + get_string("profiles_running_suffix")),
            ]
            if running_count
            else [ft.TextSpan(head + get_string("profiles_none_running"))]
        )

    def _update_stats(self, profiles, running_count: int) -> None:
        r = self.refs
        if r:
            r.stats_text.value = get_string(
                "total_profiles",
                count=len(profiles),
            )
            r.running_text.value = (
                get_string("browsers_running", count=running_count)
                if running_count
                else get_string("no_active_sessions")
            )

    def _log(self, message: str) -> None:
        logger.info(message)
        if self.state.add_log(message):
            self.state.schedule_refresh()

    def _flush_log(self) -> None:
        text = self.state.flush_log()
        if text is not None and self.refs:
            lines = text.split("\n")
            sidebar_lines = lines[-6:]
            self.refs.log_text.value = "\n".join(sidebar_lines)
            self.refs.log_column.height = max(72, len(sidebar_lines) * 18 + 20)
            self.refs.log_column.visible = bool(sidebar_lines) and not self.state.log_collapsed

    def _safe_update(self) -> None:
        if not self.page:
            return
        try:
            with self.state._ui_update_lock:
                self.page.update()
        except Exception as e:
            logger.error("Error updating UI: %s", e)

    async def _ui_reconcile_loop(self) -> None:
        while self.page:
            try:
                profiles = self.personal.reconcile_snapshot() if self.personal else None
                if self.personal and profiles is None:
                    await asyncio.sleep(0.12)
                    continue
                running_now = (
                    self.personal.run_snapshot(profiles)
                    if self.personal
                    else self.bl.running_profile_names()
                )
                changed = running_now != self.state._last_running_snapshot
                if changed:
                    self.state._last_running_snapshot = running_now
                if changed or self.state.consume_refresh():
                    self._refresh_profiles(profiles)
            except Exception as e:
                logger.error("Error in UI reconcile loop: %s", e)
            await asyncio.sleep(0.12)

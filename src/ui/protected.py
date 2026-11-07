"""Personal entry and administration inside the existing desktop surface."""

from __future__ import annotations

import asyncio
from pathlib import Path

import flet as ft

from foxprofile_ext.catalog import CatalogOperationError
from foxprofile_ext.dto import Role
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.policy import require_admin

from ..core.strings import get_string
from ..services.browser.cookies import CookieError
from .actions.transfer import import_profile
from .dialogs import (
    open_confirm_dialog,
    open_cookie_dialog,
    open_export_dialog,
    open_profile_dialog,
)
from .dialogs.lifecycle import authorized, scrub_dialog
from .handlers import AppHandlers
from .state import AppState
from .theme.colors import COLORS, FONT_BOLD
from .theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, OUTLINE_STYLE


def error_text(error: Exception) -> str:
    if isinstance(error, DomainError):
        if error.code is ErrorCode.PROFILE_CAPACITY_EXCEEDED and hasattr(
            error, "max_concurrent_profiles"
        ):
            return get_string("personal_capacity_reached", limit=error.max_concurrent_profiles)
        return get_string(f"personal_error_{error.code.value.lower()}")
    return get_string("personal_action_failed")


class PersonalDesktop:
    def __init__(self, app, session):
        self.app = app
        self.session = session
        self._visible_ids: frozenset[str] = frozenset()
        self._private_epoch = 0

    def private_authorizer(self, binding, identifier=None, *, admin=False):
        self.close_dialogs()
        epoch = self._private_epoch

        def authorize():
            if epoch != self._private_epoch:
                raise DomainError(ErrorCode.AUTH_REQUIRED)
            actor = binding.actor()
            if admin:
                require_admin(actor)
            if identifier is not None:
                binding.application.resolve_profile(actor, f"id:{identifier}")
            return actor

        return authorize

    def close_dialogs(self):
        self._private_epoch += 1
        while dialog := self.app.page.pop_dialog():
            scrub_dialog(dialog)

    def clear(self):
        self.session.clear()
        self.app.state = AppState()
        self.app.refs = None
        self.app.pm = self.app.bl = self.app.ps = self.app.h = None
        self.app._live_since.clear()
        self._visible_ids = frozenset()
        page = self.app.page
        if page:
            # Dialog controls can hold names, cookies, proxy credentials or MCP configs.
            self.close_dialogs()
            self.build_entry()

    def build_entry(self, error=""):
        page = self.app.page
        epoch = self._private_epoch
        username = ft.TextField(label=get_string("personal_username"), **DLG_FIELD_KWARGS)
        password = ft.TextField(
            label=get_string("personal_password"),
            password=True,
            can_reveal_password=True,
            **DLG_FIELD_KWARGS,
        )
        message = ft.Text(error, color=COLORS["error"])

        def login(_):
            if epoch != self._private_epoch or self.session.binding is not None:
                return
            try:
                binding = self.session.login(username.value or "", password.value or "")
                password.value = ""
                self.app.state = AppState()
                self.app.pm, self.app.bl, self.app.ps = binding.pm, binding.bl, binding.ps
                self.app.h = PersonalHandlers(self, binding)
                self._visible_ids = frozenset(p.profile_id for p in binding.pm.list_profiles())
                self.app._build_ui()
            except DomainError as exc:
                password.value = ""
                message.value = error_text(exc)
                page.update()

        password.on_submit = login
        page.controls.clear()
        page.add(
            ft.Container(
                expand=True,
                alignment=ft.Alignment(0, 0),
                bgcolor=COLORS["bg"],
                content=ft.Container(
                    width=380,
                    padding=28,
                    bgcolor=COLORS["card_bg"],
                    content=ft.Column(
                        tight=True,
                        spacing=14,
                        controls=[
                            ft.Text("FoxProfile", font_family=FONT_BOLD, size=24),
                            ft.Text(get_string("personal_sign_in"), color=COLORS["text_sub"]),
                            username,
                            password,
                            message,
                            ft.Button(
                                get_string("personal_sign_in"), style=ACCENT_STYLE, on_click=login
                            ),
                            ft.Row(
                                controls=[
                                    ft.TextButton(
                                        "Tiếng Việt",
                                        on_click=lambda _: self.app._set_language("vi"),
                                    ),
                                    ft.TextButton(
                                        "English", on_click=lambda _: self.app._set_language("en")
                                    ),
                                ]
                            ),
                        ],
                    ),
                ),
            )
        )

    def wrap(self, layout):
        binding = self.session.binding
        user = self.session.auth.current_user(binding.actor())
        controls = [
            ft.Text(get_string("personal_current_user", username=user.username), expand=True)
        ]
        if user.role is Role.ADMIN:
            controls.append(
                ft.OutlinedButton(
                    get_string("personal_admin"),
                    style=OUTLINE_STYLE,
                    on_click=lambda _: self.open_admin(binding),
                )
            )
        controls.append(
            ft.TextButton(
                get_string("personal_sign_out"), on_click=lambda _: self.sign_out(binding)
            )
        )
        return ft.Column(
            expand=True,
            spacing=0,
            controls=[
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=20, vertical=8),
                    bgcolor=COLORS["card_bg"],
                    content=ft.Row(controls=controls),
                ),
                layout,
            ],
        )

    def sign_out(self, binding):
        if self.session.binding is binding:
            self.clear()

    def reconcile(self):
        return self.reconcile_snapshot() is not None

    def reconcile_snapshot(self):
        binding = self.session.binding
        if binding is None:
            return None
        try:
            profiles = binding.application.list_profiles(binding.actor())
            ids = frozenset(p.profile_id for p in profiles)
            if ids != self._visible_ids:
                self._visible_ids = ids
                # Revocation drops cached logs and any still-open private profile dialog.
                self.app.state = AppState()
                self.app.h = PersonalHandlers(self, binding)
                self.close_dialogs()
                self.app._build_ui()
            return profiles
        except DomainError:
            self.clear()
            return None

    def run_state(self, profile_id):
        return next(
            (
                p.state
                for p in self.session.binding.pm.list_profiles()
                if p.profile_id == profile_id
            ),
            "closed",
        )

    def run_snapshot(self, profiles=None):
        if profiles is None:
            profiles = self.session.binding.pm.list_profiles()
        return {(p.profile_id, p.state) for p in profiles if p.is_running}

    def is_admin(self):
        return self.session.binding.actor().role is Role.ADMIN

    def open_mcp(self):
        from .dialogs.mcp_guide import open_mcp_guide

        binding = self.session.binding

        try:
            require_admin(binding.actor())
            authorize = self.private_authorizer(binding, admin=True)
            authorize()
            open_mcp_guide(self.app.page, self.app.clipboard, authorize=authorize)
        except DomainError as error:
            if self.session.binding is binding:
                self.app._log(error_text(error))

    def open_admin(self, binding):
        try:
            users = self.session.auth.list_users(binding.actor())
            settings = binding.application.admin_settings(binding.actor())
            profiles = binding.pm.list_profiles()
        except DomainError as error:
            if self.session.binding is binding:
                self.app._log(error_text(error))
            return
        page = self.app.page
        authorize = self.private_authorizer(binding, admin=True)
        feedback = ft.Text("", color=COLORS["error"])
        capacity = ft.TextField(
            value=str(settings.max_concurrent_profiles),
            label=get_string("personal_capacity"),
            **DLG_FIELD_KWARGS,
        )
        username = ft.TextField(label=get_string("personal_username"), **DLG_FIELD_KWARGS)
        password = ft.TextField(
            label=get_string("personal_password"), password=True, **DLG_FIELD_KWARGS
        )
        role = ft.Dropdown(
            value="user",
            options=[
                ft.DropdownOption("user", get_string("personal_role_user")),
                ft.DropdownOption("admin", get_string("personal_role_admin")),
            ],
        )

        def perform(effect, *, refresh=True):
            if not dialog.open:
                return
            try:
                authorize()
                effect()
                password.value = ""
                if refresh:
                    self.open_admin(binding)
                else:
                    feedback.value = get_string("personal_saved")
                    page.update()
            except (DomainError, ValueError) as error:
                if self.session.binding is not binding:
                    return
                try:
                    authorize()
                except DomainError:
                    self.clear()
                    return
                feedback.value = (
                    error_text(error)
                    if isinstance(error, DomainError)
                    else get_string("personal_error_validation_error")
                )
                page.update()

        rows = []
        for user in users:
            selected_role = ft.Dropdown(
                value=user.role.value,
                width=130,
                options=[
                    ft.DropdownOption("user", get_string("personal_role_user")),
                    ft.DropdownOption("admin", get_string("personal_role_admin")),
                ],
            )
            active = ft.Checkbox(label=get_string("personal_active"), value=user.active)
            reset = ft.TextField(
                label=get_string("personal_reset_password"),
                password=True,
                width=180,
                **DLG_FIELD_KWARGS,
            )
            ids = set(binding.application.assignments_for_user(binding.actor(), user.user_id))
            grants = [
                ft.Checkbox(label=p.name, value=p.profile_id in ids, data=p.profile_id)
                for p in profiles
            ]

            def save_user(_, user_id=user.user_id, chosen=selected_role, enabled=active):
                perform(
                    lambda: self.session.auth.update_user_state(
                        binding.actor(), user_id, role=chosen.value, active=bool(enabled.value)
                    )
                )

            def reset_user(_, user_id=user.user_id, field=reset):
                def effect():
                    self.session.auth.reset_password(binding.actor(), user_id, field.value or "")
                    field.value = ""

                perform(effect)

            def save_grants(_, user_id=user.user_id, boxes=grants):
                perform(
                    lambda: binding.application.replace_assignments(
                        binding.actor(), user_id, [box.data for box in boxes if box.value]
                    )
                )

            rows.append(
                ft.Column(
                    spacing=6,
                    controls=[
                        ft.Divider(color=COLORS["border"]),
                        ft.Text(user.username, font_family=FONT_BOLD),
                        ft.Row(
                            wrap=True,
                            controls=[
                                selected_role,
                                active,
                                ft.OutlinedButton(get_string("save_changes"), on_click=save_user),
                                reset,
                                ft.OutlinedButton(
                                    get_string("personal_reset_password"), on_click=reset_user
                                ),
                            ],
                        ),
                        ft.Text(get_string("personal_assignments")),
                        ft.Row(wrap=True, controls=grants),
                        ft.OutlinedButton(
                            get_string("personal_save_assignments"), on_click=save_grants
                        ),
                    ],
                )
            )
        dialog = ft.AlertDialog(
            modal=True,
            on_dismiss=lambda _: scrub_dialog(dialog),
            bgcolor=COLORS["card_bg"],
            title=ft.Text(get_string("personal_admin")),
            content=ft.Container(
                width=720,
                content=ft.Column(
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                    controls=[
                        ft.Text(
                            get_string(
                                "personal_occupancy",
                                occupied=settings.occupied,
                                limit=settings.max_concurrent_profiles,
                            )
                        ),
                        ft.Text(get_string("personal_capacity_hint"), color=COLORS["text_sub"]),
                        ft.Row(
                            controls=[
                                capacity,
                                ft.Button(
                                    get_string("save_changes"),
                                    style=ACCENT_STYLE,
                                    on_click=lambda _: perform(
                                        lambda: binding.application.update_admin_settings(
                                            binding.actor(), int(capacity.value or "")
                                        )
                                    ),
                                ),
                            ]
                        ),
                        ft.Divider(color=COLORS["border"]),
                        ft.Text(get_string("personal_create_user")),
                        username,
                        password,
                        role,
                        ft.Button(
                            get_string("personal_create_user"),
                            style=ACCENT_STYLE,
                            on_click=lambda _: perform(
                                lambda: self.session.auth.create_user(
                                    binding.actor(),
                                    username.value or "",
                                    password.value or "",
                                    role.value,
                                )
                            ),
                        ),
                        feedback,
                        *rows,
                    ],
                ),
            ),
            actions=[
                ft.TextButton(
                    get_string("close_btn"),
                    on_click=lambda _: (
                        self.close_dialogs() if dialog.open and authorized(authorize) else None
                    ),
                )
            ],
        )
        binding.actor()
        page.show_dialog(dialog)


class PersonalHandlers(AppHandlers):
    def __init__(self, desktop, binding):
        self.desktop, self.binding = desktop, binding
        app = desktop.app
        super().__init__(
            pm=binding.pm,
            bl=binding.bl,
            ps=binding.ps,
            state=app.state,
            get_page=lambda: app.page,
            get_refs=lambda: app.refs,
            log_fn=self.log,
            refresh_fn=self.refresh,
            get_page_profiles=app._get_page_profiles,
        )

    def log(self, message):
        if not self.is_current():
            return
        try:
            self.binding.actor()
        except DomainError:
            return
        self.desktop.app._log(message)

    def is_current(self):
        return (
            self.desktop.session.binding is self.binding and self.desktop.app.state is self._state
        )

    def refresh(self):
        self.binding.actor()
        self.desktop.app._refresh_profiles()

    def perform(self, effect):
        if not self.is_current():
            return
        try:
            self.binding.actor()
            result = effect()
            self.refresh()
            return result
        except (DomainError, CatalogOperationError, OSError, CookieError) as error:
            if isinstance(error, DomainError) and error.code is ErrorCode.AUTH_REQUIRED:
                if self.desktop.session.binding is self.binding:
                    self.desktop.clear()
            else:
                self.log(error_text(error))

    def on_launch(self, identifier):
        def effect():
            if self.binding.bl.is_running(identifier):
                self.binding.bl.stop_profile(identifier)
            else:
                self.report_results(self.binding.bl.start_profiles([identifier]))

        self.perform(effect)

    def report_results(self, results):
        for result in results:
            if result.error_code:
                try:
                    error = DomainError(result.error_code)
                except ValueError:
                    self.log(get_string("personal_action_failed"))
                else:
                    self.log(error_text(error))
            if getattr(result, "state", None) == "failed":
                self._state.failed.add(result.profile_id)
            else:
                self._state.failed.discard(result.profile_id)

    def on_edit(self, identifier):
        def effect():
            authorize = self.desktop.private_authorizer(self.binding, identifier)
            profile = self.binding.profile(identifier)
            profile_id = profile.profile_id

            def save(name, proxy, os_type, timezone, locale, _engine):
                try:
                    self.binding.pm.update_profile(
                        profile_id, name, proxy, os_type, timezone, locale
                    )
                    self.refresh()
                except (DomainError, CatalogOperationError) as error:
                    return error_text(error)

            open_profile_dialog(
                self._get_page(), self.binding.ps, save, profile, authorize=authorize
            )

        self.perform(effect)

    def open_add_dialog(self):
        def save(name, proxy, os_type, timezone, locale, engine):
            try:
                self.binding.pm.add_profile(name, proxy, os_type, timezone, locale, engine)
                self.refresh()
            except (DomainError, CatalogOperationError) as error:
                return error_text(error)

        def effect():
            authorize = self.desktop.private_authorizer(self.binding)
            open_profile_dialog(self._get_page(), self.binding.ps, save, authorize=authorize)

        self.perform(effect)

    def on_delete(self, identifier):
        def effect():
            authorize = self.desktop.private_authorizer(self.binding, identifier)
            profile = self.binding.profile(identifier)
            open_confirm_dialog(
                self._get_page(),
                profile.name,
                lambda: self.perform(lambda: self.binding.pm.delete_profile(profile.profile_id)),
                is_current=lambda: authorized(authorize),
            )

        self.perform(effect)

    def on_bulk_launch(self):
        if identifiers := sorted(self._state.selected_names()):
            self.perform(lambda: self.report_results(self.binding.bl.start_profiles(identifiers)))

    def on_bulk_stop(self):
        if identifiers := sorted(self._state.selected_names()):
            self.perform(lambda: self.report_results(self.binding.bl.stop_profiles(identifiers)))

    def on_bulk_delete(self):
        identifiers = tuple(sorted(self._state.selected_names()))
        if not identifiers:
            return

        def effect():
            profiles = self.binding.application.list_profiles(self.binding.actor())
            by_id = {profile.profile_id: profile for profile in profiles}
            if any(identifier not in by_id for identifier in identifiers):
                raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
            targets = tuple(
                identifier for identifier in identifiers if not by_id[identifier].is_running
            )
            skipped = sum(by_id[identifier].is_running for identifier in identifiers)
            if not targets:
                if skipped:
                    self.log(get_string("bulk_skip_running", count=skipped))
                return

            def confirm():
                self.perform(
                    lambda: self.report_results(
                        self.binding.application.delete_profiles(
                            self.binding.actor(), tuple(f"id:{value}" for value in targets)
                        )
                    )
                )

            authorize = self.desktop.private_authorizer(self.binding)
            body = get_string("cannot_undo")
            if skipped:
                body += "\n" + get_string("bulk_skip_running", count=skipped)
            open_confirm_dialog(
                self._get_page(),
                "",
                confirm,
                title=get_string("confirm_bulk_delete", count=len(targets)),
                body=body,
                is_current=lambda: authorized(authorize),
            )

        self.perform(effect)

    def on_select_all_page(self):
        if not self.is_current():
            return
        _, profiles, _ = self._get_page_profiles()
        self._state.select_all([p.profile_id for p in profiles])
        self._state.schedule_refresh()

    def on_deselect_page(self):
        if not self.is_current():
            return
        _, profiles, _ = self._get_page_profiles()
        for profile in profiles:
            if self._state.is_selected(profile.profile_id):
                self._state.toggle_selection(profile.profile_id)
        self._state.schedule_refresh()

    def on_toggle_select(self, identifier):
        self.perform(lambda: self._state.toggle_selection(identifier))

    def on_clear_selection(self):
        self.perform(self._state.clear_selection)

    def on_cookies(self, identifier):
        def effect():
            authorize = self.desktop.private_authorizer(self.binding, identifier)
            profile = self.binding.application.resolve_profile(
                self.binding.actor(), f"id:{identifier}"
            )
            info = self.binding.fingerprint(profile.profile_id)
            line = (
                get_string(
                    "fingerprint_current",
                    platform=info["platform"],
                    screen=info["screen"],
                    cores=info["hardware_concurrency"],
                )
                if info
                else get_string("fingerprint_none")
            )
            picker = self._get_refs().file_picker

            async def export(format):
                try:
                    authorize()
                    path = await picker.save_file(
                        file_name=f"{profile.name}_cookies.{'json' if format == 'json' else 'txt'}"
                    )
                    if path:
                        authorize()
                        text, _ = await asyncio.to_thread(
                            self.binding.cookies, profile.profile_id, format=format
                        )
                        authorize()
                        Path(path).write_text(text, encoding="utf-8")
                except DomainError as error:
                    if authorized(authorize):
                        self.log(error_text(error))
                except (CookieError, OSError, UnicodeError):
                    if authorized(authorize):
                        self.log(get_string("personal_action_failed"))

            async def import_cookies():
                try:
                    authorize()
                    files = await picker.pick_files(allowed_extensions=["json", "txt"])
                    if files and files[0].path:
                        authorize()
                        content = Path(files[0].path).read_text(encoding="utf-8-sig")
                        await asyncio.to_thread(
                            self.binding.cookies, profile.profile_id, content=content
                        )
                        authorize()
                except DomainError as error:
                    if authorized(authorize):
                        self.log(error_text(error))
                except (CookieError, OSError, UnicodeError):
                    if authorized(authorize):
                        self.log(get_string("personal_action_failed"))

            def reset():
                if not authorized(authorize):
                    return
                reset_authorize = self.desktop.private_authorizer(self.binding, profile.profile_id)
                self.perform(
                    lambda: open_confirm_dialog(
                        self._get_page(),
                        profile.name,
                        lambda: self.perform(
                            lambda: self.binding.fingerprint(profile.profile_id, reset=True)
                        ),
                        title=get_string("confirm_reset_fingerprint", name=profile.name),
                        action_label=get_string("fingerprint_reset"),
                        action_icon=ft.Icons.FINGERPRINT,
                        is_current=lambda: authorized(reset_authorize),
                    )
                )

            open_cookie_dialog(
                self._get_page(),
                profile.name,
                line,
                export,
                import_cookies,
                reset,
                is_current=lambda: authorized(authorize),
            )

        self.perform(effect)

    async def on_import(self, event=None):
        if not self.is_current():
            return
        try:
            self.binding.actor()
            authorize = self.desktop.private_authorizer(self.binding)
            await import_profile(
                self._get_refs().file_picker,
                self.binding.pm,
                self.log,
                self.refresh,
                authorize=authorize,
            )
        except DomainError as error:
            self.log(error_text(error))
        except (CatalogOperationError, OSError):
            self.log(get_string("personal_action_failed"))

    def on_export_open(self):
        def effect():
            authorize = self.desktop.private_authorizer(self.binding)
            profiles = self.binding.pm.list_profiles()
            identifiers = {p.name: p.profile_id for p in profiles}

            def complete(names, directory, include_data):
                def export_selected():
                    authorize()
                    for name in names:
                        self.binding.pm.export_profile(identifiers[name], directory, include_data)

                self.perform(export_selected)

            open_export_dialog(
                self._get_page(),
                self._get_refs().file_picker,
                profiles,
                complete,
                authorize=authorize,
            )

        self.perform(effect)

    def toggle_log(self):
        self.perform(super().toggle_log)

    def open_log_fullscreen(self):
        self.perform(super().open_log_fullscreen)

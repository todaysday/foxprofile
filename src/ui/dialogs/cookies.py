from collections.abc import Awaitable, Callable

import flet as ft

from ...core.strings import get_string
from ..theme.colors import COLORS
from ..theme.styles import OUTLINE_STYLE
from .lifecycle import close_dialog, scrub_dialog


def open_cookie_dialog(
    page: ft.Page,
    profile_name: str,
    fingerprint_line: str,
    on_export: Callable[[str], Awaitable[None]],
    on_import: Callable[[], Awaitable[None]],
    on_reset_fingerprint: Callable[[], None],
    *,
    is_current: Callable[[], bool] | None = None,
) -> None:
    """Dialog for cookie export/import and fingerprint reset of one profile."""

    def action_row(
        label: str,
        hint: str,
        icon: str,
        handler: Callable[[ft.ControlEvent], object],
    ) -> ft.Row:
        return ft.Row(
            spacing=14,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.OutlinedButton(
                    label,
                    icon=icon,
                    width=190,
                    height=40,
                    style=OUTLINE_STYLE,
                    on_click=handler,
                ),
                ft.Text(hint, size=12, color=COLORS["text_dim"], expand=True),
            ],
        )

    async def export_json(_: ft.ControlEvent) -> None:
        if not dlg.open or (is_current and not is_current()):
            return
        close_dialog(page, dlg)
        await on_export("json")

    async def export_netscape(_: ft.ControlEvent) -> None:
        if not dlg.open or (is_current and not is_current()):
            return
        close_dialog(page, dlg)
        await on_export("netscape")

    async def do_import(_: ft.ControlEvent) -> None:
        if not dlg.open or (is_current and not is_current()):
            return
        close_dialog(page, dlg)
        await on_import()

    def reset_fp(_: ft.ControlEvent) -> None:
        if not dlg.open or (is_current and not is_current()):
            return
        close_dialog(page, dlg)
        on_reset_fingerprint()

    dlg = ft.AlertDialog(
        modal=True,
        on_dismiss=lambda _: scrub_dialog(dlg),
        bgcolor=COLORS["card_bg"],
        title=ft.Text(
            get_string("cookie_dialog_title"),
            size=20,
            weight=ft.FontWeight.BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=520,
            content=ft.Column(
                tight=True,
                spacing=12,
                controls=[
                    ft.Text(
                        get_string("cookie_dialog_hint", name=profile_name),
                        size=13,
                        color=COLORS["text_sub"],
                    ),
                    ft.Container(height=4),
                    action_row(
                        get_string("cookie_export_json"),
                        get_string("cookie_export_json_hint"),
                        ft.Icons.DATA_OBJECT,
                        export_json,
                    ),
                    action_row(
                        get_string("cookie_export_netscape"),
                        get_string("cookie_export_netscape_hint"),
                        ft.Icons.DESCRIPTION,
                        export_netscape,
                    ),
                    action_row(
                        get_string("cookie_import"),
                        get_string("cookie_import_hint"),
                        ft.Icons.FILE_OPEN,
                        do_import,
                    ),
                    ft.Divider(height=16, color=COLORS["border"]),
                    ft.Text(fingerprint_line, size=12, color=COLORS["text_sub"]),
                    action_row(
                        get_string("fingerprint_reset"),
                        get_string("fingerprint_reset_hint"),
                        ft.Icons.FINGERPRINT,
                        reset_fp,
                    ),
                ],
            ),
        ),
        actions=[
            ft.TextButton(
                get_string("close_btn"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: (
                    close_dialog(page, dlg)
                    if dlg.open and (is_current is None or is_current())
                    else None
                ),
            ),
        ],
    )
    if is_current is None or is_current():
        page.show_dialog(dlg)

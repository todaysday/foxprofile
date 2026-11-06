from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ..theme.colors import COLORS
from ..theme.styles import ERROR_STYLE
from .lifecycle import close_dialog, scrub_dialog


def open_confirm_dialog(
    page: ft.Page,
    profile_name: str,
    on_confirm: Callable[[], None],
    *,
    title: str | None = None,
    body: str | None = None,
    action_label: str | None = None,
    action_icon: ft.IconData = ft.Icons.DELETE,
    is_current: Callable[[], bool] | None = None,
) -> None:

    def _on_confirm(_: ft.ControlEvent) -> None:
        if not dlg.open or (is_current and not is_current()):
            return
        on_confirm()
        close_dialog(page, dlg)

    dlg = ft.AlertDialog(
        modal=True,
        on_dismiss=lambda _: scrub_dialog(dlg),
        title=ft.Text(
            title if title is not None else get_string("confirm_delete_msg", name=profile_name),
            size=18,
            weight=ft.FontWeight.BOLD,
        ),
        content=ft.Text(
            body if body is not None else get_string("cannot_undo"),
            size=13,
            color=COLORS["text_dim"],
        ),
        actions=[
            ft.TextButton(
                get_string("cancel"),
                on_click=lambda _: (
                    close_dialog(page, dlg)
                    if dlg.open and (is_current is None or is_current())
                    else None
                ),
            ),
            ft.Button(
                action_label if action_label is not None else get_string("delete"),
                icon=action_icon,
                style=ERROR_STYLE,
                on_click=_on_confirm,
            ),
        ],
    )
    if is_current is None or is_current():
        page.show_dialog(dlg)

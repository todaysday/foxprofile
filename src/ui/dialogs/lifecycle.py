"""Recheck personal authority before publishing private dialog results."""

from collections.abc import Callable

import flet as ft

from foxprofile_ext.errors import DomainError, ErrorCode


def authorized(authorize: Callable[[], object] | None) -> bool:
    if authorize is None:
        return True
    try:
        authorize()
    except DomainError:
        return False
    return True


def dialog_authorizer(dialog: ft.AlertDialog, authorize: Callable[[], object] | None):
    if authorize is None:
        return None

    def check():
        if not dialog.open:
            raise DomainError(ErrorCode.AUTH_REQUIRED)
        return authorize()

    return check


def scrub_dialog(dialog: ft.AlertDialog) -> None:
    def scrub(control):
        if isinstance(control, (ft.Text, ft.TextField)):
            control.value = ""
        for name in ("content", "controls", "title", "actions"):
            value = getattr(control, name, None)
            if isinstance(value, ft.Control):
                scrub(value)
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, ft.Control):
                        scrub(child)

    scrub(dialog)
    dialog.title = dialog.content = None
    dialog.actions.clear()


def close_dialog(page: ft.Page, dialog: ft.AlertDialog) -> None:
    if dialog.open:
        page.pop_dialog()
    scrub_dialog(dialog)
